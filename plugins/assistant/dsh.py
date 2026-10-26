"""DSH 驱动聊天回合，Python 保留模块执行、权限和发送事务。"""

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from nonebot.log import logger

from plugins.tasks.desktop import launch
from plugins.tasks.process import ProcessJob
from plugins.tasks.runner import Runner, environment, process_options, stop_process

from .emote_collect import image_data
from .http import ServiceError
from .image_download import download_image
from .knowledge import display_name, read_directory, scope_name
from .llm import LLM
from .prompt_store import refresh_tools
from .sessions import epoch as session_epoch
from .trace import emit

_turn = ContextVar("dsh_chat_turn", default=None)


def view_home(config):
    home = config.dsh_view_home.strip() or os.environ.get("DSH_HOME", "").strip()
    return Path(home).expanduser().resolve() if home else Path.home() / ".dsh"


def view_title(scope):
    """显示平台名称及号码，会话身份仍由机器人账号和会话范围确定。"""
    bot, group, _ = scope.key
    archive = getattr(scope, "archive", None)
    root = archive.root if archive is not None else Path(__file__).resolve().parents[2]
    directory = read_directory(root)
    bucket = "groups" if group > 0 else "users"
    name = directory.get(str(bot), {}).get(bucket, {}).get(str(abs(group)), {}).get("name")
    if not name and archive is not None:
        name = (
            scope_name(archive.db, bot, group, directory)
            if group > 0
            else display_name(archive.db, bot, abs(group), directory)
        )
    if not name or name == f"群名待同步（{group}）":
        name = "群名待同步" if group > 0 else "昵称待同步"
    return f"{name}（{abs(group)}）"


@dataclass(frozen=True)
class ViewReport:
    synced: int = 0
    busy: int = 0
    errors: tuple[str, ...] = ()


def view_report(output, returncode):
    """仅解析补写摘要，原生输出和异常正文不进入业务日志。"""
    prefix = b"QQBOT_VIEW_SYNC "
    lines = [line[len(prefix) :] for line in output.splitlines() if line.startswith(prefix)]
    if not lines or len(lines[-1]) > 1024 * 1024:
        return ViewReport(errors=("ViewSyncProtocolError",))
    try:
        value = json.loads(lines[-1])
        report = value.get("report", value)
        if "error" in report:
            names = [report["error"]]
            synced, busy = 0, 0
        else:
            synced = report["synced"]
            if not isinstance(report["busy"], list) or not isinstance(report["failures"], list):
                raise ValueError("无效的补写摘要")
            busy = len(report["busy"])
            names = [item["name"] for item in report["failures"]]
            if type(synced) is not int or synced < 0:
                raise ValueError("无效的补写计数")
        errors = tuple(
            sorted(
                {
                    name
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name)
                    else "UnknownError"
                    for name in names
                }
            )
        )
        if returncode and not errors:
            errors = ("ViewSyncProcessError",)
        return ViewReport(synced=synced, busy=busy, errors=errors)
    except (ValueError, KeyError, TypeError, AttributeError):
        return ViewReport(errors=("ViewSyncProtocolError",))


