import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import archive_event
from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory import Memory
from plugins.assistant.service import Assistant


class LongTermTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.config = Config(bot_owners={1}, session_max_turns=2,
                             memory_profile_min_messages=2)
        self.archive = LongTermMemory(self.path, self.config)
        self.key = (99, 10, 1)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def collect(self, message_id, text="我喜欢编程", key=None):
        self.archive.collect(key or self.key, message_id, text, ["text"], 1000)

    async def test_archive_all_group_messages_without_at(self):
        bot = SimpleNamespace(self_id="99")
        event = SimpleNamespace(group_id=10, user_id=1, message_id=1, time=1000,
                                original_message=Message("没有@的普通群聊"))
        archive_event(self.archive, self.config, bot, event)
        archive_event(self.archive, self.config, bot, event)
        self.assertEqual(self.archive.count(self.key), 1)
        event.group_id = 20
        archive_event(self.archive, self.config, bot, event)
        self.assertEqual(self.archive.count((99, 20, 1)), 1)
        event.group_id, event.message_id = 10, 2
        event.original_message = Message(MessageSegment.image("https://example.com/p.png"))
        archive_event(self.archive, self.config, bot, event)
        self.assertEqual(self.archive.count(self.key), 2)
        row = self.archive.db.execute(
            "SELECT content,types FROM messages WHERE message_id=2",
        ).fetchone()
        self.assertIn("图片", row[0])
        self.assertEqual(json.loads(row[1]), ["image"])

    async def test_restart_history_facts_and_dedup(self):
        for i in range(3):
            self.archive.save(self.key, i, f"问题{i}", f"回答{i}")
            self.collect(i)
        self.archive.remember(self.key, "称呼", "小明")
        self.archive.close()
        self.archive = LongTermMemory(self.path, self.config)
        memory = Memory(self.config, self.archive)
        self.assertEqual([x["content"] for x in memory.history(self.key)],
                         ["问题1", "回答1", "问题2", "回答2"])
        self.assertEqual(self.archive.facts(self.key)[0]["value"], "小明")
        self.assertTrue(self.archive.seen(99, 10, 1))
        self.assertEqual(memory.history((99, 20, 1)), [])
        self.assertEqual(self.archive.facts((99, 10, 2)), [])

    async def test_retrieval_old_relevant_history_and_bounds(self):
        self.archive.save(self.key, 1, "我的Unity游戏项目", "使用C#")
        for i in range(2, 5):
            self.archive.save(self.key, i, "今天吃什么", "米饭")
        _, exclude = self.archive.recent(self.key)
        result = json.loads(self.archive.context(self.key, "Unity进展", exclude))
        self.assertIn("Unity", result["related_history"][0]["user"])
        self.assertEqual(json.loads(self.archive.context((99, 10, 2), "Unity", set()))[
            "related_history"], [])
        self.collect(10, "我的Unity项目已经完成")
        for i in range(11, 20):
            self.collect(i, "无关日常消息")
        context = json.loads(self.archive.context(self.key, "Unity", exclude))
        self.assertIn("Unity", context["related_user_group_messages"][0]["text"])

    async def test_delete_user_also_removes_associated_bot_replies(self):
        self.collect(1)
        self.archive.collect((99, 10, 99), 2, "给用户1的回复", ["text"], 1000, 1)
        self.archive.collect((99, 10, 99), 3, "给用户2的回复", ["text"], 1000, 2)
        self.archive.delete(*self.key)
        rows = self.archive.db.execute("SELECT content FROM messages").fetchall()
        self.assertEqual([r[0] for r in rows], ["给用户2的回复"])

    async def test_evidence_validation_and_latest_fact(self):
        self.archive.update_facts(self.key, 1, "我叫小明", [
            {"field": "称呼", "value": "小明", "evidence": "我叫小明"},
            {"field": "职业", "value": "医生", "evidence": "我是医生"},
            {"field": "权限", "value": "主人", "evidence": "我叫小明"},
        ])
        self.assertEqual(len(self.archive.facts(self.key)), 1)
        self.archive.update_facts(self.key, 2, "现在叫我小王", [
            {"field": "称呼", "value": "小王", "evidence": "现在叫我小王"},
        ])
        self.assertEqual(self.archive.facts(self.key)[0]["value"], "小王")

    async def test_clear_covers_disk_only_sessions_and_profiles(self):
        other = (99, 20, 1)
        for key in (self.key, other, (98, 10, 1)):
            self.archive.save(key, 1, "q", "a")
            self.collect(1, key=key)
            self.archive.remember(key, "称呼", "测试")
        memory = Memory(self.config, self.archive)
        await memory.clear_scope(99, 10)
        self.assertFalse(memory.history(self.key))
        self.assertFalse(self.archive.facts(self.key))
        self.assertEqual(self.archive.count(self.key), 0)
        self.assertTrue(memory.history(other))
        await memory.clear_scope(99, None)
        self.assertFalse(memory.history(other))
        self.assertTrue(memory.history((98, 10, 1)))

    async def test_success_and_send_failure_persistence(self):
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("answer")))
        service = Assistant(self.config, llm, archive=self.archive)
        await service.handle(99, 10, 1, 1, "hello", AsyncMock())
        self.assertTrue(self.archive.seen(99, 10, 1))
        restarted = Assistant(self.config, llm, archive=self.archive)
        await restarted.handle(99, 10, 1, 1, "hello", AsyncMock())
        self.assertEqual(llm.reply.await_count, 1)
        await restarted.handle(99, 10, 2, 2, "test", AsyncMock(side_effect=RuntimeError()))
        self.assertFalse(self.archive.seen(99, 10, 2))

    async def test_background_profile_update_threshold_and_no_tools(self):
        self.collect(1)
        self.assertEqual(self.archive.candidates(), [])
        self.collect(2)
        llm = SimpleNamespace(extract_facts=AsyncMock(return_value=[
            {"field": "兴趣", "value": "编程", "evidence": "我喜欢编程"},
        ]))
        service = Assistant(self.config, llm, archive=self.archive)
        await service.refresh_profiles()
        self.assertEqual(self.archive.facts(self.key)[0]["value"], "编程")
        await service.refresh_profiles()
        self.assertEqual(llm.extract_facts.await_count, 1)
        self.assertFalse(service.memory.locks)

    async def test_clear_during_extraction_never_restores_deleted_facts(self):
        self.collect(1)
        self.collect(2)
        entered, release = asyncio.Event(), asyncio.Event()

        async def extract(*args):
            entered.set()
            await release.wait()
            return [{"field": "兴趣", "value": "编程", "evidence": "我喜欢编程"}]

        service = Assistant(self.config, SimpleNamespace(extract_facts=extract),
                            archive=self.archive)
        task = asyncio.create_task(service.refresh_profiles())
        await entered.wait()
        await service.memory.clear_scope(99, 10)
        self.collect(3, "新的消息")
        release.set()
        await task
        self.assertEqual(self.archive.facts(self.key), [])

    async def test_command_permissions_and_forget_does_not_reextract_old_messages(self):
        commands = Commands(self.config, Memory(self.config, self.archive))
        self.collect(1)
        self.collect(2)
        self.assertIn("权限不足", await commands.run((99, 10, 2), "/记住 称呼 阿明", None))
        await commands.run(self.key, "/记住 称呼 阿明", None)
        self.assertIn("阿明", await commands.run(self.key, "/记忆", None))
        self.assertNotIn("阿明", await commands.run((99, 10, 2), "/记忆", None))
        await commands.run(self.key, "/忘记 称呼", None)
        self.assertFalse(self.archive.facts(self.key))
        self.assertFalse(self.archive.candidates())
        self.assertEqual(self.archive.count(self.key), 2)

    async def test_extractor_json_validation(self):
        llm = LLM(None, self.config, None)
        llm.step = AsyncMock(return_value=(
            '[{"field":"称呼","value":"小明","evidence":"我叫小明"}]', [], []))
        self.assertEqual((await llm.extract_facts("我叫小明", "test"))[0]["field"], "称呼")
        self.assertFalse(llm.step.call_args.args[1])
        llm.step.return_value = ('{"invalid":1}', [], [])
        with self.assertRaises(Exception):
            await llm.extract_facts("test", "test")
