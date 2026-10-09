import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import extract_text
from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.llm import Reply
from plugins.assistant.memory import Memory
from plugins.assistant.service import Assistant


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(bot_owners={1},
                             queue_timeout_seconds=.05)
        self.memory = Memory(self.config)
        self.commands = Commands(self.config, self.memory)
        self.owner = (99, 10, 1)
        self.admin = (99, 10, 2)
        self.member = (99, 10, 3)

    async def run_command(self, key, text, role="member"):
        return await self.commands.run(key, text, AsyncMock(return_value=role))

    async def test_permission_matrix(self):
        for key, role in ((self.member, "member"), (self.admin, "admin"),
                          ((99, 10, 4), "owner"), (self.owner, "member")):
            for command in ("/清空", "/清空 @3", "/清空 本群", "/清空全部"):
                with self.subTest(key=key, command=command):
                    result = await self.run_command(key, command, role)
                    allowed = key == self.owner or (role in ("owner", "admin")
                                                    and command != "/清空全部")
                    self.assertEqual("权限不足" not in result, allowed)

    async def test_help_visibility(self):
        self.assertIn("/后台", await self.run_command(self.owner, "/帮助"))
        self.assertNotIn("/后台", await self.run_command(self.admin, "/帮助", "admin"))
        self.assertNotIn("/清空", await self.run_command(self.member, "/help"))
        admin_help = await self.run_command(self.admin, "/帮助", "admin")
        self.assertIn("/清空", admin_help)
        self.assertNotIn("/清空全部", admin_help)
        self.assertIn("/清空全部", await self.run_command(self.owner, "/帮助"))

    async def test_role_failure_closed_and_owner_config_authoritative(self):
        self.memory.save(self.admin, "q", "a")
        failure = AsyncMock(side_effect=RuntimeError("private detail"))
        result = await self.commands.run(self.admin, "/清空", failure)
        self.assertIn("权限不足", result)
        self.assertTrue(self.memory.history(self.admin))
        await self.commands.run(self.owner, "/清空", failure)
        self.assertEqual(failure.await_count, 1)

    async def test_target_and_scope_isolation(self):
        other_group = (99, 20, 3)
        other_bot = (98, 10, 3)
        for key in (self.member, self.admin, other_group, other_bot):
            self.memory.save(key, "q", "a")
        await self.run_command(self.admin, "/清空 @3", "admin")
        self.assertFalse(self.memory.history(self.member))
        for key in (self.admin, other_group, other_bot):
            self.assertTrue(self.memory.history(key))
        await self.run_command(self.admin, "/清空 本群", "admin")
        token = self.commands.pending[self.admin].token
        self.assertTrue(self.memory.history(self.admin))
        await self.run_command(self.admin, f"/确认 {token}", "admin")
        self.assertFalse(self.memory.history(self.admin))
        self.assertTrue(self.memory.history(other_group))
        await self.run_command(self.owner, "/清空全部")
        token = self.commands.pending[self.owner].token
        await self.run_command(self.owner, f"/确认 {token}")
        self.assertFalse(self.memory.history(other_group))
        self.assertTrue(self.memory.history(other_bot))

    async def test_confirmation_binding_expiry_replay_and_reauthorization(self):
        await self.run_command(self.admin, "/清空 本群", "admin")
        token = self.commands.pending[self.admin].token
        for key in (self.owner, (99, 20, 2), (98, 10, 2)):
            self.assertIn("没有有效", await self.run_command(key, f"/确认 {token}", "admin"))
        self.assertIn("权限不足", await self.run_command(self.admin, f"/确认 {token}"))
        self.assertIn("不正确", await self.run_command(self.admin, "/确认 wrong", "admin"))
        self.commands.pending[self.admin].expires = 0
        self.assertIn("过期", await self.run_command(self.admin, f"/确认 {token}", "admin"))
        self.commands.cleanup()
        self.assertFalse(self.commands.pending)
        await self.run_command(self.owner, "/清空全部")
        token = self.commands.pending[self.owner].token
        results = await asyncio.gather(*[
            self.run_command(self.owner, f"/确认 {token}") for _ in range(2)
        ])
        self.assertEqual(sum("已清空" in result for result in results), 1)

    async def test_bulk_clear_waits_and_prevents_stale_history(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate():
            async with self.memory.locked(self.member):
                entered.set()
                await release.wait()
                self.memory.save(self.member, "old", "old")

        task = asyncio.create_task(generate())
        await entered.wait()
        clearing = asyncio.create_task(self.memory.clear_scope(99, 10))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, clearing)
        self.assertFalse(self.memory.history(self.member))
        async with self.memory.locked(self.member):
            self.memory.save(self.member, "new", "new")
        self.assertTrue(self.memory.history(self.member))

    async def test_bulk_clear_timeout_is_atomic_and_unlocks(self):
        self.memory.save(self.admin, "q", "a")
        async with self.memory.locked(self.member):
            with self.assertRaises(TimeoutError):
                await self.memory.clear_scope(99, 10)
        self.assertTrue(self.memory.history(self.admin))
        await self.memory.clear_scope(99, 10)
        self.assertFalse(self.memory.history(self.admin))
        self.assertFalse(self.memory.gate.locked())

    async def test_slash_routing_no_llm_on_unknown_or_denied(self):
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("answer")))
        service = Assistant(self.config, llm)
        for index, text in enumerate(("/未知", "/清空", "/清空全部", "/ping extra")):
            await service.handle(99, 10, 3, index, text, AsyncMock())
        llm.reply.assert_not_awaited()
        for index, text in enumerate(("清空", "帮助", "ping"), 10):
            service.limits.cooldowns.clear()
            await service.handle(99, 10, 3, index, text, AsyncMock())
        self.assertEqual(llm.reply.await_count, 3)

    async def test_real_mentions_and_role_identity(self):
        bot = SimpleNamespace(self_id="99", get_group_member_info=AsyncMock(return_value={
            "group_id": 10, "user_id": 2, "role": "admin",
        }))
        event = SimpleNamespace(group_id=10, user_id=2, original_message=(
            MessageSegment.at(99) + Message(" /清空 ") + MessageSegment.at(3)))
        self.assertEqual(extract_text(bot, event).split(), ["/清空", "@3"])

    async def test_console_entry_permissions_and_natural_queries(self):
        llm = SimpleNamespace(reply=AsyncMock(), rephrase_notice=AsyncMock())
        service = Assistant(self.config, llm)
        for index, query in enumerate(("后台入口在哪里", "请问目前的后台地址是什么？", "/后台")):
            for user in (1, 3):
                send = AsyncMock()
                await service.handle(99, 10, user, index * 10 + user, query, send)
                self.assertEqual(send.await_count, 1)
                self.assertEqual("http://127.0.0.1:8090" in send.call_args.args[0], user == 1)
        llm.reply.assert_not_awaited()
        llm.rephrase_notice.assert_not_awaited()
        for key, role in ((self.admin, "admin"), (self.member, "owner"), ((99, -3, 3), "member")):
            self.assertIn("权限不足", await self.run_command(key, "/后台", role))
        self.assertIn("http://127.0.0.1:8090", await self.run_command((99, -1, 1), "/后台"))
        self.assertIn("用法", await self.run_command(self.owner, "/后台 extra"))
        self.assertEqual(Commands.entry_command("后台任务为什么失败"), "后台任务为什么失败")
