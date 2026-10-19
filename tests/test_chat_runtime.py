"""验证聊天分条、模块权限、DSH 请求隔离与人设试聊的无写入边界。"""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from admin_server import create_app
from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.dsh import Chat, Turn, _turn
from plugins.assistant.llm import LLM, Reply, render_reply
from plugins.assistant.memory import Memory
from plugins.assistant.modules import Modules
from plugins.assistant.search import Search
from plugins.extensions.manager import Manager
from plugins.extensions.schedule import Schedule
from profile_lab import inspect_persona
from tests.test_extensions import settings


class ExpressionTests(unittest.TestCase):
    def test_natural_chunks_and_structure(self):
        cfg = Config()
        self.assertEqual(render_reply(Reply("好呀\n明天再聊"), cfg), ["好呀明天再聊"])
        text = (
            "今天碰到一件特别有意思的事情，正打算回来告诉你，结果走着走着差点忘记了。"
            "后来看到那只猫，我又想起来了！"
        )
        chunks = render_reply(Reply(text), cfg)
        self.assertEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), text)
        for value in (
            "```python\nprint(1)\n```",
            "/任务\n/帮助",
            "1. 第一项\n2. 第二项",
            "看看这个\nhttps://example.com",
            "`a.b()`\n调用说明",
        ):
            self.assertEqual(render_reply(Reply(value), cfg), [value])
        self.assertEqual(render_reply(Reply("甲\n<分条>\n乙"), cfg), ["甲", "乙"])
        self.assertLessEqual(len(render_reply(Reply("一段较长的文字\n" * 10), cfg)), 3)

    def test_lint_is_advisory(self):
        self.assertEqual(len(inspect_persona("普通助手")["warnings"]), 3)
        good = "说话语气自然。例如：嗯，回头聊。不要编造。"
        self.assertEqual(inspect_persona(good)["warnings"], [])
        self.assertTrue(
            any("可能" in w for w in inspect_persona(good + "始终简短，每次详细解释。")["warnings"])
        )


class ModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_rechecks_permission_and_refreshes_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = Config(bot_owners={1}, model_trace_enabled=False)
            manager = Manager(Path(folder), Commands(cfg, Memory(cfg)), [Schedule()])
            manager.store.write(settings())
            lookup = AsyncMock(return_value="member")
            modules = Modules(SimpleNamespace(extensions=manager.session((123, 10, 2), lookup)))
            args = {"action": "disable", "plugin": "schedule", "scope": "group"}
            result = await modules.execute("manage_plugin", args, modules.functions())
            self.assertIn("权限不足", result["result"])
            self.assertTrue(manager.enabled(123, 10, "schedule"))
            lookup.return_value = "admin"
            await modules.execute("manage_plugin", args, modules.functions())
            self.assertFalse(manager.enabled(123, 10, "schedule"))
            self.assertNotIn("manage_schedule", {f["name"] for f in modules.functions()})
            snapshot = await modules.execute("read_capabilities", {}, modules.functions())
            self.assertEqual(snapshot["plugins"][0]["enabled"], False)
            args.update(action="enable", scope="global")
            result = await modules.execute("manage_plugin", args, modules.functions())
            self.assertIn("权限不足", result["result"])
            foreign = Modules(SimpleNamespace(extensions=manager.session((123, 11, 2), lookup)))
            snapshot = await foreign.execute("read_capabilities", {}, foreign.functions())
            self.assertEqual(snapshot["plugins"], [])

    async def test_turn_image_is_copied_and_download_is_bounded(self):
        cfg = Config(model_trace_enabled=False)
        source = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                ],
            }
        ]
        data = b"\x89PNG\r\n\x1a\n" + b"test"
        with patch(
            "plugins.assistant.dsh.download_image", AsyncMock(return_value=data)
        ) as download:
            result = await Turn(cfg, "test").prepare(source)
        download.assert_awaited_once_with("https://example.com/a.png", cfg.emotes_max_bytes)
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(data).decode(),
        )
        self.assertTrue(source[0]["content"][0]["image_url"]["url"].startswith("https:"))

    async def test_reply_uses_isolated_dsh_turn_and_closes_on_cancel(self):
        cfg = Config(model_trace_enabled=False)
        chat = Chat(None, cfg, Search(None, cfg))
        gate = asyncio.Event()

        async def step(*args):
            gate.set()
            await asyncio.Event().wait()

        fake = SimpleNamespace(request_id="test", step=step, close=AsyncMock())
        with patch("plugins.assistant.dsh.Turn", return_value=fake):
            task = asyncio.create_task(chat.reply([], "你好", "test"))
            await gate.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        fake.close.assert_awaited_once()
        self.assertIsNone(_turn.get())

    async def test_tool_exchange_only_returns_new_results(self):
        cfg = Config(model_trace_enabled=False)
        turn = Turn(cfg, "test")
        sent = []
        stream = asyncio.StreamReader()
        stream.feed_data(
            b'QQBOT_CHAT {"type":"calls","calls":['
            b'{"call_id":"a","name":"probe","arguments":"{}"}]}\n'
        )
        stream.feed_data(b'QQBOT_CHAT {"type":"final","text":"done"}\n')
        stdin = SimpleNamespace(write=sent.append, drain=AsyncMock())
        turn.process = SimpleNamespace(stdin=stdin, stdout=stream)
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}]
        _, calls, output = await turn.step(messages, True, [])
        messages.extend(output)
        LLM(None, cfg, None).append_tool_result(messages, calls[0]["call_id"], {"status": "ok"})
        await turn.step(messages, False, [])
        request = json.loads(sent[-1])
        self.assertEqual(request["results"][0]["id"], "a")
        self.assertEqual(len(request["results"][0]["messages"]), 1)
        self.assertEqual(request["functions"], [])


class ProfileLabTests(unittest.TestCase):
    def test_trial_never_writes_and_checks_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "personas").mkdir()
            profile = root / "personas/默认.txt"
            profile.write_text("已保存人设", encoding="utf-8")
            app = create_app(root)
            with TestClient(app, base_url="http://127.0.0.1:8090") as client:
                current = app.state.store.get("persona:默认")
                payload = {
                    "resource": "persona:默认",
                    "revision": current["revision"],
                    "value": "草稿人设",
                    "action": "trial",
                    "scenario": "chat",
                }
                headers = {"X-Console-Token": app.state.token}
                before = {
                    p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
                }
                with patch(
                    "plugins.assistant.dsh.Chat.reply", AsyncMock(return_value=Reply("你好"))
                ) as reply:
                    response = client.post("/api/profile-lab", json=payload, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(reply.await_count, 2)
                self.assertEqual(reply.await_args_list[0].args[3], {"persona": "已保存人设"})
                self.assertEqual(reply.await_args_list[1].args[3], {"persona": "草稿人设"})
                after = {
                    p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
                }
                self.assertEqual(before, after)
                self.assertEqual(client.post("/api/profile-lab", json=payload).status_code, 403)
                payload["revision"] = "stale"
                with patch("plugins.assistant.dsh.Chat.reply", AsyncMock()) as reply:
                    self.assertEqual(
                        client.post("/api/profile-lab", json=payload, headers=headers).status_code,
                        400,
                    )
                    reply.assert_not_awaited()
