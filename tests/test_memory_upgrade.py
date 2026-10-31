"""统一记忆离线升级的关键事务验证。"""

import tempfile
import unittest
from pathlib import Path

from memory_upgrade import required, upgrade
from plugins.assistant.config import Config
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.vault import Vault


class UpgradeTests(unittest.TestCase):
    def test_preview_publish_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config())
            self.assertFalse(required(root))
            with archive.db:
                archive.db.execute(
                    "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                    (99, 10, 2, "总体认知", "", "后台维护者修正", None, 1),
                )
            archive.close()
            self.assertTrue(required(root))
            self.assertEqual(upgrade(root)["state"], "preview")
            self.assertEqual(Vault(root, 99).list(), [])
            self.assertEqual(upgrade(root, apply=True)["state"], "upgraded")
            note = Vault(root, 99).list()[0]
            self.assertTrue(note["fixed"])
            self.assertEqual(note["value"], "")
            self.assertFalse(required(root))
            self.assertEqual(upgrade(root, apply=True)["state"], "already_upgraded")

    def test_shared_sources_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config())
            for group, content in ((10, "喜欢蓝色"), (-2, "喜欢绿色")):
                archive.collect((99, group, 2), group, content, ["text"], 1)
                ident = archive.db.execute(
                    "SELECT id FROM messages WHERE grp=?", (group,)
                ).fetchone()[0]
                with archive.db:
                    archive.db.execute(
                        "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                        (99, group, 2, "总体认知", content, content, ident, 1),
                    )
            archive.close()
            upgrade(root, apply=True)
            note = Vault(root, 99).list()[0]
            self.assertEqual(len(note["sources"]), 2)
            self.assertIn("蓝色", note["value"])
            self.assertIn("绿色", note["value"])
            import sqlite3
            from contextlib import closing

            with closing(sqlite3.connect(root / "data/memory.sqlite3")) as db, db:
                db.execute("DELETE FROM messages WHERE grp=10")
            self.assertEqual(Vault(root, 99).list(), [])

    def test_fixed_conflicts_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config())
            for group, value in ((10, "甲"), (-2, "乙")):
                with archive.db:
                    archive.db.execute(
                        "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                        (99, group, 2, "总体认知", value, "后台维护者修正", None, 1),
                    )
            archive.close()
            self.assertEqual(upgrade(root, apply=True)["quarantine"], 1)
            self.assertEqual(Vault(root, 99).list(), [])
            self.assertEqual(len(Vault(root, 99).list(history=True)), 1)

    def test_failed_publish_restores(self):
        import shutil
        from unittest.mock import patch

        import migration

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config())
            with archive.db:
                archive.db.execute(
                    "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                    (99, 10, 2, "总体认知", "甲", "后台维护者修正", None, 1),
                )
            archive.close()
            original = shutil.copy2
            failed = False

            def copy(src, dest, *args, **kwargs):
                nonlocal failed
                if (root / migration.JOURNAL).exists() and not failed:
                    failed = True
                    raise OSError("模拟发布失败")
                return original(src, dest, *args, **kwargs)

            with patch("memory_upgrade.shutil.copy2", side_effect=copy), self.assertRaises(OSError):
                upgrade(root, apply=True)
            self.assertTrue(required(root))
            self.assertFalse((root / migration.JOURNAL).exists())
            self.assertEqual(Vault(root, 99).list(history=True), [])

    def test_job_validation(self):
        from memory_upgrade import validate_job
        from plugins.assistant.vault import digest

        ref = {"bot": 99, "grp": 10, "usr": 2, "message_id": 3, "created": 1, "hash": "a" * 64}
        job = {"id": digest(ref)[:32], "source": ref, "attempts": 0, "retry_at": 0, "completed": []}
        validate_job(job, 99)
        for changed in (
            {"attempts": -1},
            {"retry_at": float("nan")},
            {"completed": [0]},
            {"id": "a" * 32},
        ):
            with self.assertRaises(ValueError):
                validate_job({**job, **changed}, 99)
        with self.assertRaises(ValueError):
            validate_job(job, 88)

    def test_zip_notes_jobs_preserve_target(self):
        import migration
        from plugins.assistant.vault import digest

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            local, incoming = base / "local", base / "incoming"
            for root in (local, incoming):
                archive = LongTermMemory(root / "data/memory.sqlite3", Config())
                archive.collect((99, 10, 2), 3, "喜欢蓝色", ["text"], 1)
                archive.close()
                migration.write_json(root / "data/identity.json", {"bot_qq": 99})
            local_note = Vault(local, 99).set_field(2, "总体认知", "目标固定", manual=True)
            Vault(incoming, 99).set_field(2, "总体认知", "来源固定", manual=True)
            ref = {
                "bot": 99,
                "grp": 10,
                "usr": 2,
                "message_id": 3,
                "created": 1,
                "hash": digest("喜欢蓝色"),
            }
            ident = digest(ref)[:32]
            name = f"data/memory-jobs/99/{ident}.json"
            job = {
                "id": ident,
                "source": ref,
                "state": "done",
                "attempts": 0,
                "retry_at": 0,
                "completed": [],
            }
            migration.write_json(local / name, job)
            migration.write_json(incoming / name, {**job, "state": "pending"})
            package = base / "source.zip"
            migration.export_data(incoming, package, 99)
            migration.import_data(local, package, 99)
            self.assertEqual(Vault(local, 99).get(local_note["id"])["value"], "目标固定")
            self.assertEqual(len(Vault(local, 99).list(history=True)), 2)
            self.assertEqual(migration.read_json(local / name)["state"], "done")
            migration.import_data(local, package, 99)
            self.assertEqual(len(Vault(local, 99).list(history=True)), 2)
            with self.assertRaises(ValueError):
                migration.import_data(local, package, 88)

    def test_import_legacy_note_without_fixed_preserves_bytes(self):
        import migration

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            incoming, target = base / "incoming", base / "target"
            archive = LongTermMemory(incoming / "data/memory.sqlite3", Config())
            archive.close()
            migration.write_json(incoming / "data/identity.json", {"bot_qq": 99})
            note = Vault(incoming, 99).put({"kind": "diary", "subject": 2, "value": "legacy note"})
            path = Vault(incoming, 99).target(note["id"])
            raw = path.read_bytes().replace(b"fixed: false\r\n", b"")
            self.assertNotEqual(raw, path.read_bytes())
            path.write_bytes(raw)
            package = base / "source.zip"
            migration.export_data(incoming, package, 99)
            migration.import_data(target, package, 99)
            imported = Vault(target, 99).get(note["id"])
            self.assertFalse(imported["fixed"])
            self.assertEqual(imported["value"], "legacy note")
            self.assertEqual(Vault(target, 99).target(note["id"]).read_bytes(), raw)
            path.write_bytes(raw.replace(b"state: active\r\n", b"state: active\r\nfixed: null\r\n"))
            invalid = base / "invalid.zip"
            migration.export_data(incoming, invalid, 99)
            with self.assertRaisesRegex(ValueError, "记忆字段无效"):
                migration.import_data(base / "other", invalid, 99)

    def test_configuration_only_upgrade(self):
        import migration

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            migration.write_json(
                root / "group_chat.json",
                {"default_persona": "默认", "groups": {"10": {"persona": {"name": "旧模板"}}}},
            )
            self.assertTrue(required(root))
            self.assertEqual(upgrade(root)["state"], "preview")
            self.assertFalse((root / migration.MEMORY).exists())
            self.assertEqual(upgrade(root, apply=True)["state"], "upgraded")
            self.assertNotIn(
                "persona", migration.read_json(root / "group_chat.json")["groups"]["10"]
            )

    def test_group_uses_original_sources(self):
        import migration
        from plugins.assistant.consolidation import fingerprint, group_source

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config())
            archive.collect((99, 10, 2), 1, "旧消息", ["text"], 1)
            refs = group_source(archive.db, 99, 10)
            migration.write_json(
                root / "data/knowledge-cleanup.json",
                {
                    "group:99:10": {
                        "source": refs,
                        "fingerprint": fingerprint(refs),
                        "value": {"summary": "原摘要", "knowledge": "原认知"},
                        "history": [{"value": "更旧"}],
                    }
                },
            )
            archive.collect((99, 10, 2), 2, "无关新消息", ["text"], 2)
            archive.close()
            result = upgrade(root, apply=True)
            notes = Vault(root, 99).list(kind="context")
            self.assertEqual(len(notes), 2)
            self.assertEqual([r["message_id"] for r in notes[0]["sources"]], [1])
            self.assertTrue(
                (root / result["legacy_backup"] / "data/knowledge-cleanup.json").exists()
            )

    def test_upgraded_target_imports_old_facts(self):
        import sqlite3
        from contextlib import closing

        import migration

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            local, incoming = base / "local", base / "incoming"
            for root in (local, incoming):
                archive = LongTermMemory(root / "data/memory.sqlite3", Config())
                if root == incoming:
                    with archive.db:
                        archive.db.execute(
                            "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                            (99, 10, 2, "总体认知", "旧包认知", "后台维护者修正", None, 1),
                        )
                archive.close()
                migration.write_json(root / "data/identity.json", {"bot_qq": 99})
            upgrade(local, apply=True)
            original = Vault(local, 99).set_field(2, "总体认知", "目标新版", manual=True)
            package = base / "old.zip"
            migration.export_data(incoming, package, 99)
            migration.import_data(local, package, 99)
            notes = Vault(local, 99).list(history=True)
            self.assertEqual(Vault(local, 99).get(original["id"])["value"], "目标新版")
            self.assertTrue(any(note["value"] == "旧包认知" for note in notes))
            with closing(sqlite3.connect(local / "data/memory.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)
