"""拍一拍通知、会话边界、发送去重与模型协议回归，不操作真实QQ。"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from nonebot.adapters.onebot.v11 import PokeNotifyEvent

from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.poke import Pokes, incoming
from plugins.assistant.search import Search
from plugins.assistant.service import Assistant, Superseded


def notice(**updates):
    return PokeNotifyEvent.model_validate(
        {
            "time": int(time.time()),
            "self_id": 99,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 10,
            "user_id": 2,
            "target_id": 99,
            **updates,
        }
    )


class PokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            model_trace_enabled=False, emotes_auto_collect=False, user_cooldown_seconds=0.001
        )
        self.archive = LongTermMemory(self.root / "memory.db", self.cfg)
        self.addCleanup(self.archive.close)
        self.pokes = Pokes(self.archive)
        self.bot = SimpleNamespace(
            self_id="99",
            call_api=AsyncMock(return_value={}),
            get_group_member_info=AsyncMock(return_value={"group_id": 10, "user_id": 3}),
            send_group_msg=AsyncMock(return_value={"message_id": 900}),
        )
        self.archive.collect((99, 10, 2), 1, "拍一下甲", ["text"], time.time())

    def test_real_group_and_private_sender_formats(self):
        event = incoming(self.bot, notice())
        self.assertEqual(event.user_id, 2)
        self.assertEqual(event.group_id, 10)
        self.assertEqual(event.poke, {"sender": 2, "target": 99})
        self.assertLess(event.message_id, -(1 << 62) + 1)
        self.assertGreaterEqual(event.message_id, -(1 << 63))
        private = incoming(self.bot, notice(group_id=None, user_id=8, sender_id=2))
        self.assertEqual(private.user_id, 2)
        self.assertEqual(private.group_id, 0)
        self.assertIn("不是用户文字", str(private.original_message))
        self.assertIsNone(incoming(self.bot, notice(group_id=None, user_id=2, sender_id=99)))
        for n in [notice(target_id=3), notice(user_id=99), notice(self_id=88)]:
            self.assertIsNone(incoming(self.bot, n))

    def test_record_duplicates_restart_and_fact_exclusion(self):
        current = notice()
        event = self.pokes.receive(self.bot, current)
        self.assertIsNotNone(event)
        self.assertIsNone(self.pokes.receive(self.bot, current))
        restarted = Pokes(self.archive)
        self.assertIsNone(restarted.receive(self.bot, current))
        # 不同事件仍记录，但十秒内同一人的通知不会重复触发模型。
        self.assertIsNone(self.pokes.receive(self.bot, notice(time=current.time + 1)))
        rows = self.archive.db.execute("SELECT * FROM messages WHERE types='[\"poke\"]'").fetchall()
        self.assertEqual(len(rows), 2)
        context = self.archive.message_context(rows[0])
        self.assertEqual(context["poke"]["sender"], 2)
        text, cursor = self.archive.pending_text((99, 10, 2))
        self.assertNotIn("平台拍一拍", text)
        self.assertGreater(cursor, 0)

    async def test_dispatch_group_private_access_and_echo_suppression(self):
        contacts = SimpleNamespace(private_access=AsyncMock(return_value=(False, [])))
        group, private = AsyncMock(), AsyncMock()
        await self.pokes.dispatch(self.bot, notice(), contacts, set(), group, private)
        group.assert_awaited_once()
        self.assertTrue(group.call_args.args[3])
        private.assert_not_awaited()
        await self.pokes.dispatch(self.bot, notice(group_id=None), contacts, set(), group, private)
        private.assert_not_awaited()
        contacts.private_access.return_value = (True, [10])
        await self.pokes.dispatch(self.bot, notice(group_id=None), contacts, set(), group, private)
        private.assert_awaited_once()
        await self.pokes.dispatch(self.bot, notice(user_id=99), contacts, set(), group, private)
        self.assertEqual(group.await_count, 1)
        self.assertEqual(private.await_count, 1)

    async def test_send_group_poke_uses_current_scope_and_persists_before_api(self):
        async def sent(action, **kwargs):
            row = self.archive.db.execute(
                "SELECT metadata FROM messages WHERE message_id=1"
            ).fetchone()
            self.assertEqual(json.loads(row[0])["poke_action"]["status"], "attempting")
            self.assertEqual(action, "send_poke")
            self.assertEqual(kwargs, {"group_id": 10, "user_id": 3})
            return {}

        self.bot.call_api.side_effect = sent
        session = self.pokes.session(self.bot, 10, 2, 1)
        self.assertEqual((await session.run({"user_id": 3}))["status"], "sent")
        self.assertEqual(session.sent_target, 3)
        self.assertEqual((await session.run({"user_id": 3}))["status"], "already_attempted")
        self.bot.call_api.assert_awaited_once()
        row = self.archive.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        self.assertEqual(
            self.archive.message_context(row)["poke_action"], {"target": 3, "status": "sent"}
        )

    async def test_private_only_current_partner_and_not_robot(self):
        self.archive.collect((99, -2, 2), 2, "拍我", ["text"], time.time())
        session = self.pokes.session(self.bot, -2, 2, 2)
        self.assertEqual((await session.run({"user_id": 3}))["status"], "access_denied")
        self.assertEqual((await session.run({"user_id": 99}))["status"], "access_denied")
        self.assertEqual((await session.run({"user_id": 2}))["status"], "sent")
        self.bot.call_api.assert_awaited_once_with("send_poke", user_id=2)
        self.bot.get_group_member_info.assert_not_awaited()

    async def test_invalid_target_membership_failures_and_stale_requests(self):
        session = self.pokes.session(self.bot, 10, 2, 1)
        for args in [
            {"user_id": True},
            {"user_id": "3"},
            {"user_id": -3},
            {"user_id": 3, "group_id": 20},
            {"user_id": 2**63},
        ]:
            self.assertEqual((await session.run(args))["status"], "invalid_arguments")
        self.bot.get_group_member_info.return_value = {"group_id": 20, "user_id": 3}
        self.assertEqual((await session.run({"user_id": 3}))["status"], "access_denied")
        self.bot.get_group_member_info.side_effect = TimeoutError()
        self.assertEqual((await session.run({"user_id": 3}))["status"], "unavailable")
        self.bot.call_api.assert_not_awaited()
        current = True

        async def lookup(**kwargs):
            nonlocal current
            current = False
            return {"group_id": 10, "user_id": 3}

        self.bot.get_group_member_info.side_effect = lookup
        stale = self.pokes.session(self.bot, 10, 2, 1, lambda: current)
        with self.assertRaises(Superseded):
            await stale.run({"user_id": 3})
        self.bot.call_api.assert_not_awaited()

    async def test_unknown_results_cannot_repeat_after_restart(self):
        self.bot.call_api.side_effect = TimeoutError()
        session = self.pokes.session(self.bot, 10, 2, 1)
        self.assertEqual((await session.run({"user_id": 3}))["status"], "send_unknown")
        self.assertIsNone(session.sent_target)
        restarted = Pokes(self.archive).session(self.bot, 10, 2, 1)
        self.assertEqual((await restarted.run({"user_id": 3}))["status"], "already_attempted")
        self.bot.call_api.assert_awaited_once()

    async def test_cooldown_and_concurrent_sessions_do_not_send_twice(self):
        self.archive.collect((99, 10, 2), 2, "再拍一下", ["text"], time.time())
        first = self.pokes.session(self.bot, 10, 2, 1)
        second = self.pokes.session(self.bot, 10, 2, 2)
        results = await asyncio.gather(first.run({"user_id": 3}), second.run({"user_id": 3}))
        self.assertEqual({r["status"] for r in results}, {"sent", "cooldown"})
        self.bot.call_api.assert_awaited_once()

    async def test_silence_and_poke_only_are_valid_for_both_model_protocols(self):
        for provider in ("deepseek", "openai"):
            for do_poke in (False, True):
                cfg = self.cfg.model_copy(update={"llm_provider": provider})
                # 每个小例使用新消息，避免向模型测试混入发送去重结果。
                ident = 100 + (10 if provider == "openai" else 0) + int(do_poke)
                self.archive.collect((99, 10, 2), ident, "平台拍一拍通知", ["poke"], time.time())
                tools = HistoryTools(self.archive, (99, 10, 2))
                tools.poke = Pokes(self.archive).session(self.bot, 10, 2, ident)
                tools.allow_silent = True
                count = 0

                def respond(request):
                    nonlocal count
                    payload = json.loads(request.content)
                    if count == 0:
                        names = [
                            t["name"] if provider == "openai" else t["function"]["name"]
                            for t in payload["tools"]
                        ]
                        self.assertIn("poke_user", names)
                    use_tool = do_poke and count == 0
                    count += 1
                    if provider == "openai":
                        output = (
                            [
                                {
                                    "type": "function_call",
                                    "call_id": "p",
                                    "name": "poke_user",
                                    "arguments": '{"user_id":3}',
                                }
                            ]
                            if use_tool
                            else [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": ""}],
                                }
                            ]
                        )
                        return httpx.Response(200, json={"status": "completed", "output": output})
                    message = {"role": "assistant", "content": ""}
                    if use_tool:
                        message["tool_calls"] = [
                            {
                                "id": "p",
                                "type": "function",
                                "function": {"name": "poke_user", "arguments": '{"user_id":3}'},
                            }
                        ]
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "finish_reason": "tool_calls" if use_tool else "stop",
                                    "message": message,
                                }
                            ]
                        },
                    )

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    llm = LLM(client, cfg, Search(client, cfg))
                    result = await llm.reply([], "被拍了", "test", history_tools=tools)
                self.assertEqual(result.text, "")
                self.assertEqual(result.poke_target, 3 if do_poke else None)
                self.assertEqual(result.save, do_poke)

    async def test_group_notice_reply_cannot_quote_local_notice_and_can_stay_silent(self):
        event = self.pokes.receive(self.bot, notice())

        async def reply(*args, **kwargs):
            result = await kwargs["history_tools"].response.execute(
                {
                    "action": "reply",
                    "quote_message_id": event.message_id,
                    "topic": "",
                    "active": True,
                }
            )
            self.assertEqual(result["status"], "invalid_quote")
            return Reply("怎么啦")

        llm = SimpleNamespace(reply=AsyncMock(side_effect=reply))
        service = Assistant(self.cfg, llm, archive=self.archive)
        contacts = SimpleNamespace(refresh_group=AsyncMock(), for_scope=lambda *a: {})
        group = GroupConversation(service, contacts, GroupSettings(self.root / "group.json"))
        self.addAsyncCleanup(group.close)
        await group.respond(self.bot, event, str(event.original_message), GroupState(), True)
        message = self.bot.send_group_msg.call_args.kwargs["message"]
        self.assertEqual(message.type, "text")
        self.assertIn("拍了拍机器人", llm.reply.call_args.args[4])
        self.assertTrue(llm.reply.call_args.kwargs["history_tools"].allow_silent)
        self.bot.send_group_msg.reset_mock()
        llm.reply.side_effect = None
        llm.reply.return_value = Reply("", save=False)
        await group.respond(self.bot, event, str(event.original_message), GroupState(), True)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_poke_only_service_saves_action_without_text_message(self):
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("", poke_target=3)))
        service = Assistant(self.cfg, llm, archive=self.archive)
        send = AsyncMock()
        await service.handle(99, 10, 2, 1, "拍一下甲", send)
        send.assert_not_awaited()
        row = self.archive.db.execute("SELECT answer FROM turns WHERE message_id=1").fetchone()
        self.assertIn("已拍一拍", row[0])
        self.assertIn("3", row[0])
