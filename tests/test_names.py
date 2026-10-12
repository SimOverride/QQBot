"""昵称展示与账号存储边界验证。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from console_store import ConsoleStore, atomic_json
from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.identity import identity_context
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.names import Names


class NamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_names({"99": "鲸", "20": "小明", "30": "小红"})
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(self.archive.close)
        self.archive.collect((99, 10, 20), 1, "与QQ30交流，向99提问", ["text"], 100)

    def write_names(self, names):
        atomic_json(
            self.root / "data/contacts.json",
            {"99": {"users": {qq: {"name": name} for qq, name in names.items()}}},
        )

    def test_text_and_structured_identifiers_are_separate(self):
        renderer = Names(self.root, 99)
        data = {
            "sender": 20,
            "id": 30,
            "scope": 10,
            "sender_name": "旧昵称",
            "text": "与QQ30交流，向99提问；不是120、30.5或群30；https://host/30",
        }
        output = json.loads(renderer.text("资料：" + json.dumps(data, ensure_ascii=False))[3:])
        self.assertEqual(output["sender"], 20)
        self.assertEqual(output["id"], 30)
        self.assertEqual(output["sender_name"], "小明")
        self.assertIn("与小红交流，向鲸（机器人自己）提问", output["text"])
        self.assertIn("120、30.5或群30；https://host/30", output["text"])
        self.assertEqual(data["sender_name"], "旧昵称")

    def test_collisions_unknown_names_and_rename(self):
        self.write_names({"99": "鲸", "20": "同名", "30": "同名"})
        rendered = Names(self.root, 99).text("QQ20和QQ30，QQ40未知")
        self.assertEqual(rendered, "同名（QQ 20）和同名（QQ 30），QQ40未知")
        self.write_names({"20": "新昵称"})
        self.assertEqual(Names(self.root, 99).text("QQ20"), "新昵称")

    def test_console_display_does_not_change_stored_values(self):
        self.archive.remember((99, 10, 20), "总体认知", "经常与QQ30交流，向99提问")
        store = ConsoleStore(self.root)
        old = store.get("person:99:20")
        self.assertIn("QQ30", old["value"]["总体认知"])
        self.assertIn("小红", old["display_value"]["总体认知"])
        self.assertIn("机器人自己", old["display_value"]["总体认知"])
        self.write_names({"99": "鲸", "20": "小明", "30": "新昵称"})
        new = store.get("person:99:20")
        self.assertEqual(new["value"], old["value"])
        self.assertEqual(new["revision"], old["revision"])
        self.assertIn("新昵称", new["display_value"]["总体认知"])

    def test_explicit_bot_identity(self):
        data = identity_context(Config(), 99, 10, 20, root=self.root)
        self.assertIn('"bot_self": {"qq": 99, "name": "鲸", "is_self": true', data)
        self.assertIn('"current_speaker": {"qq": 20, "name": "小明"', data)

    async def test_reply_and_tool_history_use_names_but_keep_ids(self):
        cfg = Config()
        llm = LLM(None, cfg, SimpleNamespace(enabled=False))
        tools = HistoryTools(self.archive, (99, 10, 20))
        args = {
            "action": "search",
            "scope": 10,
            "sender": 20,
            "query": "",
            "start": None,
            "end": None,
            "anchor": None,
            "offset": 0,
        }
        llm.step = AsyncMock(
            side_effect=[
                ("", [{"call_id": "a", "name": "read_history", "arguments": json.dumps(args)}], []),
                ("完成", [], []),
            ]
        )
        await llm.reply([], "你好", "test", memory_context="经常与QQ30交流", history_tools=tools)
        first = llm.step.call_args_list[0].args[0]
        final = llm.step.call_args_list[1].args[0]
        self.assertIn("经常与小红交流", json.dumps(first, ensure_ascii=False))
        self.assertIn('"bot_self"', first[0]["content"])
        self.assertIn("与小红交流", json.dumps(final, ensure_ascii=False))
        self.assertIn("QQ30", self.archive.db.execute("SELECT content FROM messages").fetchone()[0])

    async def test_planner_uses_same_identity_projection(self):
        llm = LLM(None, Config(), SimpleNamespace(enabled=False))
        llm.step = AsyncMock(return_value=('{"reply":false}', [], []))
        await llm.plan_participation(
            [{"sender": 20, "text": "请QQ99评价QQ30"}],
            {},
            {},
            {},
            1,
            False,
            99,
            history_tools=HistoryTools(self.archive, (99, 10, 20)),
        )
        messages = llm.step.call_args.args[0]
        self.assertIn("鲸（机器人自己）", json.dumps(messages, ensure_ascii=False))
        self.assertIn("小红", json.dumps(messages, ensure_ascii=False))
