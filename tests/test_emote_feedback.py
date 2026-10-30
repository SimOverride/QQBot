"""表情经验由模型调用工具，验证跨会话来源和版本边界。"""

import tempfile
import time
import unittest
from pathlib import Path

from plugins.assistant.config import Config
from plugins.assistant.emote_feedback import migrate_legacy
from plugins.assistant.emotes import Emotes
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.vault import Vault


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(self.archive.close)
        self.library = Emotes(self.root / "emotes")
        for name in ("a.png", "b.png"):
            (self.library.root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        self.image = self.library.digest("a.png")
        self.seq = 100

    def send(self, ident=10, name="a.png", user=2):
        self.archive.collect((99, 20, 99), ident, f"[表情包：{name}]", ["image"], time.time())
        self.library.record_sent(name, 99, 20, ident, user, "交流游戏进展")

    def row(self, text, reply=None, user=2, scope=20):
        self.seq += 1
        self.archive.collect(
            (99, scope, user),
            self.seq,
            text,
            ["text"],
            time.time(),
            metadata={"reply_to": {"message_id": reply}} if reply else {},
        )
        return self.archive.db.execute(
            "SELECT * FROM messages WHERE message_id=?", (self.seq,)
        ).fetchone()

    def call(
        self,
        action="remember",
        subject=2,
        revision="",
        rows=(),
        reason="模型判断的使用经验",
        sentiment="uncertain",
    ):
        from types import SimpleNamespace

        from plugins.assistant.emote_state import execute

        session = SimpleNamespace(key=(99, 20, 2), archive=self.archive, emotes=self.library)
        return execute(
            session,
            dict(
                action=action,
                id="a.png",
                subject=subject,
                revision=revision,
                reason=reason,
                sentiment=sentiment,
                message_ids=[row["id"] for row in rows],
            ),
        )

    def test_cross_person_cross_scope_multiple_sources_without_send(self):
        first = self.row("他说“这也太逆天了？”", user=3, scope=-3)
        second = self.row("哈哈", user=4, scope=30)
        result = self.call("disable", subject=8, rows=[first, second])
        self.assertEqual(result["status"], "saved")
        note = Vault(self.root, 99).get(result["id"])
        self.assertEqual(note["subject"], 8)
        self.assertEqual({ref["usr"] for ref in note["sources"]}, {3, 4})
        self.assertTrue(self.library.experience(99, self.image, 8)["disabled"])
        self.assertFalse(self.library.experience(99, self.image, 2)["disabled"])

    def test_subjective_global_experience_without_source_and_explicit_enable(self):
        result = self.call("disable", subject=0)
        self.assertEqual(result["status"], "saved")
        note = Vault(self.root, 99).get(result["id"])
        self.assertEqual(note["authored"], "model")
        self.assertEqual(note["sources"], [])
        self.assertTrue(self.library.experience(99, self.image, 15)["disabled"])
        self.assertEqual(
            self.call("enable", subject=0, revision=result["revision"])["status"], "saved"
        )
        self.assertFalse(self.library.experience(99, self.image, 15)["disabled"])
        self.assertFalse(self.library.experience(100, self.image, 15)["disabled"])

    def test_old_feedback_can_revise_and_version_conflict_still_rejects(self):
        older = self.row("以前的意见", user=3)
        newer = self.row("新的意见", user=4)
        first = self.call(rows=[newer])
        self.assertEqual(self.call(rows=[older])["status"], "conflict")
        second = self.call(rows=[older], revision=first["revision"], reason="综合旧资料修正")
        self.assertEqual(second["status"], "saved")
        note = Vault(self.root, 99).get(second["id"])
        self.assertEqual(note["sources"][0]["usr"], 3)
        self.assertEqual(len(note["history"]), 1)

    def test_source_removal_invalidates_evidenced_experience(self):
        row = self.row("反馈")
        self.call(rows=[row], sentiment="positive")
        self.assertGreater(self.library.experience(99, self.image, 2)["score"], 0)
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        self.assertEqual(self.library.experience(99, self.image, 2)["notes"], [])

    def test_other_account_evidence_rejected(self):
        self.archive.collect((100, 30, 2), 777, "其他机器人资料", ["text"], time.time())
        row = self.archive.db.execute("SELECT * FROM messages WHERE bot=100").fetchone()
        self.assertEqual(self.call(rows=[row])["status"], "invalid_evidence")
        self.assertEqual(Vault(self.root, 99).list(kind="emote"), [])

    def test_sends_are_reference_not_write_prerequisite(self):
        self.send()
        self.library.record_sent("a.png", 99, 20, 10, 2, "重复回执")
        read = self.call("read")
        self.assertEqual(len(read["sends"]), 1)
        self.assertEqual(read["sends"][0]["message_id"], 10)
        self.assertEqual(self.library.usage(99, 20)[self.image]["use_count"], 1)

    def test_legacy_description_can_be_updated_with_current_version(self):
        self.library.remember([{"id": "a.png", "description": "旧描述"}], {"a.png": self.image})
        self.assertEqual(self.library.recall(self.image, 99), "旧描述")
        self.library.remember(
            [{"id": "a.png", "description": "新的解读"}], {"a.png": self.image}, bot=99
        )
        self.assertEqual(self.library.recall(self.image, 99), "新的解读")
        self.assertEqual(self.library.recall(self.image, 100), "旧描述")

    def test_legacy_group_experience_is_quarantined_without_expanding_ban(self):
        import json
        import sqlite3
        from contextlib import closing

        from plugins.assistant.emote_state import SCHEMA

        row = self.row("这张图不合适")
        evidence = json.dumps(
            [{"usr": 2, "message_id": row["message_id"], "content": row["content"]}]
        )
        with closing(sqlite3.connect(self.library.memory_path)) as db, db:
            db.execute("CREATE TABLE emote_group_state (" + SCHEMA + ")")
            db.execute(
                "INSERT INTO emote_group_state VALUES(?,?,?,?,?,?,?,?,?)",
                (99, 20, self.image, 1, "旧停用", "旧经验", evidence, time.time(), "[]"),
            )
        self.assertEqual(migrate_legacy(self.library, self.archive, 99), 1)
        self.assertEqual(migrate_legacy(self.library, self.archive, 99), 0)
        note = Vault(self.root, 99).list(history=True)[0]
        self.assertEqual(note["state"], "quarantine")
        self.assertEqual(note["sources"][0]["usr"], 2)
        self.assertTrue(note["legacy_group_state"]["disabled"])
        self.assertFalse(self.library.experience(99, self.image, 2)["disabled"])
        self.assertEqual(Vault(self.root, 100).list(history=True), [])


if __name__ == "__main__":
    unittest.main()
