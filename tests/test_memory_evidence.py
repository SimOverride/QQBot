"""模型判断内容含义，程序仅核对真实来源和身份数据格式。"""

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
from plugins.assistant.memory_worker import Worker, grounded
from plugins.assistant.vault import Vault


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(self.archive.close)
        self.archive.collect((99, 10, 2), 1, "小明喜欢“星际穿越”", ["text"], time.time())

    def test_punctuation_and_keyword_are_not_semantic_filters(self):
        for evidence in (
            "“星际穿越”",
            "> 引用内容",
            "```代码```",
            "我在学习密码学",
            "secret garden",
        ):
            self.assertTrue(grounded(evidence, evidence))
        self.assertFalse(grounded("不存在的内容", "实际原文"))

    async def test_background_fact_can_target_third_party(self):
        llm = SimpleNamespace(
            step=AsyncMock(
                return_value=(
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "action": "ADD",
                                    "target": "",
                                    "subject": 3,
                                    "kind": "fact",
                                    "field": "作品偏好",
                                    "value": "QQ2称QQ3喜欢星际穿越，尚未由本人确认",
                                    "evidence": "小明喜欢“星际穿越”",
                                }
                            ]
                        }
                    ),
                    [],
                    [],
                )
            )
        )
        await Worker(self.archive, llm).tick()
        notes = Vault(self.root, 99).list(kind="fact")
        self.assertEqual(notes[0]["subject"], 3)
        self.assertEqual(notes[0]["sources"][0]["usr"], 2)

    def test_memory_tool_keeps_third_party_source(self):
        row = self.archive.db.execute("SELECT * FROM messages").fetchone()
        session = SimpleNamespace(archive=self.archive, key=(99, 10, 2))
        result = execute(
            session,
            dict(
                action="write",
                subject=3,
                field="作品偏好",
                value="QQ2提及QQ3喜欢星际穿越",
                revision="",
                sources=[row["id"]],
            ),
        )
        self.assertEqual(result["status"], "saved")
        note = Vault(self.root, 99).get(result["id"])
        self.assertEqual(note["subject"], 3)
        self.assertEqual(note["sources"][0]["usr"], 2)
