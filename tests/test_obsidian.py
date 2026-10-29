"""Obsidian 同库导航、隔离编辑稿、确认保存与并发冲突验证。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from admin.console_settings import Settings
from admin.console_store import ConsoleStore, atomic_json
from admin_server import create_app
from plugins.assistant.config import Config
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory_activity import emit
from plugins.assistant.obsidian import Workbench, vault_registration
from plugins.assistant.vault import Vault


class ObsidianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(self.archive.close)
        self.archive.collect((99, 10, 20), 1, "测试部署", ["text"], 100)
        (self.root / "personas").mkdir()
        (self.root / "personas/默认.txt").write_text("测试人设", encoding="utf-8")
        atomic_json(self.root / "group_chat.json", {"groups": {}})
        self.store = ConsoleStore(self.root)
        self.vault = Vault(self.root, 99)
        self.note = self.vault.set_field(20, "偏好", "喜欢紫色纸飞机", manual=True)
        self.workbench = Workbench(self.root, 99)
        self.app = create_app(self.root)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.addCleanup(self.client.close)
        self.headers = {"X-Console-Token": self.app.state.token}

    def draft_path(self):
        return self.vault.path / "工作台" / (self.note["id"] + ".md")

    def test_first_open_routes_to_setup_then_registered_vault(self):
        with (
            patch.dict("os.environ", {"APPDATA": str(self.root / "client")}),
            patch("plugins.assistant.obsidian.sys.platform", "win32"),
        ):
            response = self.client.post(
                "/api/obsidian", json={"bot": 99, "action": "open"}, headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["uri"], "obsidian://choose-vault")
            self.assertEqual(response.json()["registration"], "unregistered")
            self.assertTrue(response.json()["prepared"])
            config = self.root / "client/obsidian/obsidian.json"
            config.parent.mkdir(parents=True)
            original = json.dumps({"vaults": {"unique-id": {"path": str(self.vault.path)}}})
            config.write_text(original, encoding="utf-8")
            result = self.workbench.status()
            self.assertEqual(result["registration"], "registered")
            params = parse_qs(urlparse(result["uri"]).query)
            self.assertEqual(params, {"vault": ["unique-id"], "file": ["导航/记忆库.md"]})
            draft = self.workbench.draft(self.note["id"])
            self.assertEqual(parse_qs(urlparse(draft["uri"]).query)["vault"], ["unique-id"])
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_launch_uses_system_handler_and_reports_failure(self):
        with (
            patch("plugins.assistant.obsidian.sys.platform", "win32"),
            patch(
                "plugins.assistant.obsidian.vault_registration",
                return_value={"state": "unregistered", "id": None},
            ),
            patch("plugins.assistant.obsidian.os.startfile", create=True) as start,
        ):
            response = self.client.post(
                "/api/obsidian",
                json={"bot": 99, "action": "launch", "uri": "https://example.com"},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with("obsidian://choose-vault")
            self.assertIn("launch_message", response.json())
            start.side_effect = OSError("客户端不可用")
            with self.assertRaisesRegex(ValueError, "无法启动 Obsidian"):
                self.workbench.launch()

    def test_launch_registered_draft_and_requires_auth(self):
        with (
            patch("plugins.assistant.obsidian.sys.platform", "win32"),
            patch(
                "plugins.assistant.obsidian.vault_registration",
                return_value={"state": "registered", "id": "test-vault"},
            ),
            patch("plugins.assistant.obsidian.os.startfile", create=True) as start,
        ):
            denied = self.client.post("/api/obsidian", json={"bot": 99, "action": "launch"})
            self.assertEqual(denied.status_code, 403)
            start.assert_not_called()
            self.workbench.launch(self.note["id"])
            params = parse_qs(urlparse(start.call_args.args[0]).query)
            self.assertEqual(params["vault"], ["test-vault"])
            self.assertEqual(params["file"], ["工作台/" + self.note["id"] + ".md"])

    def test_other_or_corrupt_vault_registration_does_not_create_broken_open_link(self):
        with (
            patch.dict("os.environ", {"APPDATA": str(self.root / "client")}),
            patch("plugins.assistant.obsidian.sys.platform", "win32"),
        ):
            config = self.root / "client/obsidian/obsidian.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"vaults": {"other": {"path": str(self.root)}}}), encoding="utf-8"
            )
            self.assertEqual(vault_registration(self.vault.path)["state"], "unregistered")
            config.write_text("broken", encoding="utf-8")
            self.assertEqual(vault_registration(self.vault.path)["state"], "unknown")
            self.assertEqual(self.workbench.uri(), "obsidian://choose-vault")

    def test_navigation_uses_same_vault_without_changing_notes(self):
        raw = self.vault.target(self.note["id"]).read_bytes()
        (self.vault.path / ".gitignore").write_text("用户目录/\n", encoding="utf-8")
        result = self.workbench.prepare()
        self.assertEqual(result["vault"], str(self.vault.path))
        self.assertEqual(self.vault.target(self.note["id"]).read_bytes(), raw)
        ignore = (self.vault.path / ".gitignore").read_text(encoding="utf-8")
        for part in ("用户目录/", "工作台/", "导航/", "图谱/"):
            self.assertIn(part, ignore)
        graph = list((self.vault.path / "图谱").glob("*.md"))
        self.assertTrue(graph)
        self.assertIn(self.note["id"], graph[0].read_text(encoding="utf-8"))
        self.assertEqual(len(self.vault.list()), 1)

    def test_graph_refresh_removes_only_unchanged_generated_stale_pages(self):
        self.workbench.prepare()
        pages = list((self.vault.path / "图谱").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.vault.put({**self.note, "state": "archived"}, self.note["revision"], manual=True)
        self.workbench.prepare()
        self.assertFalse(pages[0].exists())
        self.assertEqual(self.vault.get(self.note["id"])["state"], "archived")

    def test_external_draft_requires_confirmation_and_preserves_history(self):
        self.workbench.draft(self.note["id"])
        self.assertIn("工作台/", (self.vault.path / ".gitignore").read_text(encoding="utf-8"))
        self.draft_path().write_text("后来更喜欢蓝色纸飞机", encoding="utf-8")
        self.assertEqual(self.vault.get(self.note["id"])["value"], "喜欢紫色纸飞机")
        self.workbench.prepare()
        self.workbench.draft(self.note["id"])
        self.assertEqual(self.draft_path().read_text(encoding="utf-8"), "后来更喜欢蓝色纸飞机")
        review = self.workbench.review(self.note["id"])
        self.assertTrue(review["changed"])
        payload = {
            "bot": 99,
            "action": "apply",
            "id": self.note["id"],
            "revision": review["revision"],
        }
        self.assertEqual(self.client.post("/api/obsidian", json=payload).status_code, 403)
        self.assertEqual(
            self.client.post("/api/obsidian", json=payload, headers=self.headers).status_code, 400
        )
        response = self.client.post(
            "/api/obsidian", json={**payload, "confirm": "apply_draft"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        current = self.vault.get(self.note["id"])
        self.assertTrue(current["fixed"])
        self.assertEqual(current["value"], "后来更喜欢蓝色纸飞机")
        self.assertEqual(current["history"][-1]["value"], self.note["value"])
        self.assertFalse(self.workbench.review(self.note["id"])["changed"])

    def test_source_or_draft_changes_reject_stale_confirmation(self):
        self.workbench.draft(self.note["id"])
        self.draft_path().write_text("草稿一", encoding="utf-8")
        review = self.workbench.review(self.note["id"])
        self.draft_path().write_text("草稿二", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.workbench.apply(self.store, self.note["id"], review["revision"])
        self.vault.put({**self.note, "value": "后台的新版本"}, self.note["revision"], manual=True)
        latest = self.workbench.review(self.note["id"])
        self.assertTrue(latest["conflict"])
        with self.assertRaises(ValueError):
            self.workbench.apply(self.store, self.note["id"], latest["revision"])
        self.assertEqual(self.draft_path().read_text(encoding="utf-8"), "草稿二")

    def test_library_hides_protection_and_keeps_evidence_detail(self):
        self.vault.set_field(20, "清空字段", "", manual=True)
        data = self.client.get("/api/memory-library").json()
        self.assertEqual(len(data["groups"]), 1)
        group = data["groups"][0]
        self.assertNotIn("notes", group)
        detail = self.client.get(
            "/api/memory-library/detail",
            params={
                "bot": group["bot"],
                "key": group["key"],
            },
        ).json()
        self.assertEqual(detail["notes"][0]["id"], self.note["id"])
        self.assertNotIn("清空字段", json.dumps(data, ensure_ascii=False))
        maintenance = self.client.get("/api/memory-library?maintenance=true").json()
        self.assertTrue(any(g["category"] == "维护" for g in maintenance["groups"]))

    def test_recall_and_activity_are_explainable_without_model_calls(self):
        emit(self.root, 99, "memory_changed", notes=[self.note["id"]], action="updated")
        activities = self.client.get("/api/memory-activities").json()["items"]
        detail = self.client.get(
            "/api/memory-activities/" + activities[0]["id"], params={"bot": 99}
        ).json()
        self.assertFalse(detail["changes"][0]["historical"])
        self.assertIsNone(detail["changes"][0]["after"])
        result = self.client.post(
            "/api/memory-recall",
            headers=self.headers,
            json={"bot": 99, "query": "纸飞机", "subject": 20},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["selected"][0]["id"], self.note["id"])

    def test_interests_are_global_and_group_has_only_participation(self):
        settings = Settings(self.root)
        before = settings.defaults()
        settings.save_defaults("默认", 2, before["revision"], ["游戏", "音乐"])
        from plugins.assistant.groupchat import GroupSettings

        groups = GroupSettings(self.root / "group_chat.json")
        self.assertEqual(groups.get(10)["interests"], ["游戏", "音乐"])
        self.assertEqual(groups.get(11)["interests"], ["游戏", "音乐"])
        self.assertEqual(set(self.store.get("group:99:10")["value"]), {"min_participants"})

    def test_invalid_identifiers_and_active_migration_cannot_publish(self):
        with self.assertRaises(ValueError):
            self.workbench.draft("../outside")
        atomic_json(self.root / "data/migration-pending.json", {})
        with self.assertRaises(ValueError):
            self.workbench.prepare()


if __name__ == "__main__":
    unittest.main()
