import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import archive_event
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupConversation
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.message_context import event_metadata
from plugins.assistant.service import Assistant


class AttributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.db"
        self.archive = LongTermMemory(self.path, Config())

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def test_mentions_quotes_and_restart(self):
        import time

        stamp = time.time()
        self.archive.collect(
            (99, 10, 3), 10, "这是第三个人说的", ["text"], stamp,
            metadata={"sender_name": "同名"},
        )
        event = SimpleNamespace(
            group_id=10, user_id=2, message_id=11, time=stamp,
            sender=SimpleNamespace(card="同名", role="admin"),
            original_message=MessageSegment.reply(10) + MessageSegment.at(3)
            + MessageSegment.at(99) + Message("你说的对"),
        )
        archive_event(self.archive, Config(), SimpleNamespace(self_id="99"), event)
        self.archive.collect(
            (99, 10, 99), 12, "收到", ["text"], stamp, 2,
            metadata={"response_to": {"message_id": 11, "sender": 2}},
        )
        self.archive.close()
        self.archive = LongTermMemory(self.path, Config())
        controller = GroupConversation(SimpleNamespace(archive=self.archive), None, None)
        recent = controller.context(99, 10)
        quoted = next(r for r in recent if r["message_id"] == 11)
        self.assertEqual(quoted["sender"], 2)
        self.assertEqual(quoted["mentions"], [3, 99])
        self.assertEqual(quoted["reply_to"]["sender"], 3)
        self.assertEqual(quoted["reply_to"]["text"], "这是第三个人说的")
        tools = HistoryTools(self.archive, (99, 10, 2))
        result = tools.run({
            "action": "search", "scope": 10, "sender": None, "query": "",
            "start": None, "end": None, "anchor": None, "offset": 0,
        })
        saved = next(r for r in result["messages"] if r["message_id"] == 11)
        self.assertEqual(saved["reply_to"], quoted["reply_to"])
        self.assertEqual(result["messages"][0]["response_to"]["sender"], 2)
        self.assertEqual(json.loads(self.archive.context((99, 10, 2), "", set()))[
            "subject_qq"
        ], 2)

    def test_resolved_quote_without_local_source(self):
        event = SimpleNamespace(
            original_message=Message(MessageSegment.at("all")),
            reply=SimpleNamespace(
                message_id=8, sender=SimpleNamespace(user_id=3, nickname="旧名字"),
                message=Message("原话"),
            ),
        )
        metadata = event_metadata(event)
        self.assertEqual(metadata["mentions"], ["all"])
        self.assertEqual(metadata["reply_to"]["sender"], 3)

    def test_old_database_migration(self):
        self.archive.close()
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE messages DROP COLUMN metadata")
            db.execute(
                "INSERT INTO messages(bot,grp,usr,message_id,content,types,created) "
                "VALUES(99,10,2,1,'old','[]',1)"
            )
        db.close()
        self.archive = LongTermMemory(self.path, Config())
        row = self.archive.db.execute("SELECT * FROM messages").fetchone()
        context = self.archive.message_context(row)
        self.assertEqual(context["sender"], 2)
        self.assertIsNone(context["mentions"])
        self.assertIsNone(context["reply_to"])

    async def test_live_role_and_failure(self):
        for role, label in (("admin", "群管理员"), ("owner", "群主"), (None, "普通用户")):
            llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("ok")))
            service = Assistant(Config(), llm)
            lookup = AsyncMock(return_value=role)
            if role is None:
                lookup.side_effect = RuntimeError()
            await service.handle(99, 10, 2, 1, "我是谁", AsyncMock(), lookup)
            identity = json.loads(llm.reply.call_args.args[5].split("\n")[-1])
            self.assertEqual(identity["current_speaker"]["role"], label)
            self.assertEqual(identity["current_speaker"]["group_role"], role)
