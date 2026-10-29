import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.consolidation import Consolidation, fingerprint, group_source, organized
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

    def test_view_preserves_sources_and_scope(self):
        self.add("a")
        self.add("b")
        original = [dict(r) for r in self.facts()]
        self.worker.cache["person:99:10:1:角色"] = {
            "value": "喜欢编程，偏好待确认",
            "fingerprint": fingerprint(original),
        }
        self.worker.save()
        self.assertEqual([dict(r) for r in self.facts()], original)
        self.assertEqual(len(organized(self.facts(), self.root)), 1)
        self.assertEqual(organized(self.facts()[:1], self.root)[0]["value"], "喜欢编程")
        self.add("c")
        self.assertEqual(len(organized(self.facts(), self.root)), 3)

    def test_group_sources_and_manual_override(self):
        from plugins.assistant.consolidation import automatic_group
        from plugins.assistant.vault import Vault
        from plugins.assistant.vault import source as stable_source

        self.archive.collect((99, 10, 1), 1, "周末交流编程", ["text"], 1000)
        source = group_source(self.archive.db, 99, 10)
        self.worker.cache["group:99:10"] = {
            "value": {"summary": "周末交流", "knowledge": "编程"},
            "source": source,
            "fingerprint": fingerprint(source),
        }
        self.worker.save()
        self.assertEqual(automatic_group(self.root, 99, 10)["summary"], "周末交流")
        row = self.archive.db.execute("SELECT * FROM messages").fetchone()
        vault = Vault(self.root, 99)
        note = vault.set_field(
            0, "10:summary", "周末交流", kind="context", sources=[stable_source(row)]
        )
        self.assertEqual(group_knowledge(99, 10, self.root)["summary"], "周末交流")
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE message_id=1")
        self.assertEqual(group_knowledge(99, 10, self.root), {})
        self.assertEqual(automatic_group(self.root, 99, 10), {})
        vault.set_field(0, "10:summary", "", kind="context", manual=True, expected=note["revision"])
        vault.set_field(0, "10:knowledge", "人工固定", kind="context", manual=True)
        self.assertEqual(
            group_knowledge(99, 10, self.root), {"summary": "", "knowledge": "人工固定"}
        )
