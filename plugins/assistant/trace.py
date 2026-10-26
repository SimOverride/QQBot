"""本机模型诊断日志：独立文件、调用链关联、固定提示词折叠及凭据过滤。"""

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from nonebot.log import logger
from pydantic import SecretStr

from .prompt_store import module_value, modules

TRACE_DIR = Path(__file__).resolve().parents[2] / "logs" / "model"
CURRENT = ContextVar("model_trace", default=None)
LOCK = threading.Lock()
SENSITIVE = re.compile(r"api[_-]?key|authorization|access[_-]?token|password|secret", re.I)
HIDDEN = {"reasoning", "reasoning_content", "encrypted_content"}


def sanitize(value, config, *, fold_prompts=False):
    """不修改请求对象；凭据、图片数据和内部推理不进入诊断文件。"""
    secrets = [
        item.get_secret_value()
        for item in vars(config).values()
        if isinstance(item, SecretStr) and item.get_secret_value()
    ]
    prompts = []
    if fold_prompts:
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        for module in modules():
            for key, prompt in module_value(module)["prompts"].items():
                text = prompt.replace("{today}", today)
                if text:
                    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
                    prompts.append((text, f"<提示词:{key};字符:{len(text)};sha256:{digest}>"))
        root = Path(__file__).resolve().parents[2]
        for folder in ("personas", "styles"):
            for path in (root / folder).glob("*.txt"):
                text = path.read_text(encoding="utf-8-sig").strip()
                if text:
                    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
                    reference = f"<提示词:{folder}/{path.name};字符:{len(text)};sha256:{digest}>"
                    prompts.append((text, reference))
        prompts.sort(key=lambda item: len(item[0]), reverse=True)

    def visit(item):
        if isinstance(item, dict):
            if item.get("type") == "reasoning":
                return {"type": "reasoning", "omitted": True}
            return {
                str(k): "<已隐藏>" if SENSITIVE.search(str(k)) or k in HIDDEN else visit(v)
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [visit(v) for v in item]
        if isinstance(item, str):
            # 工具参数和用户上下文常为 JSON 字符串，先展开以折叠其中的人设。
            if item.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, (dict, list)):
                        return json.dumps(visit(parsed), ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
            for source, reference in prompts:
                item = item.replace(source, reference)
                item = item.replace(json.dumps(source, ensure_ascii=False)[1:-1], reference)
            for secret in sorted(secrets, key=len, reverse=True):
                item = item.replace(secret, "<密钥已隐藏>")
            item = re.sub(r"data:image/[^\s\"']+", "<图片数据已省略>", item)
            item = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer <已隐藏>", item)
            item = re.sub(
                r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)[\"\s]*[:=][\"\s]*)"
                r"[^\s\"&,}]+",
                r"\1<已隐藏>",
                item,
            )
            return item
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return f"<{type(item).__name__}>"

    return visit(value)


def emit(config, event, **data):
    """每行一个事件；关闭开关时不序列化数据也不访问日志目录。"""
    if not config.model_trace_enabled:
        return
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        scope = CURRENT.get() or {}
        record = {
            "time": now.isoformat(),
            "event": event,
            **{k: v for k, v in scope.items() if k != "config"},
            "data": sanitize(data, config, fold_prompts=event == "model_context"),
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with LOCK:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            path = TRACE_DIR / f"trace-{now:%Y-%m-%d}.jsonl"
            if path.exists() and path.stat().st_size >= 10 * 1024 * 1024:
                path.rename(TRACE_DIR / f"trace-{now:%Y-%m-%d}-{uuid.uuid4().hex}.jsonl")
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
            cutoff = time.time() - config.model_trace_retention_days * 86400
            for old in TRACE_DIR.glob("trace-*.jsonl"):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
    except Exception as error:
        logger.warning("model_trace_write_failure={}", type(error).__name__)


def scoped(stage):
    """通过异步上下文关联嵌套调用；并发请求拥有独立的链路编号。"""

    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            config = self.config if hasattr(self, "config") else self.service.config
            if not config.model_trace_enabled:
                return await function(self, *args, **kwargs)
            parent = CURRENT.get()
            token = CURRENT.set(
                {
                    "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
                    "span_id": uuid.uuid4().hex,
                    "parent_span_id": parent["span_id"] if parent else None,
                    "stage": stage,
                    "config": config,
                }
            )
            start = time.monotonic()
            emit(config, "stage_start")
            status = "completed"
            try:
                return await function(self, *args, **kwargs)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as error:
                status = "failed"
                emit(
                    config,
                    "stage_error",
                    error_type=type(error).__name__,
                    reason=str(error) if type(error).__name__ == "ServiceError" else None,
                )
                raise
            finally:
                emit(
                    config,
                    "stage_end",
                    status=status,
                    elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                )
                CURRENT.reset(token)

        return wrapped

    return decorate
