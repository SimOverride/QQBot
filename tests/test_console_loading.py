"""轻量分页与共享读取缓存的失效边界。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin.console_store import ConsoleStore, atomic_json
from admin_server import create_app
from plugins.assistant.vault import Vault, atomic


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ConsoleStore(self.root)
        self.vault = Vault(self.root, 99)
        self.client = TestClient(create_app(self.root), base_url="http://127.0.0.1:8090")
        self.addCleanup(self.client.close)

    def test_navigation_does_not_read_memory_or_inventory(self):
        self.vault.set_field(20, "偏好", "红色", manual=True)
        with (
            patch.object(Vault, "list", side_effect=AssertionError("不能遍历正文")),
            patch.object(ConsoleStore, "inventory", side_effect=AssertionError("不能读取完整目录")),
        ):
            data = self.client.get("/api/navigation").json()
        self.assertEqual(data["bots"], [99])
        self.assertNotIn("people", data)

    def test_parsed_cache_returns_independent_current_values(self):
        note = self.vault.set_field(20, "偏好", "红色", manual=True)
        first = self.vault.get(note["id"])
        first["sources"].append({"invalid": True})
        self.assertEqual(self.vault.get(note["id"])["sources"], [])
        path = self.vault.target(note["id"])
        raw = path.read_text(encoding="utf-8")
        atomic(path, raw.replace("红色", "蓝色"))
        current = Vault(self.root, 99).get(note["id"])
        self.assertEqual(current["value"], "蓝色")
        self.assertNotEqual(current["revision"], note["revision"])
        with self.assertRaisesRegex(ValueError, "版本冲突"):
            self.vault.put({**note, "value": "黄色"}, note["revision"], manual=True)
        path.unlink()
        self.assertIsNone(self.vault.get(note["id"]))

    def test_library_filters_before_paging_and_omits_history(self):
        for user in range(1, 5):
            self.vault.set_field(user, "偏好", f"颜色{user}", manual=True)
        data = self.client.get("/api/memory-library?limit=2").json()
        self.assertEqual(len(data["groups"]), 2)
        self.assertEqual(data["total"], 4)
        self.assertTrue(data["has_more"])
        self.assertNotIn("notes", data["groups"][0])
        found = self.client.get("/api/memory-library?query=颜色4&limit=1").json()
        self.assertEqual(found["total"], 1)
        group = found["groups"][0]
        detail = self.client.get(
            "/api/memory-library/detail",
            params={
                "bot": 99,
                "key": group["key"],
            },
        ).json()
        self.assertIn("颜色4", detail["content"])
        self.assertNotIn("history", detail["notes"][0])
        self.assertEqual(
            self.client.get(
                "/api/memory-library/detail",
                params={
                    "bot": 98,
                    "key": group["key"],
                },
            ).status_code,
            404,
        )

    def test_audit_reads_page_only_and_detail_is_separate(self):
        for index in range(4):
            ident = f"{index:032x}"
            atomic_json(
                self.root / "backups/admin-edits" / (ident + ".json"),
                {
                    "id": ident,
                    "before": "旧内容",
                    "after": "新内容",
                    "resource": "memory:99:x",
                },
            )
        import migration

        with patch("migration.read_json", wraps=migration.read_json) as read:
            data = self.client.get("/api/changes?limit=1").json()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(data["total"], 4)
        self.assertNotIn("before", data["items"][0])
        detail = self.client.get("/api/changes/" + data["items"][0]["id"]).json()
        self.assertEqual(detail["before"], "旧内容")
