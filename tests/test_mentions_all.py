"""全体提醒的关注优先级、主模型决策和静默边界，不调用真实平台。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import addressed, archive_event, extract_text
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.llm import Reply
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

        async def assess(context):
            start = (
                context["topics"][0]["start"]
                if context["topics"]
                else context["incoming"][0]["message_id"]
            )
            return [
                {
                    "message_id": m["message_id"],
                    "topic_start": start,
                    "title": "开黑",
                    "judgement": "讨论开黑",
                    "response_to_bot": None,
                    "should_reply": True,
                    "reply_reason": "可以参与",
                }
                for m in context["incoming"]
            ]

        self.llm = SimpleNamespace(
            reply=AsyncMock(return_value=Reply("知道啦")),
            assess_topics=AsyncMock(side_effect=assess),
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
        archive_event(self.archive, self.cfg, self.bot, self.event(100, user=3))
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

    async def test_all_context_without_changing_group_settings(self):
        original = self.settings.path.read_bytes()
        await self.respond(self.event())
        self.bot.send_group_msg.assert_awaited_once()
        self.llm.reply.assert_awaited_once()
        context = json.loads(json.loads(self.llm.reply.call_args.args[4])["group"])
        self.assertFalse(context["explicit"])
        self.assertTrue(context["mention_all"])
        self.assertNotIn("activity", context)
        self.assertEqual(self.settings.path.read_bytes(), original)

    async def test_failed_main_agent_has_no_explicit_fallback(self):
        self.llm.reply.side_effect = TimeoutError()
        await self.respond(self.event())
        self.llm.reply.assert_awaited_once()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_all_and_ordinary_messages_use_latest_trigger(self):
        self.controller.worker = AsyncMock()
        all_event = self.event()
        await self.controller.receive(self.bot, all_event, "@全体成员 今晚开黑", False)
        latest = self.event(2, all_members=False)
        await self.controller.receive(self.bot, latest, "路过", False)
        state = self.controller.states[(99, 10)]
        self.assertIs(state.pending[0], latest)
        await self.controller.receive(self.bot, self.event(3, user=99, group=20), "自己", False)
        self.assertNotIn((99, 20), self.controller.states)

    async def test_priority_does_not_bypass_stale_send_guard(self):
        state = GroupState()

        async def newer(*args, **kwargs):
            state.versions[2] = 1
            return Reply("旧回复")

        self.llm.reply.side_effect = newer
        await self.respond(self.event(), state)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_direct_mentions_keep_explicit_context(self):
        event = self.event()
        event.original_message += MessageSegment.at(99)
        self.assertTrue(addressed(self.bot, event, self.cfg))
        await self.respond(event, explicit=True)
        context = json.loads(json.loads(self.llm.reply.call_args.args[4])["group"])
        self.assertTrue(context["explicit"])
        self.assertTrue(context["mention_all"])
        self.assertNotIn("activity", context)
        self.bot.send_group_msg.assert_awaited_once()
