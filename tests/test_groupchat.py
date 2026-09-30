import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.llm import Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.service import Assistant


class GroupChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = GroupSettings(root / "groups.json")
        self.cfg = Config(bot_owners={1}, user_cooldown_seconds=0.001)
        self.archive = LongTermMemory(root / "memory.db", self.cfg)
        self.llm = SimpleNamespace(
            plan_participation=AsyncMock(
                return_value={
                    "reply": True,
                    "relevance": 80,
                    "quote": True,
                    "topic": "天文摄影",
                    "active": True,
                    "ended": False,
                }
            ),
            reply=AsyncMock(return_value=Reply("可以试试拍月亮。")),
        )
        self.service = Assistant(self.cfg, self.llm, archive=self.archive)
        self.service.commands.group_settings = self.settings
        self.contacts = SimpleNamespace(refresh_group=AsyncMock(), for_scope=lambda *a: {})
        self.controller = GroupConversation(self.service, self.contacts, self.settings)
        self.bot = SimpleNamespace(
            self_id="99", send_group_msg=AsyncMock(return_value={"message_id": 900})
        )
        self.event = SimpleNamespace(group_id=10, user_id=2, message_id=12)
        self.archive.collect((99, 10, 3), 11, "我想拍月亮", ["text"], time.time())
        self.archive.collect((99, 10, 2), 12, "有什么建议吗", ["text"], time.time())
        self.archive.collect((99, 20, 3), 20, "别的群的消息", ["text"], time.time())

    async def asyncTearDown(self):
        await self.controller.close()
        self.archive.close()
        self.temp.cleanup()

    async def test_multispeaker_context_and_quote(self):
        state = GroupState()
        await self.controller.respond(self.bot, self.event, "有什么建议吗", state, False)
        self.assertTrue(state.active)
        context = self.llm.reply.call_args.args[4]
        self.assertIn("我想拍月亮", context)
        self.assertNotIn("别的群的消息", context)
        message = self.bot.send_group_msg.call_args.kwargs["message"]
        self.assertEqual(message[0].type, "reply")
        self.assertEqual(str(message[0].data["id"]), "12")
        self.assertEqual(self.archive.count((99, 10, 99)), 1)

    async def test_topic_ended_stays_silent(self):
        self.llm.plan_participation.return_value.update(ended=True, active=False)
        state = GroupState(active=True)
        await self.controller.respond(self.bot, self.event, "先聊到这里", state, False)
        self.assertFalse(state.active)
        self.llm.reply.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_zero_disables_passive_but_explicit_still_works(self):
        self.settings.set_activity(10, 0)
        await self.controller.receive(self.bot, self.event, "hello", False)
        self.llm.plan_participation.assert_not_awaited()
        await self.controller.receive(
            self.bot, SimpleNamespace(group_id=10, user_id=2, message_id=13), "hello", True
        )
        self.llm.reply.assert_awaited_once()

    async def test_explicit_survives_planner_failure(self):
        self.llm.plan_participation.side_effect = TimeoutError
        await self.controller.respond(self.bot, self.event, "你好", GroupState(), True)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_passive_failure_does_not_spam_group(self):
        self.llm.reply.side_effect = TimeoutError
        await self.controller.respond(self.bot, self.event, "你好", GroupState(), False)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_no_self_loop_and_burst_coalescing(self):
        await self.controller.receive(
            self.bot, SimpleNamespace(group_id=10, user_id=99, message_id=90), "自己消息", False
        )
        self.assertEqual(self.controller.states, {})
        await self.controller.receive(self.bot, self.event, "第一条", False)
        event2 = SimpleNamespace(group_id=10, user_id=3, message_id=13)
        await self.controller.receive(self.bot, event2, "第二条", False)
        state = self.controller.states[(99, 10)]
        self.assertEqual(state.pending[0].message_id, 13)
        self.assertEqual(len(self.controller.states), 1)
        await self.controller.close()

    async def test_settings_permissions_persistence_and_isolation(self):
        commands = self.service.commands
        denied = await commands.run((99, 10, 2), "/积极性 90", None)
        self.assertIn("权限不足", denied)
        await commands.run((99, 10, 1), "/积极性 90", None)
        self.assertEqual(GroupSettings(self.settings.path).get(10)["activity"], 90)
        self.assertEqual(self.settings.get(20)["activity"], 50)
        before = self.settings.path.read_text()
        await commands.run((99, 10, 1), "/积极性 101", None)
        self.assertEqual(before, self.settings.path.read_text())
        data = json.loads(before)
        data["groups"]["10"]["interests"] = ["天文"]
        self.settings.write(data)
        await commands.run((99, 10, 1), "/积极性 60", None)
        self.assertEqual(self.settings.get(10)["interests"], ["天文"])

    async def test_new_group_chat_and_live_role_permissions(self):
        self.event.group_id = 30
        await self.controller.receive(self.bot, self.event, "你好", True)
        self.bot.send_group_msg.assert_awaited()
        self.assertEqual(self.bot.send_group_msg.call_args.kwargs["group_id"], 30)
        self.bot.get_group_member_info = AsyncMock(
            return_value={
                "group_id": 30,
                "user_id": 2,
                "role": "admin",
            }
        )
        self.event.message_id = 13
        await self.controller.receive(self.bot, self.event, "/积极性 90", True)
        self.assertEqual(self.settings.get(30)["activity"], 90)
        self.bot.get_group_member_info.assert_awaited_once_with(
            group_id=30, user_id=2, no_cache=True
        )
        self.bot.get_group_member_info.return_value["role"] = "member"
        self.event.message_id = 14
        await self.controller.receive(self.bot, self.event, "/积极性 20", True)
        self.assertEqual(self.settings.get(30)["activity"], 90)
        self.bot.get_group_member_info.return_value.update(user_id=3, role="owner")
        self.event.message_id = 15
        await self.controller.receive(self.bot, self.event, "/积极性 20", True)
        self.assertEqual(self.settings.get(30)["activity"], 90)

    async def test_low_activity_rejects_low_relevance(self):
        self.settings.set_activity(10, 10)
        await self.controller.respond(self.bot, self.event, "你好", GroupState(), False)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_new_message_during_generation_suppresses_stale_reply(self):
        state = GroupState()

        async def newer(*args):
            state.pending = (self.event, "新消息", time.monotonic())
            return Reply("旧回复")

        self.llm.reply.side_effect = newer
        await self.controller.respond(self.bot, self.event, "你好", state, False)
        self.bot.send_group_msg.assert_not_awaited()
        self.assertFalse(self.archive.seen(99, 10, 12))

    async def test_worker_batches_to_latest_event(self):
        proceed = asyncio.Event()
        entered = asyncio.Event()

        async def pause(_):
            entered.set()
            await proceed.wait()

        self.controller.respond = AsyncMock()
        with patch("plugins.assistant.groupchat.asyncio.sleep", side_effect=pause):
            await self.controller.receive(self.bot, self.event, "第一句", False)
            await entered.wait()
            latest = SimpleNamespace(group_id=10, user_id=3, message_id=13)
            await self.controller.receive(self.bot, latest, "第二句", False)
            proceed.set()
            await self.controller.states[(99, 10)].task
        self.controller.respond.assert_awaited_once()
        self.assertEqual(self.controller.respond.call_args.args[1].message_id, 13)

    async def test_quote_metadata_available_without_at(self):
        from nonebot.adapters.onebot.v11 import Message

        self.event.reply = SimpleNamespace(
            message_id=9, sender=SimpleNamespace(user_id=99), message=Message("可以降低快门速度")
        )
        await self.controller.respond(self.bot, self.event, "为什么呢", GroupState(), False)
        context = self.llm.plan_participation.call_args.args[0]
        self.assertEqual(context[-1]["quoted_message"]["qq"], 99)
        self.assertEqual(context[-1]["quoted_message"]["text"], "可以降低快门速度")

    async def test_image_only_reply_uses_real_image_segment(self):
        from plugins.assistant.emotes import Emotes

        self.service.emotes = Emotes(Path(self.temp.name) / "emotes")
        (self.service.emotes.root / "开心.gif").write_bytes(b"GIF89a" + bytes(20))
        self.llm.reply.return_value = Reply("", emote_id="开心.gif")
        await self.controller.respond(self.bot, self.event, "发个表情", GroupState(), True)
        msg = self.bot.send_group_msg.call_args.kwargs["message"]
        self.assertEqual(msg[0].type, "reply")
        self.assertEqual(msg[1].type, "image")
        self.assertTrue(msg[1].data["file"].startswith("base64://"))
        row = self.archive.db.execute("SELECT content,types FROM messages WHERE usr=99").fetchone()
        self.assertIn("开心.gif", row[0])
        self.assertEqual(json.loads(row[1]), ["image"])

    async def test_vision_reaches_planner_and_answer(self):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment

        self.event.original_message = Message([MessageSegment.image("https://gchat.qpic.cn/a")])
        self.service.vision = SimpleNamespace(
            prepare=AsyncMock(
                return_value=[
                    {"type": "image_url", "image_url": {"url": "https://gchat.qpic.cn/a"}}
                ]
            )
        )
        await self.controller.respond(self.bot, self.event, "看图", GroupState(), True)
        images = self.service.vision.prepare.return_value
        self.assertEqual(self.llm.plan_participation.call_args.kwargs["images"], images)
        self.assertEqual(self.llm.reply.call_args.kwargs["images"], images)

    async def test_coalesced_image_followup_preserves_original_author(self):
        self.service.vision = SimpleNamespace(
            prepare=AsyncMock(
                return_value=[
                    {"type": "image_url", "image_url": {"url": "https://gchat.qpic.cn/a"}}
                ]
            )
        )
        from nonebot.adapters.onebot.v11 import Message, MessageSegment

        source = SimpleNamespace(
            user_id=2,
            message_id=11,
            original_message=Message([MessageSegment.image("https://gchat.qpic.cn/a")]),
        )
        state = GroupState(image_pending=(source, "[图片]", time.monotonic()))
        await self.controller.respond(self.bot, self.event, "这是什么", state, True)
        self.assertIs(self.service.vision.prepare.call_args.args[1], source)
        self.assertIsNone(state.image_pending)