async def sync_views(config):
    """仅在存在待同步记录时启动短进程，由原生写锁决定是否可追加。"""
    root = Path(__file__).resolve().parents[2]
    home = view_home(config)
    namespace = hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]
    if not any((home / "qqbot-pending" / namespace).glob("*.json")):
        return ViewReport()
    package = Runner(root, config).package()
    env = environment()
    executable = launch(package, env)[0]
    env["QQBOT_DSH_PACKAGE"] = str(package)
    process = await asyncio.create_subprocess_exec(
        executable,
        "--expose-internals",
        str(Path(__file__).with_name("dsh_view_sync.mjs")),
        str(home),
        str(root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **process_options(),
    )
    try:
        async with asyncio.timeout(20):
            output, _ = await process.communicate()
            return view_report(output, process.returncode)
    finally:
        await stop_process(process)


class ViewSync:
    """预期占用只提示一次；重试退避，恢复后重新使用正常检查间隔。"""

    def __init__(self, config):
        self.config = config
        self.delay = 15
        self.state = None

    async def run(self):
        try:
            report = await sync_views(self.config)
        except Exception as error:
            report = ViewReport(errors=(type(error).__name__,))
        state = ("error", report.errors) if report.errors else ("busy",) if report.busy else None
        previous = self.state
        self.delay = min(self.delay * 2, 300) if state and state == previous else 15
        self.state = state
        emit(
            self.config,
            "dsh_view_sync",
            synced=report.synced,
            busy=report.busy,
            errors=report.errors,
            retry_seconds=self.delay,
        )
        if state == previous:
            return
        if report.errors:
            logger.warning(
                "DSH 浏览记录补写失败（类型：{}），队列已保留，{}秒后重试；同类错误不重复提示",
                "、".join(report.errors),
                self.delay,
            )
        elif report.busy:
            logger.info("DSH 客户端正占用浏览会话，已保留补写队列；释放写锁后自动同步")
        elif previous:
            logger.info("DSH 浏览记录补写队列已清空，恢复正常检查")


class Turn:
    """单次进程恢复会话执行状态；浏览镜像保持独立。"""

    def __init__(self, config, request_id):
        self.config, self.request_id = config, request_id
        self.process = self.job = self.temp = self.drain = None
        self.offset = 0
        self.pending = None
        self.view = None
        self.budget = None
        self.runtime = None

    async def start(self):
        package = Runner(Path.cwd(), self.config).package()
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-chat-")
        root = Path(self.temp.name)
        home = self.runtime or root / "home"
        home.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": name, "disabled": True}
            for name in (
                "headless-runner",
                "headless-startup",
                "session-title-llm",
                "tool-subagent",
                "tool-subagent-fork",
                "tool-workflow",
                "tool-ralph",
            )
        ]
        entries.extend(
            [
                {
                    "id": "agent-default-model",
                    "config": {
                        "provider": "deepseek-official",
                        "model": self.config.dsh_model,
                    },
                },
                {"id": "approval", "config": {"policy": "never"}},
                {
                    "id": "permission",
                    "config": {
                        "defaultPreset": "workspace-write",
                        "presets": {
                            "workspace-write": {"sandbox": "workspace-write", "approval": "never"},
                        },
                    },
                },
                {
                    "insert": [
                        {
                            "id": "qqbot-chat",
                            "name": Path(__file__).with_name("dsh_bridge.mjs").resolve().as_uri(),
                        }
                    ]
                },
            ]
        )
        if self.view:
            entries.append({"id": "attachment-local", "config": {"dshHome": self.view["home"]}})
        patch = root / "bridge.json"
        patch.write_text(json.dumps(entries), encoding="utf-8")
        env = environment()
        env.update(
            DSH_HOME=str(home),
            DSH_TELEMETRY_DISABLED="1",
            DSH_PERMISSION_MODE="workspace-write",
            DEEPSEEK_API_KEY=self.config.deepseek_api_key.get_secret_value(),
            QQBOT_DSH_PACKAGE=str(package),
            QQBOT_OUTPUT_TOKENS=str(self.config.dsh_output_tokens),
            QQBOT_SESSION_ID=self.view["id"] if self.runtime else "",
            QQBOT_RUNTIME_CWD=str(home),
        )
        self.process = await asyncio.create_subprocess_exec(
            *launch(package, env),
            "--profile",
            "headless",
            "--patch",
            str(patch),
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=262144,
            **process_options(),
        )
        self.job = ProcessJob(self.process.pid)

        async def discard():
            # DSH 诊断可能含请求正文，业务日志只记录受控事件。
            while await self.process.stderr.read(4096):
                pass

        self.drain = asyncio.create_task(discard())

    async def close(self):
        if self.process:
            await stop_process(self.process)
        if self.job:
            self.job.close()
        if self.drain:
            await self.drain
        if self.temp:
            self.temp.cleanup()

    async def prepare(self, messages):
        result = copy.deepcopy(messages)
        for message in result:
            if message.get("type") == "function_call_output":
                message["content"] = message["output"]
            parts = message.get("content")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if part.get("type") not in ("image_url", "input_image"):
                    continue
                holder = part["image_url"]
                url = holder if isinstance(holder, str) else holder["url"]
                if not url.startswith("data:"):
                    data = await download_image(url, self.config.emotes_max_bytes)
                    _, mime = image_data(data)
                    url = f"data:{mime};base64," + base64.b64encode(data).decode()
                if isinstance(holder, str):
                    part["image_url"] = url
                else:
                    holder["url"] = url
        return result

    async def step(self, messages, allow, functions):
        if self.process is None:
            await self.start()
        item = {
            "type": "result" if self.pending else "start",
            "system": messages[0]["content"],
            "functions": refresh_tools(functions) if allow else [],
            "output_tokens": self.config.dsh_output_tokens,
            "max_steps": self.config.agent_max_calls + 2,
            "messages": await self.prepare(messages[self.offset or 1 :]),
            "view": self.view,
            "budget": self.budget.remaining() if self.budget else None,
        }
        if self.pending:
            results = item.pop("messages")
            attachments = [m for m in results if m.get("role") == "user"]
            item["results"] = []
            for index, identity in enumerate(self.pending):
                found = [m for m in results if m.get("call_id", m.get("tool_call_id")) == identity]
                if index == len(self.pending) - 1:
                    found.extend(attachments)
                item["results"].append({"id": identity, "messages": found})
        self.process.stdin.write((json.dumps(item, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()
        while line := await self.process.stdout.readline():
            if not line.startswith(b"QQBOT_CHAT "):
                continue
            event = json.loads(line[11:])
            if event["type"] == "usage":
                if self.budget:
                    self.budget.usage(event.get("usage"))
                emit(self.config, "dsh_usage", request_id=self.request_id, usage=event.get("usage"))
            elif event["type"] == "calls":
                calls = event["calls"]
                self.pending = [call["call_id"] for call in calls]
                if self.config.llm_provider == "openai":
                    output = [{"type": "function_call", **call} for call in calls]
                else:
                    output = [
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": call["call_id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["name"],
                                        "arguments": call["arguments"],
                                    },
                                }
                                for call in calls
                            ],
                        }
                    ]
                self.offset = len(messages) + len(output)
                return "", calls, output
            elif event["type"] == "final":
                emit(self.config, "dsh_reply", request_id=self.request_id, text=event["text"])
                return event["text"], [], []
            elif event["type"] == "view_error":
                from nonebot.log import logger

                logger.warning("dsh_view_save_failed request={}", self.request_id)
            elif event["type"] == "error":
                emit(
                    self.config,
                    "dsh_error",
                    request_id=self.request_id,
                    code=event.get("code"),
                    detail=event.get("message"),
                )
                notices = {
                    "BUDGET_EXHAUSTED": "本轮资源预算已用完，未能完成收尾，请缩小问题范围后重试。",
                    "STEP_LIMIT": "本轮处理步骤已达上限，请缩小问题范围后重试。",
                    "REQUEST_TIMEOUT": "本轮处理时间已用完，请稍后重试。",
                    "VISION_UNSUPPORTED": (
                        "当前 DSH 模型不支持图片，请配置视觉模型 deepseek-flash。"
                    ),
                }
                raise ServiceError(
                    notices.get(
                        event.get("code"),
                        "DSH 本次聊天未完成，请稍后重试；详细原因请查看本机模型诊断日志。",
                    )
                )
        raise ServiceError("DSH 聊天执行器已断开。")


