"""表情统一经验的真实发送归因、纠正、去重与人工保护。"""

import tempfile
import time
import unittest
from pathlib import Path

from plugins.assistant.config import Config
from plugins.assistant.emote_feedback import collect_feedback, migrate_legacy
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

    def test_reply_feedback_shared_private_and_other_group(self):
        self.send()
        result = collect_feedback(self.archive, self.row("不要再发这张图", 10), self.library)
        self.assertEqual(result["status"], "saved")
        self.assertTrue(self.library.experience(99, self.image, 2)["disabled"])
        self.assertFalse(self.library.experience(99, self.image, 3)["disabled"])
        self.assertFalse(self.library.experience(100, self.image, 2)["disabled"])
        self.assertEqual(
            self.library.list({"query": "", "offset": 0}, 99, -2, self.archive, 2)["emotes"][0][
                "id"
            ],
            "b.png",
        )

    def test_silence_unrelated_and_ambiguous_feedback_do_not_learn(self):
        self.send()
        for text in (
            "哈哈",
            "这个游戏好有趣",
            "假如这张图不好笑呢",
            "> 这张图别用了",
            "这张图我不是不喜欢",
        ):
            self.assertEqual(
                collect_feedback(self.archive, self.row(text), self.library)["status"],
                "not_feedback",
            )
        self.send(11, "b.png")
        self.assertEqual(
            collect_feedback(self.archive, self.row("这张图很可爱"), self.library)["status"],
            "not_feedback",
        )
        self.assertEqual(Vault(self.root, 99).list(kind="emote"), [])

    def test_only_confirmed_send_can_receive_feedback(self):
        self.library.record_sent("a.png", 99, 20)
        self.assertEqual(
            collect_feedback(self.archive, self.row("这张图很好笑", 10), self.library)["status"],
            "not_feedback",
        )

    def test_platform_second_precision_and_repeated_send_receipt(self):
        self.send()
        self.library.record_sent("a.png", 99, 20, 10, 2, "重复回执")
        self.assertEqual(self.library.usage(99, 20)[self.image]["use_count"], 1)
        self.archive.collect(
            (99, 20, 2),
            501,
            "这张图很合适",
            ["text"],
            int(time.time()),
            metadata={"reply_to": {"message_id": 10}},
        )
        row = self.archive.db.execute("SELECT * FROM messages WHERE message_id=501").fetchone()
        self.assertEqual(collect_feedback(self.archive, row, self.library)["status"], "saved")

    def test_retry_idempotent_and_explicit_correction_retains_history(self):
        self.send()
        first = self.row("以后别发这张图了", 10)
        saved = collect_feedback(self.archive, first, self.library)
        self.assertEqual(collect_feedback(self.archive, first, self.library)["status"], "unchanged")
        self.assertEqual(len(Vault(self.root, 99).get(saved["id"])["history"]), 0)
        result = collect_feedback(self.archive, self.row("这张图可以再发了", 10), self.library)
        self.assertEqual(result["status"], "saved")
        note = Vault(self.root, 99).get(result["id"])
        self.assertEqual(len(note["history"]), 1)
        self.assertFalse(note["disabled"])
        self.assertTrue(note["history"][0]["disabled"])

    def test_source_removal_invalidates_feedback(self):
        self.send()
        row = self.row("这张图好笑", 10)
        collect_feedback(self.archive, row, self.library)
        self.assertGreater(self.library.experience(99, self.image, 2)["score"], 0)
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE message_id=10")
        self.assertEqual(self.library.experience(99, self.image, 2)["notes"], [])

    def test_fixed_feedback_cannot_be_overwritten(self):
        self.send()
        saved = collect_feedback(self.archive, self.row("不要再发", 10), self.library)
        vault = Vault(self.root, 99)
        note = vault.get(saved["id"])
        vault.put({**note, "fixed": True}, note["revision"], manual=True)
        self.assertEqual(
            collect_feedback(self.archive, self.row("可以再发", 10), self.library)["status"],
            "fixed",
        )

    def test_description_uses_vault_after_first_import(self):
        self.library.remember(
            [{"id": "a.png", "description": "画面是一只猫"}], {"a.png": self.image}
        )
        self.assertEqual(self.library.recall(self.image, 99), "画面是一只猫")
        note = self.library.description_note(99, self.image)
        vault = Vault(self.root, 99)
        vault.put({**note, "value": "人工确认的猫", "fixed": True}, note["revision"], manual=True)
        self.assertEqual(self.library.recall(self.image, 99), "人工确认的猫")
        self.assertEqual(self.library.recall(self.image, 100), "画面是一只猫")

    def test_feedback_changes_ranking_without_cross_person_ban(self):
        self.send()
        collect_feedback(self.archive, self.row("这张图不好笑", 10), self.library)
        result = self.library.list({"query": "", "offset": 0}, 99, 30, self.archive, 2)
        self.assertEqual(result["emotes"][0]["id"], "b.png")
        self.assertEqual(len(result["emotes"]), 2)

    def test_legacy_description_is_fixed_until_maintainer_unpins_it(self):
        from plugins.assistant.http import ServiceError

        self.library.remember([{"id": "a.png", "description": "旧人工描述"}], {"a.png": self.image})
        self.assertEqual(self.library.recall(self.image, 99), "旧人工描述")
        note = self.library.description_note(99, self.image)
        self.assertTrue(note["fixed"])
        self.assertTrue(note["legacy_import"])
        with self.assertRaises(ServiceError):
            self.library.remember(
                [{"id": "a.png", "description": "新的自动解读"}],
                {"a.png": self.image},
                bot=99,
            )
        vault = Vault(self.root, 99)
        vault.put({**note, "fixed": False}, note["revision"], manual=True)
        self.library.remember(
            [{"id": "a.png", "description": "新的自动解读"}],
            {"a.png": self.image},
            bot=99,
        )
        self.assertEqual(self.library.recall(self.image, 99), "新的自动解读")
        self.assertEqual(self.library.recall(self.image, 100), "旧人工描述")

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
