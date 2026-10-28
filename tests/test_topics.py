"""验证固定参与策略、话题完整记录及后台只读分页，不连接真实 QQ 或模型。"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from admin_server import create_app
from console_topics import TopicReader
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.llm import Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.service import Assistant
from plugins.assistant.topics import TopicError


class TopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(model_trace_enabled=False, deepseek_api_key="private-test-key")
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", self.config)
        self.spec = {}
        self.bot_message = 900

        async def assess(context):
            result = []
            start = context["topics"][0]["start"] if context["topics"] else None
            for m in context["incoming"]:
                chosen = self.spec.get(m["message_id"], {})
                start = chosen.get("topic", start or m["message_id"])
                result.append(
                    {
                        "message_id": m["message_id"],
                        "topic_start": start,
                        "title": f"话题 {start}",
                        "judgement": "新话题" if start == m["message_id"] else "延续已有话题",
                        "response_to_bot": chosen.get("response"),
                        "should_reply": chosen.get("reply", True),
                        "reply_reason": "有适当补充" if chosen.get("reply", True) else "倾听即可",
                    }
                )
            return result

        self.llm = SimpleNamespace(
            reply=AsyncMock(return_value=Reply("拍月亮可以降低曝光")),
            assess_topics=AsyncMock(side_effect=assess),
        )
        self.service = Assistant(self.config, self.llm, archive=self.archive)
        self.contacts = SimpleNamespace(refresh_group=AsyncMock(), for_scope=lambda *a: {})
        self.settings = GroupSettings(self.root / "group_chat.json")
        self.controller = GroupConversation(self.service, self.contacts, self.settings)
        self.state = GroupState()

        async def send(**kwargs):
            self.bot_message += 1
            return {"message_id": self.bot_message}

        self.bot = SimpleNamespace(self_id="99", send_group_msg=AsyncMock(side_effect=send))

    async def asyncTearDown(self):
        await self.controller.close()
        self.archive.close()
        self.temp.cleanup()

    async def message(self, mid, user, text="一起拍月亮", explicit=False, group=10):
        self.service.limits.cooldowns.clear()
        event = SimpleNamespace(message_id=mid, user_id=user, group_id=group, time=time.time())
        await self.controller.respond(self.bot, event, text, self.state, explicit)

    async def test_two_people_then_only_replies_to_bot(self):
        await self.message(1, 2)
        await self.message(2, 2)
        self.llm.reply.assert_not_awaited()
        await self.message(3, 3)
        self.bot.send_group_msg.assert_awaited_once()
        await self.message(4, 4, "我觉得你们刚才聊得对")
        self.bot.send_group_msg.assert_awaited_once()
        self.spec[5] = {"topic": 1, "response": 901}
        await self.message(5, 3, "你说降低曝光，是降多少？")
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        details = TopicReader(self.root).detail(99, 10, 1)
        self.assertEqual(len(details["messages"]), 7)
        self.assertTrue(details["messages"][0]["is_start"])
        last = next(m for m in details["messages"] if m["message_id"] == 5)
        self.assertEqual(last["response_to_bot"], 901)
        self.assertEqual(last["decisions"][0]["outcome"], "已回复")

    async def test_same_people_can_be_reconsidered_after_quorum(self):
        await self.message(1, 2)
        self.spec[2] = {"topic": 1, "reply": False}
        await self.message(2, 3)
        self.llm.reply.assert_not_awaited()
        self.assertEqual(TopicReader(self.root).listing()["items"], [])
        await self.message(3, 2)
        self.bot.send_group_msg.assert_awaited_once()
        await self.message(4, 4)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_live_group_threshold_reaches_model_and_program_gate(self):
        self.settings.write(
            {"default_min_participants": 5, "groups": {"10": {"min_participants": 3}}}
        )
        await self.message(1, 2)
        await self.message(2, 3)
        self.llm.reply.assert_not_awaited()
        self.assertEqual(self.llm.assess_topics.call_args.args[0]["min_participants"], 3)
        await self.message(3, 4)
        self.bot.send_group_msg.assert_awaited_once()
        context = json.loads(json.loads(self.llm.reply.call_args.args[4])["group"])
        self.assertEqual(context["topic_assessment"]["min_participants"], 3)
        self.assertEqual(context["topic_assessment"]["participants"], 3)
        # 修改配置后下一次普通消息直接读取新值，无须重建控制器。
        self.settings.write({"default_min_participants": 1, "groups": {}})
        self.spec[4] = {"topic": 4}
        await self.message(4, 2)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_evaluation_continues_beyond_third(self):
        self.spec.update({mid: {"reply": False} for mid in (2, 3, 4, 5)})
        await self.message(1, 2)
        await self.message(2, 3)
        await self.message(3, 4)
        await self.message(4, 4)
        # 同一批成员也持续评估，模型暂时选择旁听。
        self.llm.reply.assert_not_awaited()
        await self.message(5, 4)
        self.llm.reply.assert_not_awaited()
        # 第四位成员加入仍可重新评估，不止于旧的三人阶段。
        await self.message(6, 5)
        self.bot.send_group_msg.assert_awaited_once()
        recent = self.controller.topics.recent(99, 10)[0]
        self.assertEqual(set(recent["participants"]), {2, 3, 4, 5})
        detail = TopicReader(self.root).detail(99, 10, 1)
        last = next(m for m in detail["messages"] if m["message_id"] == 6)
        self.assertIn("4位群成员", last["decisions"][0]["reason"])

    async def test_ten_plus_blocks_proactive_reply_even_with_more_than_ten_people(self):
        self.settings.write({"default_min_participants": 10, "groups": {}})
        for mid in range(1, 13):
            await self.message(mid, mid + 100)
        self.llm.reply.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()
        decision = self.controller.topics.db.execute(
            "SELECT reason FROM topic_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("10+", decision[0])
        self.assertEqual(TopicReader(self.root).listing()["items"], [])

    async def test_ten_plus_keeps_direct_reply_but_blocks_ordinary_followup(self):
        self.settings.write({"default_min_participants": 10, "groups": {}})
        await self.message(1, 2, explicit=True)
        self.bot.send_group_msg.assert_awaited_once()
        self.spec[2] = {"topic": 1, "response": 901}
        await self.message(2, 3)
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(self.llm.reply.await_count, 1)

    async def test_group_can_override_ten_plus_default(self):
        self.settings.write(
            {"default_min_participants": 10, "groups": {"10": {"min_participants": 1}}}
        )
        await self.message(1, 2)
        await self.message(2, 3, group=20)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_switching_to_ten_plus_during_generation_stops_unsent_reply(self):
        self.settings.write({"default_min_participants": 1, "groups": {}})

        async def disable(*args, **kwargs):
            self.settings.write({"default_min_participants": 10, "groups": {}})
            return Reply("已生成但不应发出的主动回复")

        self.llm.reply.side_effect = disable
        await self.message(1, 2)
        self.llm.reply.assert_awaited_once()
        self.bot.send_group_msg.assert_not_awaited()
        await self.message(2, 2, explicit=True)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_old_evaluation_stage_is_removed_without_losing_chat(self):
        await self.message(1, 2, explicit=True)
        path = self.controller.topics.path
        await self.controller.close()
        with sqlite3.connect(path) as db:
            db.execute("ALTER TABLE topics ADD COLUMN attempted INTEGER DEFAULT 99")
        db.close()
        self.controller = GroupConversation(self.service, self.contacts, self.settings)
        columns = {
            r["name"] for r in self.controller.topics.db.execute("PRAGMA table_info(topics)")
        }
        self.assertNotIn("attempted", columns)
        self.assertEqual(len(TopicReader(self.root).detail(99, 10, 1)["messages"]), 2)

    async def test_interleaved_topics_and_same_user_do_not_share_quorum(self):
        self.spec.update(
            {1: {"topic": 1}, 2: {"topic": 2}, 3: {"topic": 1}, 4: {"topic": 2}, 5: {"topic": 1}}
        )
        await self.message(1, 2)
        await self.message(2, 3, "明天开黑吗")
        await self.message(3, 2)
        self.llm.reply.assert_not_awaited()
        await self.message(4, 4, "我也来开黑")
        self.bot.send_group_msg.assert_awaited_once()
        await self.message(5, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        topics = TopicReader(self.root).listing()["items"]
        self.assertEqual({t["start"] for t in topics}, {1, 2})
        a = TopicReader(self.root).detail(99, 10, 1)["messages"]
        self.assertEqual([m["message_id"] for m in a if not m["is_self"]], [1, 3, 5])

    async def test_direct_mention_and_persistent_followup_guard(self):
        await self.message(1, 2, explicit=True)
        self.bot.send_group_msg.assert_awaited_once()
        await self.controller.close()
        self.controller = GroupConversation(self.service, self.contacts, self.settings)
        await self.message(2, 3)
        self.bot.send_group_msg.assert_awaited_once()
        await self.message(3, 3, explicit=True)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_bad_response_reference_fails_closed(self):
        await self.message(1, 2, explicit=True)
        self.spec[2] = {"topic": 1, "response": 99999}
        await self.message(2, 3)
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(TopicReader(self.root).detail(99, 10, 1)["topic"]["replies"], 1)

    async def test_topic_model_only_receives_platform_message_ids(self):
        await self.message(1001, 2, explicit=True)
        self.spec[1002] = {"topic": 1001, "response": 901}
        await self.message(1002, 3)
        context = self.llm.assess_topics.call_args.args[0]
        for message in context["incoming"] + context["topics"][0]["messages"]:
            self.assertNotIn("id", message)
            self.assertNotIn("source_id", message)
            self.assertIsInstance(message["message_id"], int)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_bad_old_message_does_not_block_valid_followup_or_cursor(self):
        await self.message(1001, 2, explicit=True)
        self.archive.collect((99, 10, 3), 1002, "旧消息完整保留", ["text"], time.time())
        self.spec.update(
            {
                1002: {"topic": 1001, "response": 99999},
                1003: {"topic": 1001, "response": 901},
            }
        )
        with patch("plugins.assistant.groupchat.emit") as emit:
            await self.message(1003, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        invalid = [c.kwargs for c in emit.call_args_list if c.args[1] == "topic_assignment_invalid"]
        self.assertEqual([i["error_code"] for i in invalid], ["unknown_response"] * 2)
        self.assertEqual([i["attempt"] for i in invalid], [1, 2])
        saved = self.controller.topics.db.execute(
            "SELECT payload,judgement FROM topic_messages WHERE message_id=1002"
        ).fetchone()
        self.assertEqual(json.loads(saved["payload"])["text"], "旧消息完整保留")
        self.assertIn("unknown_response", saved["judgement"])
        current = self.archive.db.execute(
            "SELECT id FROM messages WHERE message_id=1003"
        ).fetchone()
        self.assertEqual(self.controller.topics.cursor(99, 10), current[0])
        self.spec[1004] = {"topic": 1001, "response": 901}
        await self.message(1004, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 3)
        self.assertEqual(
            [m["message_id"] for m in self.llm.assess_topics.call_args.args[0]["incoming"]], [1004]
        )

    async def test_invalid_reference_is_repaired_once_for_current_message(self):
        await self.message(1001, 2, explicit=True)
        original = self.llm.assess_topics.side_effect
        archive_id = self.archive.db.execute(
            "SELECT id FROM messages WHERE message_id=901"
        ).fetchone()[0]

        async def assess(context):
            self.spec[1002] = {
                "topic": 1001,
                "response": 901 if context["validation_error"] else archive_id,
            }
            return await original(context)

        self.llm.assess_topics.side_effect = assess
        self.llm.assess_topics.reset_mock()
        await self.message(1002, 3)
        self.assertEqual(self.llm.assess_topics.await_count, 2)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        repair = self.llm.assess_topics.call_args.args[0]
        self.assertEqual(repair["validation_error"], "unknown_response")
        self.assertEqual([m["message_id"] for m in repair["incoming"]], [1002])
        saved = self.controller.topics.db.execute(
            "SELECT response_to FROM topic_messages WHERE message_id=1002"
        ).fetchone()
        self.assertEqual(saved[0], 901)

    async def test_old_message_repair_cannot_see_future_topic_or_bot_reply(self):
        await self.message(1001, 2, explicit=True)
        self.archive.collect((99, 10, 3), 1002, "较早的未分类消息", ["text"], time.time())
        original = self.llm.assess_topics.side_effect
        self.llm.assess_topics.side_effect = ValueError("模拟暂时不可用")
        await self.message(1003, 2, explicit=True)

        async def assess(context):
            if context["validation_error"]:
                self.assertEqual(context["validation_error"], "topic_after_message")
                self.assertEqual([t["start"] for t in context["topics"]], [1001])
                self.assertEqual(
                    {m["message_id"] for t in context["topics"] for m in t["messages"]},
                    {1001, 901},
                )
                self.spec[1002] = {"topic": 1001, "reply": False}
            else:
                self.spec[1002] = {"topic": 1003, "response": 902}
            self.spec[1004] = {"topic": 1003, "response": 902}
            return await original(context)

        self.llm.assess_topics.side_effect = assess
        await self.message(1004, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 3)
        self.assertEqual(
            self.llm.assess_topics.call_args.args[0]["validation_error"], "topic_after_message"
        )

    async def test_missing_or_duplicate_assignment_only_affects_that_message(self):
        await self.message(1001, 2, explicit=True)
        self.archive.collect((99, 10, 3), 1002, "缺失判断", ["text"], time.time())
        self.archive.collect((99, 10, 3), 1003, "重复判断", ["text"], time.time())
        self.spec.update({mid: {"topic": 1001, "response": 901} for mid in (1002, 1003, 1004)})
        original = self.llm.assess_topics.side_effect

        async def assess(context):
            assignments = await original(context)
            return [a for a in assignments if a["message_id"] != 1002] + [
                a for a in assignments if a["message_id"] == 1003
            ]

        self.llm.assess_topics.side_effect = assess
        await self.message(1004, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        self.assertEqual(self.llm.assess_topics.await_count, 4)
        self.assertEqual(
            self.controller.topics.db.execute(
                "SELECT COUNT(*) FROM topic_messages WHERE message_id IN (1002,1003,1004)"
            ).fetchone()[0],
            3,
        )

    async def test_old_backlog_repair_uses_its_historical_context(self):
        past = time.time() - 3600
        with patch("plugins.assistant.groupchat.time.time", return_value=past):
            await self.message(1001, 2, explicit=True)
        log = self.controller.topics
        self.archive.collect((99, 10, 3), 1002, "半小时前尚未分类的消息", ["text"], past + 300)
        original = self.llm.assess_topics.side_effect

        async def assess(context):
            if context["validation_error"]:
                self.assertEqual([t["start"] for t in context["topics"]], [1001])
                self.assertTrue(
                    all(
                        m["time"] < context["incoming"][0]["time"]
                        for t in context["topics"]
                        for m in t["messages"]
                    )
                )
                self.spec[1002] = {"topic": 1001, "response": 901}
            else:
                self.assertEqual(context["topics"], [])
                self.spec[1002] = {"topic": 99999}
            self.spec[1003] = {"topic": 1003, "reply": False}
            return await original(context)

        self.llm.assess_topics.side_effect = assess
        await self.message(1003, 3)
        saved = log.db.execute(
            "SELECT topic,response_to FROM topic_messages WHERE message_id=1002"
        ).fetchone()
        self.assertEqual(tuple(saved), (1001, 901))
        # 只记录旧消息，不补发其回答。
        self.bot.send_group_msg.assert_awaited_once()

    async def test_repair_request_failure_is_safe_and_does_not_block_next_item(self):
        await self.message(1001, 2, explicit=True)
        self.archive.collect((99, 10, 3), 1002, "重判失败的消息", ["text"], time.time())
        original = self.llm.assess_topics.side_effect
        self.spec.update({1002: {"response": 99999}, 1003: {"topic": 1001, "response": 901}})

        async def assess(context):
            if context["validation_error"]:
                raise TimeoutError("不能进入诊断事件的正文")
            return await original(context)

        self.llm.assess_topics.side_effect = assess
        with patch("plugins.assistant.groupchat.emit") as emit:
            await self.message(1003, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        errors = [c.kwargs for c in emit.call_args_list if c.args[1] == "topic_assessment_failed"]
        self.assertEqual(
            errors,
            [
                {
                    "message_id": 1002,
                    "error_type": "TimeoutError",
                    "error_code": "repair_request_failed",
                }
            ],
        )

    async def test_invalid_current_message_stays_silent_even_with_one_person_threshold(self):
        self.settings.write({"default_min_participants": 1, "groups": {}})
        self.spec[1001] = {"topic": 99999}
        await self.message(1001, 2)
        self.llm.reply.assert_not_awaited()
        self.assertEqual(self.llm.assess_topics.await_count, 2)
        await self.message(1002, 2, explicit=True)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_reference_validation_keeps_scope_topic_and_causality(self):
        await self.message(1001, 2, explicit=True)
        # 先归档旧消息，随后才产生另一个话题与机器人回复。
        self.archive.collect((99, 10, 3), 1002, "早于回复", ["text"], time.time())
        self.llm.assess_topics.side_effect = ValueError("暂时失败")
        await self.message(1003, 2, explicit=True)
        await self.message(2001, 2, explicit=True, group=20)
        old = self.archive.message_context(
            self.archive.db.execute("SELECT * FROM messages WHERE message_id=1002").fetchone()
        )
        self.archive.collect((99, 10, 3), 1004, "较晚的消息", ["text"], time.time())
        later = self.archive.message_context(
            self.archive.db.execute("SELECT * FROM messages WHERE message_id=1004").fetchone()
        )
        base = {
            "topic_start": 1001,
            "title": "测试话题",
            "judgement": "测试归属",
            "response_to_bot": None,
            "should_reply": True,
            "reply_reason": "测试依据",
        }
        cases = [
            (later, {"response_to_bot": 99999}, "unknown_response"),
            (later, {"response_to_bot": 903}, "unknown_response"),
            (later, {"response_to_bot": 1001}, "response_not_bot"),
            (later, {"response_to_bot": 902}, "response_other_topic"),
            (old, {"topic_start": 1003}, "topic_after_message"),
            (old, {"topic_start": 99999}, "unknown_topic"),
        ]
        # 同话题中也不能引用尚未发送的机器人消息。
        self.archive.collect((99, 10, 99), 904, "较晚的机器人发言", ["text"], time.time())
        future = self.archive.message_context(
            self.archive.db.execute("SELECT * FROM messages WHERE message_id=904").fetchone()
        )
        with self.controller.topics.db:
            self.controller.topics.add_message(99, 10, 1001, future, "测试机器人发言")
        cases.append((old, {"response_to_bot": 904}, "response_after_message"))
        for message, overrides, code in cases:
            with self.subTest(code=code), self.assertRaises(TopicError) as error:
                self.controller.topics.record(
                    99, 10, message, {**base, "message_id": message["message_id"], **overrides}
                )
            self.assertEqual(error.exception.code, code)

    async def test_platform_second_precision_does_not_reject_real_followup(self):
        await self.message(1, 2, explicit=True)
        self.spec[2] = {"topic": 1, "response": 901}
        # 平台消息时间只有秒，机器人本地发送时间包含小数；按归档顺序校验因果。
        self.archive.collect((99, 10, 3), 2, "你刚才说的是这个吗", ["text"], int(time.time()))
        await self.message(2, 3)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_full_text_and_silent_tail_persist(self):
        original = "完整聊天正文" * 1200
        await self.message(1, 2, original)
        await self.message(2, 3)
        await self.message(3, 4, "这条仅讨论话题，不回应机器人")
        messages = TopicReader(self.root).detail(99, 10, 1)["messages"]
        self.assertEqual(messages[0]["text"], original)
        self.assertFalse(messages[0]["truncated"])
        self.assertEqual(messages[-1]["message_id"], 3)

    async def test_successful_split_is_one_round_failed_send_is_hidden(self):
        self.llm.reply.return_value = Reply("第一段\n<分条>\n第二段")
        await self.message(1, 2, explicit=True)
        detail = TopicReader(self.root).detail(99, 10, 1)
        self.assertEqual(detail["topic"]["replies"], 1)
        self.assertEqual(len(detail["messages"]), 3)
        self.bot.send_group_msg.side_effect = TimeoutError()
        self.spec[2] = {"topic": 2}
        await self.message(2, 3, explicit=True)
        self.assertEqual(len(TopicReader(self.root).listing()["items"]), 1)

    async def test_api_hides_silent_topics_and_isolates_scope(self):
        await self.message(1, 2)
        self.spec[2] = {"topic": 2}
        await self.message(2, 3, explicit=True)
        with TestClient(create_app(self.root), base_url="http://127.0.0.1:8090") as client:
            listing = client.get("/api/topics").json()
            self.assertEqual([t["start"] for t in listing["items"]], [2])
            self.assertEqual(client.get("/api/topics?group=20").json()["items"], [])
            self.assertEqual(
                client.get("/api/topics/messages?bot=99&group=10&start=1").status_code, 400
            )
            self.assertEqual(
                client.get("/api/topics/messages?bot=88&group=10&start=2").status_code, 400
            )
            self.assertEqual(client.get("/api/topics?offset=-1").status_code, 400)

    async def test_log_credentials_are_filtered(self):
        await self.message(1, 2, "private-test-key", explicit=True)
        data = json.dumps(TopicReader(self.root).detail(99, 10, 1), ensure_ascii=False)
        self.assertNotIn("private-test-key", data)

    async def test_detail_pagination_is_complete(self):
        await self.message(1, 2, explicit=True)
        log = self.controller.topics
        with log.db:
            for index in range(201):
                log.add_message(
                    99,
                    10,
                    1,
                    {
                        "message_id": 1000 + index,
                        "sender": 3,
                        "time": time.time() + index,
                        "text": f"后续聊天{index}",
                    },
                    "继续讨论",
                )
        reader = TopicReader(self.root)
        first, second = reader.detail(99, 10, 1), reader.detail(99, 10, 1, 200)
        self.assertTrue(first["more"])
        self.assertFalse(second["more"])
        combined = first["messages"] + second["messages"]
        self.assertEqual(len(combined), 203)
        self.assertEqual(len({m["message_id"] for m in combined}), 203)

    async def test_expired_topics_are_removed_as_a_whole(self):
        await self.message(1, 2, explicit=True)
        with self.controller.topics.db:
            self.controller.topics.db.execute("UPDATE topics SET updated=0")
        self.controller.topics.prune()
        self.assertEqual(TopicReader(self.root).listing()["items"], [])
        for table in ("topics", "topic_messages", "topic_decisions"):
            self.assertEqual(
                self.controller.topics.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0
            )

    async def test_direct_fallback_does_not_lose_unclassified_messages(self):
        # 此前尚未分类的消息不能因直接呼叫兜底而跳过游标。
        self.archive.collect((99, 10, 3), 1, "之前的消息", ["text"], time.time())
        original = self.llm.assess_topics.side_effect
        self.llm.assess_topics.side_effect = ValueError("暂时失败")
        await self.message(2, 2, explicit=True)
        self.llm.assess_topics.side_effect = original
        self.spec.update({1: {"topic": 1}, 3: {"topic": 1}})
        await self.message(3, 4)
        ids = {
            r[0]
            for r in self.controller.topics.db.execute(
                "SELECT message_id FROM topic_messages WHERE usr<>99"
            )
        }
        self.assertEqual(ids, {1, 2, 3})

    async def test_old_parameter_is_removed_from_settings_and_commands(self):
        self.settings.write({"default_activity": 0, "groups": {"10": {"activity": 99}}})
        self.assertNotIn("activity", self.settings.path.read_text())
        result = await self.service.commands.run((99, 10, 2), "/积极性 0", None)
        self.assertIn("未知指令", result)