class Chat(LLM):
    """正式聊天使用 DSH；轻量唤醒判断、审核等辅助操作使用独立 API。"""

    async def reply(self, history, user, request_id, *args, **kwargs):
        scope = kwargs.get("history_tools", args[3] if len(args) > 3 else None)
        if not hasattr(self, "_sessions"):
            self._sessions = {}
        key = tuple(scope.key[:2]) if scope is not None else ("trial", request_id)
        lock = self._sessions.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await self._reply(history, user, request_id, *args, **kwargs)
            finally:
                if not lock._waiters:
                    self._sessions.pop(key, None)

    async def _reply(self, history, user, request_id, *args, **kwargs):
        turn = Turn(self.config, request_id)
        scope = kwargs.get("history_tools", args[3] if len(args) > 3 else None)
        if scope is not None and getattr(scope, "key", None):
            from .budget import Budget

            scope.budget = turn.budget = Budget(self.config)
            bot, group, sender = scope.key
            archive = getattr(scope, "archive", None)
            project = archive.root if archive is not None else Path.cwd()
            epoch = session_epoch(project, bot, group)
            # 未手动重置时保持原有会话路径，部署本功能不会隐式重置。
            version = epoch[1] if epoch[0] == "initial" else epoch
            policy = json.dumps([sorted(self.config.bot_owners), sorted(scope.scopes), version])
            generation = hashlib.sha256(policy.encode()).hexdigest()[:16]
            turn.runtime = project / "data" / "agent" / str(bot) / str(group) / generation
            original_current = getattr(scope, "is_current", lambda: True)
            scope.is_current = lambda: (
                original_current() and session_epoch(project, bot, group) == epoch
            )
            turn.view = {
                "home": str(view_home(self.config)),
                "cwd": str(Path(__file__).resolve().parents[2]),
                "id": f"qqbot-{bot}-{'group' if group > 0 else 'private'}-{abs(group)}",
                "title": view_title(scope),
                "user": f"【QQ {sender} · 请求 {request_id}】\n{user}",
            }
        token = _turn.set(turn)
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                return await super().reply(history, user, request_id, *args, **kwargs)
        finally:
            _turn.reset(token)
            await turn.close()

    async def step(self, messages, allow_tools, request_id, functions=None, **kwargs):
        turn = _turn.get()
        if turn is None or request_id != turn.request_id:
            return await super().step(messages, allow_tools, request_id, functions, **kwargs)
        from .prompts import SEARCH_FUNCTION

        emit(
            self.config,
            "dsh_context",
            request_id=request_id,
            messages=messages,
            tools=[f["name"] for f in (functions or [])],
            model=self.config.dsh_model,
        )
        return await turn.step(
            messages, allow_tools, functions if functions is not None else [SEARCH_FUNCTION]
        )
