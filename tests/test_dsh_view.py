"""客户端浏览记录的隔离、并发和原生持久化验证。"""

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.assistant.config import Config
from plugins.assistant.dsh import Chat
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.search import Search
from plugins.tasks.desktop import installation, launch


class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_is_grouped_and_trial_is_excluded(self):
        cfg = Config(dsh_view_home="D:/test-view")
        chat = Chat(None, cfg, Search(None, cfg))
        turns = []

        def create(*args):
            turn = SimpleNamespace(view=None, close=AsyncMock())
            turns.append(turn)
            return turn

        with (
            patch("plugins.assistant.dsh.Turn", side_effect=create),
            patch.object(LLM, "reply", AsyncMock(return_value=Reply("测试"))),
        ):
            for key in ((1, 10, 2), (1, 10, 3), (2, 10, 2), (1, -2, 2)):
                await chat.reply(
                    [], "虚构消息", "test", history_tools=SimpleNamespace(key=key, scopes={key[1]})
                )
            await chat.reply([], "试聊", "trial")
        self.assertEqual(turns[0].view["id"], turns[1].view["id"])
        self.assertNotEqual(turns[0].view["id"], turns[2].view["id"])
        self.assertNotEqual(turns[0].view["id"], turns[3].view["id"])
        self.assertIsNone(turns[4].view)
        self.assertIn("QQ 2", turns[0].view["user"])

    async def test_native_concurrent_append_and_reopen(self):
        try:
            package = installation()
        except ValueError:
            self.skipTest("未安装适配的 DSH 客户端")
        env = os.environ.copy()
        executable = launch(package, env)[0]
        env["QQBOT_DSH_PACKAGE"] = str(package)
        script = str(Path(__file__).with_name("dsh_view_check.mjs").resolve())

        async def run(mode, home, ident):
            result = await asyncio.to_thread(
                subprocess.run,
                [executable, "--expose-internals", script, mode, home, ident],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        with tempfile.TemporaryDirectory(prefix="qqbot-view-test-") as home:
            await asyncio.gather(*(run("save", home, "qqbot-test") for _ in range(4)))
            await run("save", home, "qqbot-other")
            result = json.loads(await run("read", home, "qqbot-test"))
            other = json.loads(await run("read", home, "qqbot-other"))
            self.assertEqual(result["users"], 4)
            self.assertEqual(other["users"], 1)
            locked = json.loads(await run("locked", home, "qqbot-test"))
            self.assertEqual(locked["users"], 5)
            recovered = json.loads(await run("recover", home, "qqbot-test"))
            self.assertEqual(recovered["users"], 7)
