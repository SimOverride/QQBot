"""客户端浏览记录的隔离、并发和原生持久化验证。"""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.assistant.config import Config
from plugins.assistant.dsh import Chat, ViewReport, ViewSync, sync_views, view_report
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.search import Search
from plugins.tasks.desktop import installation, launch


class ViewSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config(model_trace_enabled=False)

    def test_report_distinguishes_ownership_from_real_errors(self):
        def encode(value):
            return b"QQBOT_VIEW_SYNC " + json.dumps(value).encode()

        waiting = view_report(encode({"synced": 0, "busy": [{}], "failures": []}), 0)
        self.assertEqual(waiting, ViewReport(busy=1))
        failed = view_report(
            encode(
                {
                    "synced": 1,
                    "busy": [{}],
                    "failures": [{"name": "SessionPersistenceCorruptionError"}],
                }
            ),
            2,
        )
        self.assertEqual(failed.errors, ("SessionPersistenceCorruptionError",))
        self.assertEqual(failed.synced, 1)
        self.assertEqual(view_report(encode({"error": "SyntaxError"}), 1).errors, ("SyntaxError",))
        self.assertEqual(
            view_report(b"unstructured-private-output", 1).errors, ("ViewSyncProtocolError",)
        )
        hidden = view_report(encode({"error": "private key or message body"}), 1)
        self.assertEqual(hidden.errors, ("UnknownError",))
        self.assertEqual(
            view_report(encode({"synced": 0, "busy": "invalid", "failures": []}), 0).errors,
            ("ViewSyncProtocolError",),
        )

    async def test_repeated_busy_is_quiet_backs_off_and_recovers(self):
        sync = ViewSync(self.cfg)
        delays = []
        reports = [ViewReport(busy=1)] * 7 + [ViewReport(synced=3), ViewReport()]
        with (
            patch("plugins.assistant.dsh.sync_views", AsyncMock(side_effect=reports)),
            patch("plugins.assistant.dsh.logger") as logger,
        ):
            for _ in range(7):
                await sync.run()
                delays.append(sync.delay)
            self.assertEqual(delays, [15, 30, 60, 120, 240, 300, 300])
            logger.warning.assert_not_called()
            self.assertEqual(logger.info.call_count, 1)
            await sync.run()
            await sync.run()
            self.assertEqual(sync.delay, 15)
            self.assertEqual(logger.info.call_count, 2)

    async def test_real_errors_are_visible_without_repeating_private_output(self):
        sync = ViewSync(self.cfg)
        reports = [ViewReport(errors=("SessionPersistenceCorruptionError",))] * 2
        with (
            patch("plugins.assistant.dsh.sync_views", AsyncMock(side_effect=reports)),
            patch("plugins.assistant.dsh.logger") as logger,
        ):
            await sync.run()
            await sync.run()
            logger.warning.assert_called_once()
            self.assertIn("SessionPersistenceCorruptionError", str(logger.warning.call_args))
        with (
            patch("plugins.assistant.dsh.sync_views", AsyncMock(side_effect=OSError("private"))),
            patch("plugins.assistant.dsh.logger") as logger,
        ):
            await sync.run()
            await sync.run()
            logger.warning.assert_called_once()
            self.assertNotIn("private", str(logger.warning.call_args))

    async def test_empty_queue_does_not_launch_helper(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("plugins.assistant.dsh.view_home", return_value=Path(directory)),
            patch("plugins.assistant.dsh.asyncio.create_subprocess_exec") as launch_helper,
        ):
            self.assertEqual(await sync_views(self.cfg), ViewReport())
            launch_helper.assert_not_called()

    async def test_helper_output_is_parsed_and_process_is_always_closed(self):
        from plugins.assistant import dsh

        namespace = hashlib.sha256(str(Path(dsh.__file__).resolve().parents[2]).lower().encode())
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "qqbot-pending" / namespace.hexdigest()[:16]
            folder.mkdir(parents=True)
            (folder / "pending.json").write_text("{}")
            process = SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(
                    return_value=(b'QQBOT_VIEW_SYNC {"synced":0,"busy":[{}],"failures":[]}', b"")
                ),
            )
            with (
                patch("plugins.assistant.dsh.view_home", return_value=Path(directory)),
                patch("plugins.assistant.dsh.Runner"),
                patch("plugins.assistant.dsh.launch", return_value=["test-runtime"]),
                patch(
                    "plugins.assistant.dsh.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=process),
                ),
                patch("plugins.assistant.dsh.stop_process", AsyncMock()) as stop,
            ):
                self.assertEqual(await sync_views(self.cfg), ViewReport(busy=1))
                stop.assert_awaited_once_with(process)
                process.communicate.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await sync_views(self.cfg)
                self.assertEqual(stop.await_count, 2)


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
            await run("save", home, "qqbot-invalid")
            invalid = json.loads(await run("invalid", home, "qqbot-invalid"))
            self.assertEqual(invalid["users"], 1)
