"""Obsidian 草稿与基线的完整迁移、冲突保留验证。"""

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

import migration
from plugins.assistant.obsidian import Workbench
from plugins.assistant.vault import Vault


class DraftMigrationTests(unittest.TestCase):
    def test_export_import_preserves_pending_draft_and_excludes_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, target = base / "source", base / "target"
            root.mkdir()
            target.mkdir()
            note = Vault(root, 99).set_field(2, "偏好", "蓝色", manual=True)
            work = Workbench(root, 99)
            work.prepare()
            work.draft(note["id"])
            draft = work.vault.path / "工作台" / (note["id"] + ".md")
            draft.write_text("绿色", encoding="utf-8")
            output = base / "backup.zip"
            migration.export_data(root, output, 99)
            with ZipFile(output) as archive:
                self.assertTrue(any("工作台/" in name for name in archive.namelist()))
                self.assertFalse(
                    any("图谱/" in name or "导航/" in name for name in archive.namelist())
                )
            migration.import_data(target, output, 99)
            review = Workbench(target, 99).review(note["id"])
            self.assertEqual(review["after"], "绿色")
            self.assertFalse(review["conflict"])
            self.assertTrue(review["changed"])

    def test_draft_conflict_retains_target_and_quarantines_source(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, target = base / "source", base / "target"
            root.mkdir()
            target.mkdir()
            source = Vault(root, 99).set_field(2, "偏好", "蓝色", manual=True)
            for folder, value in ((root, "来源待审"), (target, "目标待审")):
                vault = Vault(folder, 99)
                if folder == target:
                    vault.put({k: v for k, v in source.items() if k != "revision"}, manual=True)
                work = Workbench(folder, 99)
                work.draft(source["id"])
                (vault.path / "工作台" / (source["id"] + ".md")).write_text(value, encoding="utf-8")
            output = base / "backup.zip"
            migration.export_data(root, output, 99)
            migration.import_data(target, output, 99)
            self.assertEqual(Workbench(target, 99).review(source["id"])["after"], "目标待审")
            conflicts = [n for n in Vault(target, 99).list(history=True) if "draft_origin" in n]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["value"], "来源待审")
            self.assertEqual(conflicts[0]["state"], "quarantine")
            migration.import_data(target, output, 99)
            self.assertEqual(
                len([n for n in Vault(target, 99).list(history=True) if "draft_origin" in n]), 1
            )

    def test_foreign_or_incomplete_draft_pair_is_rejected(self):
        from plugins.assistant.memory_drafts import merge

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = "data/memory-obsidian/99/" + "a" * 32 + ".json"
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "同时"):
                merge(root, root, {name: path}, 99)
            with self.assertRaisesRegex(ValueError, "账号"):
                merge(root, root, {name: path}, 100)
