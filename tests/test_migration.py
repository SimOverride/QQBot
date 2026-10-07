"""使用隔离数据验证迁移的账号边界、引用完整性及故障恢复。"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import migration as m
from plugins.assistant.config import Config
from plugins.assistant.longterm import LongTermMemory


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def project(self, name, qq=123):
        root = self.base / name
        root.mkdir()
        (root / ".env").write_text("ONEBOT_ACCESS_TOKEN=test\n", encoding="utf-8")
        for folder in ("personas", "styles", "emotes"):
            (root / folder).mkdir()
        (root / "personas/默认.txt").write_text("人格", encoding="utf-8")
        (root / "styles/默认.txt").write_text("风格", encoding="utf-8")
        m.write_json(root / "group_chat.json", {"default_activity": 50, "groups": {}})
        db = LongTermMemory(root / m.MEMORY, Config())
        db.collect((qq, 10, 20), 100, "第一条", ["text"], 1000)
        db.close()
        return root

    def test_live_wal_export_and_new_import(self):
        source = self.project("source")
        db = LongTermMemory(source / m.MEMORY, Config())
        self.addCleanup(db.close)
        db.collect((123, 10, 20), 101, "WAL最新记录", ["text"], 1001)
        output = self.base / "export.zip"
        m.export_data(source, output)
        target = self.base / "target"
        target.mkdir()
        m.import_data(target, output, 123)
        with closing(sqlite3.connect(target / m.MEMORY)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertIn("ONEBOT_ACCESS_TOKEN=test", (target / ".env").read_text(encoding="utf-8"))

    def test_merge_references_cursor_and_repeat(self):
        source = self.project("source")
        target = self.project("target")
        db = LongTermMemory(source / m.MEMORY, Config())
        db.collect((123, 10, 20), 101, "来源独有", ["text"], 1001)
        with db.db:
            db.db.execute("INSERT INTO profile_cursor VALUES(123,10,20,2)")
            db.db.execute("INSERT INTO facts VALUES(123,10,20,'兴趣','游戏','来源独有',2,100)")
            db.db.execute("INSERT INTO disclosure_reviews VALUES(2,1)")
            db.db.execute("INSERT INTO share_attempts VALUES(2,123,10,20,100,'sent')")
        db.close()
        db = LongTermMemory(target / m.MEMORY, Config())
        db.collect((123, 10, 20), 102, "本地待处理", ["text"], 999)
        db.close()
        output = self.base / "export.zip"
        m.export_data(source, output)
        for _ in range(2):
            m.import_data(target, output)
            with closing(sqlite3.connect(target / m.MEMORY)) as conn, conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
                self.assertEqual(
                    conn.execute(
                        "SELECT m.message_id FROM facts f JOIN messages m ON f.source_id=m.id"
                    ).fetchone()[0],
                    101,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT m.message_id FROM messages m JOIN profile_cursor c "
                        "ON m.bot=c.bot AND m.grp=c.grp AND m.usr=c.usr WHERE m.id>c.last_id"
                    ).fetchall(),
                    [(102,)],
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT m.message_id FROM share_attempts s "
                        "JOIN messages m ON s.source_id=m.id"
                    ).fetchone()[0],
                    101,
                )

    def test_different_account_rejected_without_changes(self):
        source, target = self.project("source"), self.project("target", 456)
        output = self.base / "export.zip"
        m.export_data(source, output)
        previous = (target / m.MEMORY).read_bytes()
        with self.assertRaisesRegex(ValueError, "不同"):
            m.import_data(target, output)
        self.assertEqual(previous, (target / m.MEMORY).read_bytes())
        self.assertFalse((target / "backups").exists())

    def test_tamper_and_traversal_rejected(self):
        source = self.project("source")
        output = self.base / "export.zip"
        m.export_data(source, output)
        bad = self.base / "bad.zip"
        with ZipFile(output) as src, ZipFile(bad, "w") as dst:
            for name in src.namelist():
                dst.writestr(name, b"changed" if name == ".env" else src.read(name))
        with self.assertRaisesRegex(ValueError, "校验失败"):
            m.unpack(bad, self.base / "unpacked")
        with ZipFile(bad, "w") as dst:
            dst.writestr("../outside", "x")
        with self.assertRaises(ValueError):
            m.unpack(bad, self.base / "unpacked2")

    def test_lock_and_interrupted_recovery(self):
        root = self.project("target")
        with m.project_lock(root):
            with self.assertRaises(ValueError):
                with m.project_lock(root, runtime=True):
                    pass
        backup = root / "backups/rollback"
        backup.mkdir(parents=True)
        (backup / ".env").write_text("old", encoding="utf-8")
        (root / ".env").write_text("partial", encoding="utf-8")
        (root / "styles/new.txt").write_text("new", encoding="utf-8")
        m.write_json(
            root / m.JOURNAL, {"rollback": "backups/rollback", "paths": [".env", "styles/new.txt"]}
        )
        with self.assertRaises(ValueError):
            with m.project_lock(root, runtime=True):
                pass
        m.recover(root)
        self.assertEqual((root / ".env").read_text(encoding="utf-8"), "old")
        self.assertFalse((root / "styles/new.txt").exists())

    def test_failure_rolls_back(self):
        source, target = self.project("source"), self.project("target")
        output = self.base / "export.zip"
        m.export_data(source, output)
        old = (target / m.MEMORY).read_bytes()
        original = m.shutil.copy2
        failed = False

        def fail_once(src, dest, *args, **kwargs):
            nonlocal failed
            if Path(dest) == target / m.MEMORY and not failed:
                failed = True
                raise OSError("模拟发布失败")
            return original(src, dest, *args, **kwargs)

        with patch.object(m.shutil, "copy2", side_effect=fail_once):
            with self.assertRaises(OSError):
                m.import_data(target, output)
        self.assertEqual(old, (target / m.MEMORY).read_bytes())
        self.assertFalse((target / m.JOURNAL).exists())

    def test_configuration_and_emote_conflicts(self):
        source, target = self.project("source"), self.project("target")
        (source / ".env").write_text("ONEBOT_ACCESS_TOKEN=source\nEXTRA=value\n")
        (source / "personas/custom.txt").write_text("来源", encoding="utf-8")
        (target / "personas/custom.txt").write_text("本地", encoding="utf-8")
        m.write_json(
            source / "group_chat.json", {"groups": {"10": {"persona": {"name": "custom"}}}}
        )
        for root, updated, description in ((source, 2, "新"), (target, 1, "旧")):
            with closing(sqlite3.connect(root / "emotes/.memory.sqlite3")) as conn, conn:
                conn.execute(
                    "CREATE TABLE emote_memory(digest TEXT PRIMARY KEY, "
                    "description TEXT NOT NULL, updated REAL NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO emote_memory VALUES(?,?,?)", ("hash", description, updated)
                )
        output = self.base / "export.zip"
        m.export_data(source, output)
        m.import_data(target, output)
        self.assertIn("ONEBOT_ACCESS_TOKEN=test", (target / ".env").read_text(encoding="utf-8"))
        self.assertIn("EXTRA=value", (target / ".env").read_text(encoding="utf-8"))
        settings = json.loads((target / "group_chat.json").read_text(encoding="utf-8"))
        self.assertTrue(settings["groups"]["10"]["persona"]["name"].startswith("custom_导入_"))
        with closing(sqlite3.connect(target / "emotes/.memory.sqlite3")) as conn, conn:
            self.assertEqual(
                conn.execute("SELECT description FROM emote_memory").fetchone()[0], "新"
            )
