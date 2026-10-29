"""统一认知、来源失效、固定值和后台队列的隔离验证。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory_tools import execute
from plugins.assistant.memory_worker import Worker
from plugins.assistant.vault import Vault, source


class VaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.vault = Vault(self.root, 99)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def message(self, scope=10, user=2, ident=1, content="我喜欢紫色纸飞机"):
        self.archive.collect((99, scope, user), ident, content, ["text"], time.time())
        return self.archive.db.execute(
            "SELECT * FROM messages WHERE grp=? AND message_id=?", (scope, ident)
        ).fetchone()

    def test_cross_group_private_and_identity(self):
        row = self.message(-2)
        self.vault.set_field(2, "总体认知", "喜欢紫色纸飞机", sources=[source(row)])
        self.assertEqual(self.archive.facts((99, 20, 2))[0]["value"], "喜欢紫色纸飞机")
        self.assertTrue(self.archive.recall((99, 30, 3), "谁喜欢紫色纸飞机"))
        self.assertEqual(Vault(self.root, 100).list(), [])
        self.assertEqual(self.archive.facts((99, 30, 3)), [])

    def test_fixed_empty_suppresses_other_notes_and_auto_write(self):
        row = self.message()
        self.vault.put(
            {"subject": 2, "field": "总体认知", "value": "旧值", "sources": [source(row)]}
        )
        self.vault.set_field(2, "总体认知", "", manual=True)
        self.assertEqual(self.vault.person(2)[0]["总体认知"], "")
        with self.assertRaisesRegex(ValueError, "固定"):
            self.vault.put(
                {"subject": 2, "field": "总体认知", "value": "旧值", "sources": [source(row)]}
            )

    def test_source_modified_invalidates_global_note(self):
        row = self.message()
        self.vault.set_field(2, "总体认知", "喜欢紫色纸飞机", sources=[source(row)])
        with self.archive.db:
            self.archive.db.execute("UPDATE messages SET content='已修改'")
        self.assertEqual(self.vault.list(), [])
        self.assertEqual(len(self.vault.list(history=True)), 1)

    def test_stale_revision_and_history(self):
        old = self.vault.set_field(99, "自我档案", "好奇", kind="self")
        new = self.vault.set_field(99, "自我档案", "认真", kind="self", expected=old["revision"])
        self.assertEqual(new["history"][0]["value"], "好奇")
        with self.assertRaisesRegex(ValueError, "版本冲突"):
            self.vault.set_field(99, "自我档案", "旧回合", kind="self", expected=old["revision"])

    def test_tool_uses_cross_scope_evidence_but_not_other_person(self):
        row = self.message(-2)
        session = SimpleNamespace(archive=self.archive, key=(99, 20, 3))
        args = dict(
            action="write",
            subject=2,
            field="总体认知",
            value="喜欢紫色纸飞机",
            revision="",
            sources=[row["id"]],
        )
        self.assertEqual(execute(session, args)["status"], "saved")
        args.update(subject=3)
        self.assertEqual(execute(session, args)["status"], "invalid_evidence")

    async def test_worker_restarts_without_duplicate_and_no_wrapped_context(self):
        row = self.message()
        action = dict(
            action="ADD",
            target="",
            kind="fact",
            field="偏好",
            value="QQ2喜欢紫色纸飞机",
            evidence=row["content"],
            occurred_at="unknown",
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [action]}), [], []))
        )
        await Worker(self.archive, llm).tick()
        await Worker(self.archive, llm).tick()
        self.assertEqual(len(self.vault.list(kind="fact")), 1)
        self.assertEqual(llm.step.await_count, 1)
        request = json.loads(llm.step.call_args.args[0][1]["content"])
        self.assertEqual(request["message"]["content"], row["content"])
        self.assertNotIn("assistant", request)

    async def test_worker_retry_persists_and_deleted_source_is_not_captured(self):
        self.message()
        llm = SimpleNamespace(step=AsyncMock(side_effect=TimeoutError))
        await Worker(self.archive, llm).tick()
        path = next((self.root / "data/memory-jobs/99").glob("*.json"))
        job = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(job["state"], "failed")
        job["retry_at"] = 0
        path.write_text(json.dumps(job))
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages")
        await Worker(self.archive, llm).tick()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["result"], "source_removed")
        self.assertEqual(llm.step.await_count, 1)

    async def test_worker_rejects_ungrounded_fact(self):
        self.message()
        action = dict(
            action="ADD",
            target="",
            kind="fact",
            field="偏好",
            value="不存在",
            evidence="模型幻觉",
            occurred_at="unknown",
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [action]}), [], []))
        )
        await Worker(self.archive, llm).tick()
        self.assertEqual(self.vault.list(), [])

    def test_decay_does_not_delete_or_reinforce_on_read(self):
        row = self.message()
        note = self.vault.put(
            {
                "subject": 2,
                "value": "紫色纸飞机",
                "sources": [source(row)],
                "reinforced": time.time() - 86400 * 180,
            }
        )
        self.assertEqual(self.vault.recall("紫色纸飞机"), [])
        self.assertEqual(self.vault.get(note["id"])["revision"], note["revision"])

    async def test_correction_retains_history_and_checkpoint_is_idempotent(self):
        row = self.message()
        first = dict(
            action="ADD",
            target="",
            kind="fact",
            field="偏好",
            value="喜欢紫色纸飞机",
            evidence=row["content"],
            occurred_at="unknown",
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [first]}), [], []))
        )
        worker = Worker(self.archive, llm)
        await worker.tick()
        old = self.vault.list(kind="fact")[0]
        row = self.message(ident=2, content="纠正一下，我喜欢蓝色纸飞机")
        second = {
            **first,
            "action": "CORRECT",
            "target": old["id"],
            "value": "喜欢蓝色纸飞机",
            "evidence": row["content"],
        }
        llm.step.return_value = (json.dumps({"actions": [second]}), [], [])
        await worker.tick()
        note = self.vault.list(kind="fact")[0]
        self.assertEqual(note["value"], "喜欢蓝色纸飞机")
        self.assertEqual(note["history"][0]["value"], "喜欢紫色纸飞机")
        for path in (self.root / "data/memory-jobs/99").glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["source"]["message_id"] == 2:
                job.update(state="pending", completed=[])
                path.write_text(json.dumps(job))
        await Worker(self.archive, llm).tick()
        self.assertEqual(self.vault.get(note["id"])["revision"], note["revision"])

    async def test_quoted_examples_cannot_become_facts(self):
        self.message(content="举例：“我喜欢紫色纸飞机”，这是示例。")
        action = dict(
            action="ADD",
            target="",
            kind="fact",
            field="偏好",
            value="喜欢紫色纸飞机",
            evidence="我喜欢紫色纸飞机",
        )
        llm = SimpleNamespace(
            step=AsyncMock(return_value=(json.dumps({"actions": [action]}), [], []))
        )
        await Worker(self.archive, llm).tick()
        self.assertEqual(self.vault.list(), [])

    def test_quarantined_manual_conflict_blocks_automatic_replacement(self):
        self.vault.put(
            {
                "subject": 2,
                "field": "偏好",
                "value": "冲突待处理",
                "fixed": True,
                "state": "quarantine",
            },
            manual=True,
        )
        row = self.message()
        with self.assertRaisesRegex(ValueError, "固定"):
            self.vault.put(
                {"subject": 2, "field": "偏好", "value": "新猜测", "sources": [source(row)]}
            )
