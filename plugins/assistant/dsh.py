"""DSH 驱动聊天回合，Python 保留模块执行、权限和发送事务。"""

import asyncio
import base64
import copy
import json
import tempfile
import os
from contextvars import ContextVar
from pathlib import Path

from plugins.tasks.desktop import launch
from plugins.tasks.process import ProcessJob
from plugins.tasks.runner import Runner, environment, process_options, stop_process

from .emote_collect import image_data
from .http import ServiceError
from .image_download import download_image
from .llm import LLM
from .prompt_store import refresh_tools
from .trace import emit

_turn = ContextVar("dsh_chat_turn", default=None)


class Turn:
    """单个请求的独立执行器；结束即回收，不保留未发送问答。"""

    def __init__(self, config, request_id):
        self.config, self.request_id = config, request_id
        self.process = self.job = self.temp = self.drain = None
        self.offset = 0
        self.pending = None
        self.view = None

    async def start(self):
        package = Runner(Path.cwd(), self.config).package()
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-chat-")
        root = Path(self.temp.name)
        (root / "home").mkdir()
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
            DSH_HOME=str(root / "home"),
            DSH_TELEMETRY_DISABLED="1",
            DSH_PERMISSION_MODE="workspace-write",
            DEEPSEEK_API_KEY=self.config.deepseek_api_key.get_secret_value(),
            QQBOT_DSH_PACKAGE=str(package),
            QQBOT_OUTPUT_TOKENS=str(self.config.llm_max_output_tokens),
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
            "output_tokens": self.config.llm_max_output_tokens,
            "max_steps": self.config.history_max_calls + 2,
            "messages": await self.prepare(messages[self.offset or 1 :]),
            "view": self.view,
        }
        if self.pending:
            item["id"] = self.pending
        self.process.stdin.write((json.dumps(item, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()
        while line := await self.process.stdout.readline():
            if not line.startswith(b"QQBOT_CHAT "):
                continue
            event = json.loads(line[11:])
            if event["type"] == "usage":
                emit(self.config, "dsh_usage", request_id=self.request_id, usage=event.get("usage"))
            elif event["type"] == "call":
                self.pending = event["id"]
                call = {
                    "call_id": event["id"],
                    "name": event["name"],
                    "arguments": event["arguments"],
                }
                # 这些协议记录仅供原有业务校验器定位结果；DSH 自身维护真实工具回合。
                output = (
                    {"type": "function_call", **call}
                    if self.config.llm_provider == "openai"
                    else {
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
                        ],
                    }
                )
                self.offset = len(messages) + 1
                return "", [call], [output]
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
                    detail=event.get("message"),
                )
                raise ServiceError("DSH 聊天执行失败，请检查本机安装和模型配置。")
        raise ServiceError("DSH 聊天执行器已断开。")


class Chat(LLM):
    """正式聊天使用 DSH；认知提取、审核等独立模型操作保持各自职责。"""

    async def reply(self, history, user, request_id, *args, **kwargs):
        turn = Turn(self.config, request_id)
        scope = kwargs.get("history_tools")
        if scope is not None and getattr(scope, "key", None):
            bot, group, sender = scope.key
            home = self.config.dsh_view_home.strip() or os.environ.get("DSH_HOME", "").strip()
            turn.view = {
                "home": str(Path(home).expanduser().resolve() if home else Path.home() / ".dsh"),
                "cwd": str(Path(__file__).resolve().parents[2]),
                "id": f"qqbot-{bot}-{'group' if group > 0 else 'private'}-{abs(group)}",
                "title": f"QQBot · {bot} · {'群' if group > 0 else '私聊'} {abs(group)}",
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
