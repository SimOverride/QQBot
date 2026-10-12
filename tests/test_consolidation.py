import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from console_store import ConsoleStore
from plugins.assistant.config import Config
from plugins.assistant.consolidation import Consolidation, organized
from plugins.assistant.knowledge import person_value
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.prompt_store import group_knowledge


class ConsolidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(knowledge_cleanup_enabled=True, trace_enabled=False)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", self.config)
        self.worker = Consolidation(self.archive, self.root, self.config)
        self.model = AsyncMock(return_value={"value": "喜欢编程；语言偏好待确认：Python 或 C#。"})

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def facts(self):
        return self.archive.db.execute("SELECT * FROM facts ORDER BY field").fetchall()

    def add(self, suffix, evidence="我喜欢编程"):
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(?,?,?,?,?,?,NULL,?)",
                (99, 10, 1, "角色:" + suffix, "喜欢编程", evidence, 1000),
            )

    async def test_view_preserves_sources_and_scope(self):
        self.add("a")
        self.add("b")
        original = [dict(r) for r in self.facts()]
        await self.worker.tick(self.model)
        self.assertEqual([dict(r) for r in self.facts()], original)
        self.assertEqual(len(organized(self.facts(), self.root)), 1)
        self.assertEqual(len(organized(self.facts()[:1], self.root)), 1)
        self.assertEqual(organized(self.facts()[:1], self.root)[0]["value"], "喜欢编程")
        value, evidence = person_value(self.archive.db, 99, 1, self.root)
        self.assertIn("待确认", value["会话印象"]["10"]["角色"])
        self.assertEqual(evidence, original)
        await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 1)
        self.add("c")
        await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(len(organized(self.facts(), self.root)), 3)
        with patch("plugins.assistant.consolidation.time.time", return_value=10**11):
            await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 2)

    async def test_fixed_and_inflight_changes(self):
        self.add("a")
        self.add("b")

        async def pin(*args):
            with self.archive.db:
                self.archive.db.execute(
                    "UPDATE facts SET value='',evidence='后台维护者修正' WHERE field='角色:a'"
                )
            return {"value": "不可保存"}

        await self.worker.tick(pin)
        self.assertFalse(self.worker.cache)
        await self.worker.tick(self.model)
        self.model.assert_not_awaited()
        self.assertEqual(organized(self.facts(), self.root)[0]["value"], "")

    async def test_invalid_result_retry_and_switch(self):
        self.add("a")
        self.add("b")
        self.model.return_value = {"wrong": "无效"}
        await self.worker.tick(self.model)
        await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(len(organized(self.facts(), self.root)), 2)
        self.config.knowledge_cleanup_enabled = False
        with patch("plugins.assistant.consolidation.time.time", return_value=10**11):
            await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 1)

    async def test_group_fixed_and_deleted_sources(self):
        self.archive.collect((99, 10, 1), 1, "周末交流编程", ["text"], 1000)
        self.model.return_value = {"summary": "周末交流", "knowledge": "交流编程"}
        await self.worker.tick(self.model)
        self.assertEqual(group_knowledge(99, 10, self.root), self.model.return_value)
        self.assertEqual(group_knowledge(98, 10, self.root), {})
        self.assertEqual(group_knowledge(99, 20, self.root), {})
        self.archive.collect((99, 10, 1), 2, "新消息", ["text"], 1001)
        self.assertEqual(group_knowledge(99, 10, self.root), self.model.return_value)
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE message_id=1")
        self.assertEqual(group_knowledge(99, 10, self.root), {})
        fixed = {"summary": "", "knowledge": "人工固定"}
        (self.root / "data/console.json").write_text(
            json.dumps({"groups": {"99:10": fixed}}), encoding="utf-8"
        )
        self.assertEqual(group_knowledge(99, 10, self.root), fixed)
        with patch("plugins.assistant.consolidation.time.time", return_value=10**11):
            await self.worker.tick(self.model)
        self.assertEqual(self.model.await_count, 1)

    async def test_group_inflight_pin(self):
        self.archive.collect((99, 10, 1), 1, "周末交流", ["text"], 1000)

        async def pin(*args):
            (self.root / "data/console.json").write_text(
                json.dumps({"groups": {"99:10": {"summary": "", "knowledge": ""}}}),
                encoding="utf-8",
            )
            return {"summary": "过期", "knowledge": "过期"}

        await self.worker.tick(pin)
        self.assertFalse(self.worker.cache)
        self.assertEqual(group_knowledge(99, 10, self.root), {"summary": "", "knowledge": ""})

    async def test_new_messages_and_console_edit(self):
        self.archive.collect((99, 10, 1), 1, "周末交流", ["text"], 1000)

        async def summarize(*args):
            self.archive.collect((99, 10, 1), 2, "后来发言", ["text"], 1001)
            return {"summary": "周末交流", "knowledge": "编程"}

        await self.worker.tick(summarize)
        for folder in ("personas",):
            (self.root / folder).mkdir()
            (self.root / folder / "默认.txt").write_text("默认内容", encoding="utf-8")
        store = ConsoleStore(self.root)
        before = store.get("group:99:10")
        self.assertEqual(before["value"]["summary"], "周末交流")
        value = {**before["value"], "activity": 25}
        saved = store.put("group:99:10", value, before["revision"])
        config = json.loads((self.root / "data/console.json").read_text(encoding="utf-8"))
        self.assertNotIn("99:10", config.get("groups", {}))
        value["summary"] = ""
        store.put("group:99:10", value, saved["revision"])
        self.assertEqual(group_knowledge(99, 10, self.root)["summary"], "")
