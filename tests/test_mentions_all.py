"""全体提醒的关注优先级、参与门槛和静默边界，不调用真实平台。"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import addressed, archive_event, extract_text
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.message_context import mentions_all
from plugins.assistant.service import Assistant


class AllMembersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(model_trace_enabled=False, emotes_auto_collect=False)
        self.archive = LongTermMemory(self.root / "memory.db", self.cfg)
        self.addCleanup(self.archive.close)
        self.settings = GroupSettings(self.root / "groups.json")
        self.llm = SimpleNamespace(
            plan_participation=AsyncMock(return_value={"reply": True, "relevance": 40}),
            reply=AsyncMock(return_value=Reply("知道啦")),
        )
        service = Assistant(self.cfg, self.llm, archive=self.archive)
        contacts = SimpleNamespace(refresh_group=AsyncMock(), for_scope=lambda *a: {})
        self.controller = GroupConversation(service, contacts, self.settings)
        self.addAsyncCleanup(self.controller.close)
        self.bot = SimpleNamespace(
            self_id="99", send_group_msg=AsyncMock(return_value={"message_id": 900})
        )

    def event(self, ident=1, all_members=True, user=2, text="今晚开黑", group=10):
        message = MessageSegment.at("all") + text if all_members else MessageSegment.text(text)
        return SimpleNamespace(
            group_id=group,
            user_id=user,
            message_id=ident,
            original_message=Message(message),
            sender=SimpleNamespace(nickname="群友"),
            time=time.time(),
            reply=None,
        )

    async def respond(self, event, state=None, explicit=False):
        archive_event(self.archive, self.cfg, self.bot, event)
        return await self.controller.respond(
            self.bot, event, extract_text(self.bot, event), state or GroupState(), explicit
        )

    def test_only_current_native_all_is_special(self):
        event = self.event()
        self.assertTrue(mentions_all(event))
        self.assertFalse(addressed(self.bot, event, self.cfg))
        self.assertIn("@全体成员", extract_text(self.bot, event))
        for text in ("@全体成员", "@all", "[CQ:at,qq=all]"):
            self.assertFalse(mentions_all(self.event(all_members=False, text=text)))
        quoted = self.event(all_members=False)
        quoted.reply = SimpleNamespace(message=self.event().original_message)
        self.assertFalse(mentions_all(quoted))
        quoted.original_message = Message(MessageSegment.at(3))
        self.assertFalse(mentions_all(quoted))

    async def test_lower_threshold_without_changing_group_settings(self):
        original = self.settings.path.read_bytes()
        await self.respond(self.event(2, all_members=False))
        self.bot.send_group_msg.assert_not_awaited()
        await self.respond(self.event())
        self.bot.send_group_msg.assert_awaited_once()
        args = self.llm.plan_participation.call_args.args
        self.assertFalse(args[5])
        self.assertTrue(args[1]["mention_all"])
        self.assertEqual(args[1]["activity"], 50)
        self.assertEqual(args[1]["attention_activity"], 70)
        self.assertIn('"mention_all": true', self.llm.reply.call_args.args[4])
        self.assertEqual(self.settings.path.read_bytes(), original)

    async def test_zero_activity_still_judges_all_and_can_reply(self):
        self.settings.set_activity(10, 0)
        self.llm.plan_participation.return_value = {"reply": True, "relevance": 85}
        await self.respond(self.event(2, all_members=False))
        self.llm.plan_participation.assert_not_awaited()
        await self.respond(self.event())
        self.assertEqual(self.llm.plan_participation.call_args.args[1]["attention_activity"], 20)
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(self.settings.get(10)["activity"], 0)

    async def test_all_does_not_force_response_or_accept_invalid_scores(self):
        self.settings.set_activity(10, 0)
        cases = [
            {"reply": False, "relevance": 100},
            {"reply": True, "relevance": 100, "ended": True},
            {"reply": True, "relevance": 79},
            {"reply": True, "relevance": True},
            {"reply": True, "relevance": 101},
            {},
        ]
        for decision in cases:
            self.llm.plan_participation.return_value = decision
            await self.respond(self.event())
        self.bot.send_group_msg.assert_not_awaited()
        self.llm.reply.assert_not_awaited()

    async def test_failed_planning_has_no_explicit_fallback(self):
        self.llm.plan_participation.side_effect = TimeoutError()
        await self.respond(self.event())
        self.llm.reply.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_pending_all_survives_ordinary_message_and_zero_activity(self):
        self.settings.set_activity(10, 0)
        self.controller.worker = AsyncMock()
        all_event = self.event()
        await self.controller.receive(self.bot, all_event, "@全体成员 今晚开黑", False)
        await self.controller.receive(self.bot, self.event(2, all_members=False), "路过", False)
        state = self.controller.states[(99, 10)]
        self.assertIs(state.pending[0], all_event)
        self.assertTrue(state.attention.is_set())
        # 同账号其他群独立，自己的全体提醒不进入处理。
        await self.controller.receive(self.bot, self.event(3, user=99, group=20), "自己", False)
        self.assertNotIn((99, 20), self.controller.states)

    async def test_priority_wakes_long_wait_and_keeps_all_as_trigger(self):
        self.controller.respond = AsyncMock()
        state = self.controller.states.setdefault((99, 10), GroupState())
        state.last_checked = state.last_sent = time.monotonic()
        entered = asyncio.Event()
        delays = []
        original_wait = self.controller.wait_pending

        async def wait(current, delay):
            delays.append(delay)
            if len(delays) == 1:
                entered.set()
                return await original_wait(current, delay)
            return False

        with patch.object(self.controller, "wait_pending", side_effect=wait):
            ordinary = self.event(2, all_members=False)
            await self.controller.receive(self.bot, ordinary, "普通消息", False)
            await asyncio.wait_for(entered.wait(), 1)
            all_event = self.event()
            await self.controller.receive(self.bot, all_event, "@全体成员 今晚开黑", False)
            await self.controller.receive(
                self.bot, self.event(3, all_members=False), "跟一句", False
            )
            await asyncio.wait_for(state.task, 1)
        self.assertGreater(delays[0], 30)
        self.assertLessEqual(delays[1], 8)
        self.controller.respond.assert_awaited_once()
        self.assertIs(self.controller.respond.call_args.args[1], all_event)
        self.assertFalse(self.controller.respond.call_args.args[4])

    async def test_priority_does_not_bypass_stale_send_guard(self):
        state = GroupState()
        self.llm.plan_participation.return_value = {"reply": True, "relevance": 100}

        async def newer(*args, **kwargs):
            state.revision += 1
            return Reply("旧回复")

        self.llm.reply.side_effect = newer
        await self.respond(self.event(), state)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_attention_cap_and_direct_mentions_keep_original_behavior(self):
        self.settings.set_activity(10, 95)
        self.llm.plan_participation.return_value = {"reply": False}
        event = self.event()
        event.original_message += MessageSegment.at(99)
        self.assertTrue(addressed(self.bot, event, self.cfg))
        await self.respond(event, explicit=True)
        self.assertEqual(self.llm.plan_participation.call_args.args[1]["attention_activity"], 100)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_planner_payload_marks_all_without_explicit_flag(self):
        config = {"activity": 0, "mention_all": True, "attention_activity": 20}
        llm = LLM(None, self.cfg, SimpleNamespace(enabled=False))
        llm.step = AsyncMock(return_value=('{"reply":false}', [], []))
        decision = await llm.plan_participation([], config, {}, {}, 1, False, 99)
        self.assertFalse(decision["reply"])
        messages = llm.step.call_args.args[0]
        payload = json.loads(messages[-1]["content"])
        self.assertFalse(payload["explicit"])
        self.assertTrue(payload["settings"]["mention_all"])
        self.assertIn("不强制回应", messages[0]["content"])
        self.assertIn("不改变身份", messages[0]["content"])
