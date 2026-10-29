"""统一记忆后台的资源、确认保存、版本冲突与来源范围测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from admin.console_store import ConsoleStore, atomic_json
from admin_server import create_app
from plugins.assistant.config import Config
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.vault import FIELDS, Vault, source


class UnifiedConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "personas").mkdir()
        (self.root / "personas/默认.txt").write_text("测试人设", encoding="utf-8")
        (self.root / ".env").write_text(
            "DEEPSEEK_API_KEY=test-secret\nDEEPSEEK_MODEL=test\n", encoding="utf-8"
        )
        atomic_json(self.root / "group_chat.json", {"groups": {}})
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(self.archive.close)
        self.archive.collect((99, 10, 20), 1, "群里的测试事实", ["text"], 100)
        self.archive.collect((99, -20, 20), 2, "私聊里的测试事实", ["text"], 101)
        self.store = ConsoleStore(self.root)
        self.vault = Vault(self.root, 99)
        self.app = create_app(self.root)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.addCleanup(self.client.close)
        self.headers = {"X-Console-Token": self.app.state.token}

    def create(self, **values):
        return self.client.post(
            "/api/memories",
            headers=self.headers,
            json={
                "bot": 99,
                "kind": "thought",
                "subject": 99,
                "field": "测试想法",
                "content": "尚未验证的主观看法",
                **values,
            },
        )

    def test_person_is_one_flat_profile_across_private_and_groups(self):
        for row, field in zip(self.archive.db.execute("SELECT * FROM messages"), FIELDS):
            self.vault.set_field(20, field, row["content"], sources=[source(row)])
        current = self.store.get("person:99:20")
        self.assertEqual(set(current["value"]), set(FIELDS))
        self.assertIsInstance(current["value"]["会话印象"], str)
        self.assertIn("私聊里的测试事实", current["value"].values())
        self.assertEqual(len(self.store.inventory()["people"]), 1)
        self.assertEqual(
            {x["scope"] for x in self.store.knowledge_context("person:99:20")}, {10, -20}
        )

    def test_manual_clear_pins_empty_and_rejects_stale_revision(self):
        old = self.vault.set_field(20, "总体认知", "旧内容", manual=True)
        current = self.store.get("person:99:20")
        value = {**current["value"], "总体认知": ""}
        self.store.put(current["resource"], value, current["revision"])
        note = self.vault.field(20, "总体认知")
        self.assertEqual(note["value"], "")
        self.assertTrue(note["fixed"])
        self.assertEqual(note["history"][-1]["value"], old["value"])
        with self.assertRaises(ValueError):
            self.store.put(current["resource"], value, current["revision"])

    def test_memory_creation_edit_and_undo_keep_history(self):
        created = self.create()
        self.assertEqual(created.status_code, 200, created.text)
        current = created.json()
        self.assertTrue(current["value"]["fixed"])
        saved = self.store.put(
            current["resource"],
            {**current["value"], "content": "修改后的想法"},
            current["revision"],
        )
        self.assertEqual(saved["history"][-1]["value"], "尚未验证的主观看法")
        record = next(r for r in self.store.changes() if not r.get("action"))
        self.store.undo(record["id"])
        self.assertEqual(
            self.store.get(current["resource"])["value"]["content"], "尚未验证的主观看法"
        )

    def test_creation_undo_archives_instead_of_deleting(self):
        current = self.create().json()
        record = self.store.changes()[0]
        self.store.undo(record["id"])
        self.assertEqual(self.store.get(current["resource"])["value"]["state"], "archived")

    def test_queue_status_does_not_expose_actions_or_message(self):
        path = self.root / "data/memory-jobs/99/test.json"
        atomic_json(
            path,
            {
                "id": "test",
                "state": "failed",
                "attempts": 2,
                "retry_at": 1234,
                "error": "ValueError",
                "actions": [{"value": "不可返回的正文"}],
            },
        )
        response = self.client.get("/api/memory-status")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("不可返回", response.text)
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(response.json()["counts"]["failed"], 1)

    def test_search_matches_body_and_group_has_no_persona_override(self):
        created = self.create(content="正文中的紫色纸飞机").json()
        matches = self.client.get("/api/memories", params={"query": "紫色纸飞机"}).json()
        self.assertEqual([m["resource"] for m in matches], [created["resource"]])
        group = self.store.get("group:99:10")
        self.assertNotIn("persona", group["value"])
        self.assertEqual(set(group["value"]), {"min_participants"})
        background = self.store.create_memory(99, "context", 0, "10:summary", "共同背景")
        self.assertEqual(self.store.get(background["resource"])["value"]["content"], "共同背景")

    def test_memory_writes_require_token_and_account_match(self):
        self.assertEqual(self.client.post("/api/memories", json={}).status_code, 403)
        self.assertEqual(self.create(bot=100).status_code, 400)
        self.assertEqual(
            self.client.post("/api/memory-upgrade", headers=self.headers, json={}).status_code, 400
        )

    def test_model_suggestion_cannot_write_before_confirmation(self):
        current = self.create().json()
        suggestion = {
            "value": {**current["value"], "content": "模型建议的新内容"},
            "explanation": "待维护者确认",
        }
        with patch.object(LLM, "step", AsyncMock(return_value=(json.dumps(suggestion), [], {}))):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": current["resource"],
                    "revision": current["revision"],
                    "chat": [{"role": "user", "content": "整理表述"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get(current["resource"])["value"], current["value"])

    def test_old_data_requires_explicit_upgrade(self):
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                (99, 10, 20, "总体认知", "旧值", "旧依据", 1, 100),
            )
        self.assertTrue(self.store.inventory()["memory_upgrade_required"])
        response = self.client.get("/api/resource", params={"id": "person:99:20"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("迁移", response.json()["detail"])

    def test_index_status_returns_only_health_metadata(self):
        atomic_json(
            self.root / "data/memory-index/99/status.json",
            {
                "state": "degraded",
                "mode": "text",
                "error": "TimeoutError",
                "updated": 100,
                "unexpected_content": "不能附带正文",
            },
        )
        data = self.client.get("/api/memory-status").json()
        self.assertEqual(data["indexes"][0]["state"], "degraded")
        self.assertNotIn("不能附带正文", json.dumps(data, ensure_ascii=False))

    def test_file_change_between_snapshot_and_write_rejects_update(self):
        created = self.create().json()
        kind, key = self.store.resolve(created["resource"])
        note = self.vault.get(key[1])
        self.vault.put({**note, "value": "外部维护后的版本"}, note["revision"], manual=True)
        with self.assertRaises(ValueError):
            self.store._write(
                created["resource"], {**created["value"], "content": "过期修改"}, created
            )
        self.assertEqual(self.vault.get(key[1])["value"], "外部维护后的版本")
