"""人工来源不再锁定内容，旧记录保留正文与版本检查。"""

import tempfile
import unittest
from pathlib import Path

from plugins.assistant.vault import Vault, atomic


class ManualMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Vault(Path(self.temp.name), 99)

    def test_legacy_fixed_is_read_as_editable_manual_source(self):
        note = self.vault.set_field(2, "偏好", "喜欢茶", manual=True)
        path = self.vault.target(note["id"])
        raw = path.read_text(encoding="utf-8").replace("manual: true", "fixed: true")
        atomic(path, raw)
        before = path.read_bytes()
        legacy = self.vault.get(note["id"])
        self.assertTrue(legacy["manual"])
        self.assertNotIn("fixed", legacy)
        self.assertTrue(self.vault.valid(legacy))
        self.assertEqual(path.read_bytes(), before)
        saved = self.vault.put(
            {**legacy, "kind": "thought", "value": "更新后的想法"}, legacy["revision"]
        )
        self.assertFalse(saved["manual"])
        self.assertNotIn("fixed:", path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "版本冲突"):
            self.vault.put({**legacy, "value": "过期内容"}, legacy["revision"])

    def test_manual_source_does_not_exempt_nonempty_evidence(self):
        note = self.vault.put(
            {
                "kind": "fact",
                "subject": 2,
                "value": "手动内容",
                "sources": [
                    {
                        "bot": 99,
                        "grp": 10,
                        "usr": 2,
                        "message_id": 1,
                        "created": 1,
                        "hash": "a" * 64,
                    }
                ],
            },
            manual=True,
        )
        self.assertFalse(self.vault.valid(note))
