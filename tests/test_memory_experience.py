"""记忆阅读视图、统一预算、真实变化记录与全局兴趣验证。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupSettings
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory_activity import recall, recent
from plugins.assistant.memory_records import seed_profile
from plugins.assistant.memory_worker import Worker
from plugins.assistant.personality import ProfileStore
from plugins.assistant.vault import Vault, source


class ExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.vault = Vault(self.root, 99)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def message(self, ident=1, scope=10, user=2, content="我喜欢蓝色纸飞机"):
        self.archive.collect((99, scope, user), ident, content, ["text"], time.time())
        return self.archive.db.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()

    def test_overview_omits_empty_protection_and_deduplicates_reading(self):
        self.vault.set_field(2, "角色", "", manual=True)
        for field in ("兴趣", "偏好"):
            self.vault.set_field(2, field, "喜欢蓝色纸飞机", manual=True)
        groups = self.vault.overview()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "人物")
        self.assertEqual(groups[0]["content"].count("喜欢蓝色纸飞机"), 1)
        self.assertEqual(len(groups[0]["notes"]), 2)
        self.assertTrue(self.vault.field(2, "角色")["fixed"])

    async def test_budget_includes_actual_cross_scope_continuity(self):
        self.message(scope=-2, content="旧的请求：" + "蓝色" * 400)
        for i in range(5):
            self.vault.set_field(2, f"偏好{i}", "蓝色" * 1000, manual=True, kind="fact")
        context = json.loads(await self.archive.context_async((99, 20, 2), "蓝色", set()))
        notes = context["unified_memory"]
        history = context["recent_shared_conversation"]
        self.assertTrue(history)
        self.assertTrue(all(n["historical"] for n in history))
        self.assertLessEqual(
            sum(len(n["value"]) for n in notes) + sum(len(n["text"]) for n in history), 2400
        )
        event = recent(self.root, 99)[0]
        self.assertEqual(event["kind"], "recall")
        self.assertNotIn("旧的请求", json.dumps(event, ensure_ascii=False))

    def test_diagnostic_explains_invalid_and_fixed_empty(self):
        row = self.message()
        note = self.vault.set_field(2, "偏好", "蓝色纸飞机", sources=[source(row)])
        empty = self.vault.set_field(3, "偏好", "", manual=True)
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages")
        report = recall(self.vault, "蓝色", 2)
        reasons = {n["id"]: n["reason"] for n in report["excluded"]}
        self.assertEqual(reasons[note["id"]], "source_invalid")
        self.assertEqual(reasons[empty["id"]], "empty_protection")
        self.assertEqual(report["selected"], [])

    def test_index_cannot_retain_deleted_or_revised_source_text(self):
        row = self.message()
        note = self.vault.set_field(2, "偏好", "蓝色纸飞机", sources=[source(row)])
        Worker.index(self.vault, 2)
        index = self.vault.list(kind="index")[0]
        self.vault.set_field(2, "偏好", "其他偏好", manual=True, expected=note["revision"])
        self.assertFalse(self.vault.valid(index))
        self.assertFalse(self.vault.list(kind="index"))

    def test_global_interests_migrate_once_and_reach_persona(self):
        path = self.root / "group_chat.json"
        path.write_text(
            json.dumps(
                {"groups": {"10": {"interests": ["天文"]}, "20": {"interests": ["天文", "游戏"]}}}
            ),
            encoding="utf-8",
        )
        before = path.read_bytes()
        settings = GroupSettings(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(settings.get(10)["interests"], ["天文", "游戏"])
        self.assertEqual(settings.get(10)["interests"], settings.get(20)["interests"])
        self.assertNotIn("interests", settings.read()["groups"]["10"])
        profiles = ProfileStore()
        profiles.group_settings = settings
        self.assertEqual(profiles.effective()["interests"], "天文、游戏")
        settings.normalize()
        self.assertNotIn("interests", json.loads(path.read_text(encoding="utf-8"))["groups"]["10"])
        self.assertEqual(len(list((self.root / "backups/settings").glob("interests-*.json"))), 1)
        settings.normalize()
        self.assertEqual(len(list((self.root / "backups/settings").glob("interests-*.json"))), 1)

    async def test_real_changes_produce_journal_and_honest_outcome(self):
        row = self.message()
        action = dict(
            action="ADD",
            target="",
            field="偏好",
            kind="fact",
            value="喜欢蓝色纸飞机",
            evidence=row["content"],
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [action]}), [], []))
        )
        await Worker(self.archive, llm, profiles=ProfileStore()).tick()
        self.assertTrue(self.vault.list(kind="self"))
        self.assertTrue(self.vault.list(kind="diary"))
        completed = next(n for n in recent(self.root) if n["kind"] == "processing_completed")
        self.assertEqual(completed["result"], "saved")
        self.assertEqual(completed["model_calls"], 1)
        self.assertEqual(len(completed["notes"]), 1)

    async def test_fixed_skip_is_not_reported_saved(self):
        row = self.message()
        self.vault.set_field(2, "偏好", "", kind="fact", manual=True)
        action = dict(
            action="ADD",
            target="",
            field="偏好",
            kind="fact",
            value="喜欢蓝色纸飞机",
            evidence=row["content"],
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [action]}), [], []))
        )
        await Worker(self.archive, llm).tick()
        event = next(n for n in recent(self.root) if n["kind"] == "processing_completed")
        self.assertEqual(event["result"], "skipped")
        self.assertEqual(event["outcomes"][0]["result"], "fixed")
        self.assertFalse(self.vault.list(kind="diary"))

    def test_profile_seed_does_not_overwrite_learned_or_manual_profile(self):
        seed_profile(self.vault, ProfileStore())
        old = self.vault.field(99, "自我档案", "self")
        self.vault.set_field(
            99, "自我档案", "已调整的自我认识", kind="self", expected=old["revision"]
        )
        seed_profile(self.vault, SimpleNamespace(effective=lambda: {"persona": "新的基础"}))
        self.assertEqual(self.vault.field(99, "自我档案", "self")["value"], "已调整的自我认识")
