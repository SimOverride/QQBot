"""验证插件权限边界、管理员确认、定时去重和迁移。"""

import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import migration
from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM
from plugins.assistant.memory import Memory
from plugins.extensions.manager import Manager
from plugins.extensions.schedule import TZ, Schedule, next_time
from plugins.extensions.store import FILE, validate


def settings():
    return {
        "version": 1,
        "bots": {
            "123": {
                "schedule": {
                    "enabled": True,
                    "scope": ["10"],
                    "groups": {"10": True},
                    "data": {},
                }
            }
        },
    }


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(bot_owners={1}, model_trace_enabled=False)
        self.commands = Commands(self.config, Memory(self.config))
        self.manager = Manager(self.root, self.commands, [Schedule()])
        self.commands.extensions = self.manager
        self.manager.store.write(settings())
        self.admin = AsyncMock(return_value="admin")
        self.member = AsyncMock(return_value="member")
        self.key = (123, 10, 2)
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=TZ).timestamp()

    async def propose(self, action="once", at="2030-01-01 12:01", **changes):
        args = {"action": action, "at": at, "text": "喝水 [CQ:at,qq=all]", "job_id": None}
        args.update(changes)
        with patch("time.time", return_value=self.now):
            return await self.manager.session(self.key, self.admin).execute("manage_schedule", args)

    async def confirm(self, proposal):
        with patch("time.time", return_value=self.now):
            return await self.commands.run(self.key, proposal["confirm"], self.admin)

    def jobs(self):
        return Schedule().jobs(self.manager, 123, 10)

    async def test_scope_and_permission_checks(self):
        for key in ((123, 11, 2), (124, 10, 2), (123, -2, 2)):
            self.assertEqual(self.manager.session(key, self.admin).functions(), [])
        denied = await self.commands.run(self.key, "/插件 本群 schedule 关闭", self.member)
        self.assertIn("权限不足", denied)
        denied = await self.commands.run(self.key, "/插件 全局 schedule 关闭", self.admin)
        self.assertIn("权限不足", denied)
        denied = await self.commands.run((123, 11, 1), "/插件 本群 schedule 开启", None)
        self.assertIn("不能扩大", denied)
        result = await self.manager.session(self.key, self.member).execute(
            "manage_schedule", {"action": "list", "at": None, "text": None, "job_id": None}
        )
        self.assertEqual(result["status"], "access_denied")

    async def test_global_gate_and_group_override(self):
        await self.commands.run((123, -1, 1), "/插件 全局 schedule 关闭", None)
        self.assertFalse(self.manager.enabled(123, 10, "schedule"))
        await self.commands.run(self.key, "/插件 本群 schedule 开启", self.admin)
        self.assertFalse(self.manager.enabled(123, 10, "schedule"))
        await self.commands.run((123, -1, 1), "/插件 全局 schedule 开启", None)
        self.assertTrue(self.manager.enabled(123, 10, "schedule"))

    async def test_proposal_confirmation_is_scoped_single_use_and_rechecks_role(self):
        proposal = await self.propose()
        self.assertEqual(self.jobs(), {})
        for key, lookup in (((123, 10, 3), self.admin), (self.key, self.member)):
            with patch("time.time", return_value=self.now):
                await self.commands.run(key, proposal["confirm"], lookup)
        self.assertEqual(self.jobs(), {})
        self.assertIn("已创建", await self.confirm(proposal))
        self.assertEqual(len(self.jobs()), 1)
        self.assertIn("没有有效", await self.confirm(proposal))

    async def test_cancel_requires_confirmation_and_disable_invalidates_pending(self):
        await self.confirm(await self.propose())
        job_id = next(iter(self.jobs()))
        proposal = await self.propose("cancel", job_id=job_id)
        self.assertEqual(self.jobs()[job_id]["status"], "active")
        await self.confirm(proposal)
        self.assertEqual(self.jobs()[job_id]["status"], "cancelled")
        proposal = await self.propose()
        await self.commands.run(self.key, "/插件 本群 schedule 关闭", self.admin)
        await self.commands.run(self.key, "/插件 本群 schedule 开启", self.admin)
        self.assertIn("没有有效", await self.confirm(proposal))

    async def test_backend_toggle_invalidates_pending_confirmation(self):
        from admin.console_settings import Settings

        proposal = await self.propose()
        settings = Settings(self.root)
        for enabled in (False, True):
            settings.switch(123, "schedule", enabled, settings.plugins()["revision"], 10)
        self.assertIn("未执行", await self.confirm(proposal))
        self.assertEqual(self.jobs(), {})

    async def test_expired_and_past_time_not_created(self):
        proposal = await self.propose(at="2020-01-01 12:00")
        self.assertEqual(proposal["status"], "invalid_time")
        proposal = await self.propose()
        with patch("time.time", return_value=self.now + 301):
            response = await self.commands.run(self.key, proposal["confirm"], self.admin)
        self.assertIn("没有有效", response)
        self.assertEqual(self.jobs(), {})

    async def run_tick(self, bot, at):
        self.manager.started = self.now - 1
        self.manager.connected = {"123": self.now - 1}
        with patch("time.time", return_value=at):
            await self.manager.tick({"123": bot} if bot else {})

    async def test_single_send_is_persisted_and_plain_text(self):
        await self.confirm(await self.propose())
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        await self.run_tick(bot, self.now + 61)
        await self.run_tick(bot, self.now + 62)
        bot.send_group_msg.assert_awaited_once()
        call = bot.send_group_msg.call_args.kwargs
        self.assertEqual(call["group_id"], 10)
        self.assertEqual(call["message"].type, "text")
        self.assertEqual(next(iter(self.jobs().values()))["last_status"], "sent")
        restarted = Manager(self.root, self.commands, [Schedule()])
        with patch("time.time", return_value=self.now + 63):
            await restarted.tick({"123": bot})
        bot.send_group_msg.assert_awaited_once()

    async def test_failure_and_offline_never_retry(self):
        await self.confirm(await self.propose())
        bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=TimeoutError))
        await self.run_tick(bot, self.now + 61)
        await self.run_tick(bot, self.now + 62)
        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(next(iter(self.jobs().values()))["last_status"], "unknown")
        await self.confirm(await self.propose())
        await self.run_tick(None, self.now + 61)
        await self.run_tick(bot, self.now + 62)
        bot.send_group_msg.assert_awaited_once()

    async def test_disabled_daily_skips_occurrence_and_advances(self):
        await self.confirm(await self.propose("daily", at="12:01"))
        await self.commands.run(self.key, "/插件 本群 schedule 关闭", self.admin)
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        await self.run_tick(bot, self.now + 61)
        bot.send_group_msg.assert_not_awaited()
        job = next(iter(self.jobs().values()))
        self.assertEqual(job["next_at"], self.now + 60 + 86400)
        self.assertEqual(job["status"], "active")

    async def test_failed_reservation_prevents_send(self):
        await self.confirm(await self.propose())
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        with patch.object(self.manager.store, "write", side_effect=OSError):
            await self.run_tick(bot, self.now + 61)
        bot.send_group_msg.assert_not_awaited()

    async def test_disable_during_role_lookup_blocks_tool(self):
        async def disable():
            value = self.manager.store.read()
            value["bots"]["123"]["schedule"]["enabled"] = False
            self.manager.store.write(value)
            return "admin"

        result = await self.manager.session(self.key, disable).execute(
            "manage_schedule",
            {
                "action": "list",
                "at": None,
                "text": None,
                "job_id": None,
            },
        )
        self.assertEqual(result["status"], "plugin_disabled")

    async def test_disable_while_sending_prevents_later_task(self):
        await self.confirm(await self.propose())
        await self.confirm(await self.propose())

        async def sending(**kwargs):
            await self.commands.run(self.key, "/插件 本群 schedule 关闭", self.admin)

        bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=sending))
        await self.run_tick(bot, self.now + 61)
        bot.send_group_msg.assert_awaited_once()
        states = {job["last_status"] for job in self.jobs().values()}
        self.assertEqual(states, {"sent", "skipped"})

    async def test_reenable_does_not_send_missed_occurrence(self):
        await self.confirm(await self.propose())
        with patch("time.time", return_value=self.now + 59):
            await self.commands.run(self.key, "/插件 本群 schedule 关闭", self.admin)
        with patch("time.time", return_value=self.now + 61):
            await self.commands.run(self.key, "/插件 本群 schedule 开启", self.admin)
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        await self.run_tick(bot, self.now + 62)
        bot.send_group_msg.assert_not_awaited()

    async def test_model_tool_dispatch(self):
        tools = HistoryTools(None, self.key)
        tools.extensions = self.manager.session(self.key, self.admin)
        llm = LLM(None, self.config, SimpleNamespace(enabled=False))
        llm.step = AsyncMock(
            side_effect=[
                (
                    "",
                    [
                        {
                            "call_id": "1",
                            "name": "manage_schedule",
                            "arguments": '{"action":"list","at":null,"text":null,"job_id":null}',
                        }
                    ],
                    [],
                ),
                ("暂无任务", [], []),
            ]
        )
        await llm.investigate([{"role": "system", "content": "测试"}], "test", tools)
        self.assertIn("manage_schedule", [f["name"] for f in llm.step.call_args_list[0].args[3]])

    def test_store_rejects_cross_scope_and_invalid_data(self):
        bad = settings()
        bad["bots"]["123"]["schedule"]["groups"]["11"] = True
        with self.assertRaises(ValueError):
            validate(bad)
        with self.assertRaises(ValueError):
            validate(settings(), 124)
        self.assertEqual(next_time("daily", "12:00", self.now), self.now + 86400)

    def test_migration_preserves_destination_and_rejects_other_account(self):
        incoming, staged = self.root / "incoming", self.root / "staged"
        source = copy.deepcopy(settings())
        source["bots"]["123"]["schedule"]["enabled"] = False
        migration.write_json(incoming / FILE, source)
        migration.stage_merge(self.root, incoming, staged, 123, False)
        self.assertTrue(migration.read_json(staged / FILE)["bots"]["123"]["schedule"]["enabled"])
        self.assertTrue(migration.allowed(FILE))
        with self.assertRaises(ValueError):
            migration.stage_merge(self.root, incoming, self.root / "other", 124, False)

    async def test_migration_roundtrip_keeps_jobs_and_completed_progress(self):
        await self.confirm(await self.propose())
        output = self.root / "export.zip"
        migration.export_data(self.root, output, 123)
        destination = self.root / "destination"
        destination.mkdir()
        migration.import_data(destination, output, 123)
        self.assertEqual(migration.read_json(destination / FILE), self.manager.store.read())
        incoming = self.root / "incoming"
        source = self.manager.store.read()
        job = next(iter(source["bots"]["123"]["schedule"]["data"]["10"]["jobs"].values()))
        job["status"], job["last_status"] = "completed", "sent"
        migration.write_json(incoming / FILE, source)
        staged = self.root / "staged"
        migration.stage_merge(self.root, incoming, staged, 123, False)
        merged = migration.read_json(staged / FILE)
        job = next(iter(merged["bots"]["123"]["schedule"]["data"]["10"]["jobs"].values()))
        self.assertEqual((job["status"], job["last_status"]), ("completed", "sent"))
