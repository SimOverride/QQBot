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
        self.assertEqual(rendered, "同名和同名，QQ40未知")
        self.write_names({"20": "新昵称"})
        self.assertEqual(Names(self.root, 99).text("QQ20"), "新昵称")

    def directory(self):
        atomic_json(
            self.root / "data/contacts.json",
            {
                "99": {
                    "users": {
                        "99": {"name": "鲸"},
                        "20": {"name": "同名"},
                        "30": {"name": "同名"},
                        "40": {"name": "外部人"},
                    },
                    "groups": {
                        "10": {"name": "测试群", "members": [20, 30]},
                        "11": {"name": "测试群", "members": [40]},
                    },
                }
            },
        )

    def test_legacy_formats_groups_and_safe_highlights(self):
        self.directory()
        renderer = Names(self.root, 99)
        value = "QQ号20、[30]、群号10、group_id=11、scope=-20、[CQ:at,qq=99]"
        rendered = renderer.data({"总体认知": value})["总体认知"]
        self.assertEqual(
            rendered, "同名、[同名]、测试群、测试群、私聊（同名）、[CQ:at,鲸（机器人自己）]"
        )
        parts = renderer.parts(value)
        self.assertEqual("".join(p["text"] for p in parts), rendered)
        self.assertEqual({p["kind"] for p in parts if "kind" in p}, {"person", "group"})
        self.assertEqual(renderer.text("引用[20,30]与群10"), "引用[同名,同名]与测试群")
        self.write_names({"20": "<img src=x onerror=alert(1)>"})
        self.assertEqual(
            Names(self.root, 99).parts("QQ20"),
            [{"text": "<img src=x onerror=alert(1)>", "kind": "person"}],
        )

    def test_no_directory_in_context_or_instructions(self):
        self.directory()
        renderer = Names(self.root, 99)
        identity = identity_context(Config(), 99, 10, 20, root=self.root)
        self.assertNotIn("platform_display_names", identity)
        self.assertNotIn("外部人", identity)
        messages = renderer.messages(
            [{"role": "system", "content": identity}, {"role": "user", "content": "QQ30在群10"}]
        )
        payload = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("current_names", payload)
        self.assertNotIn("外部人", payload)
        self.assertIn("kind=user", payload)
        self.assertIn("kind=group", payload)
        self.assertEqual(messages[1]["content"], "同名在测试群")

    def test_name_queries_enforce_scope_and_return_ambiguity(self):
        self.directory()
        tools = HistoryTools(self.archive, (99, 10, 20))
        args = {"kind": "user", "scope": None, "name": "同名"}
        result = tools.execute("resolve_entity", args)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({v["qq"] for v in result["matches"]}, {20, 30})
        self.assertEqual(tools.execute("resolve_entity", {**args, "name": "外部人"})["matches"], [])
        self.assertEqual(
            tools.execute("resolve_entity", {**args, "scope": 11})["status"], "access_denied"
        )
        self.assertEqual(
            tools.execute("resolve_entity", {**args, "name": ""})["status"], "invalid_arguments"
        )
        result = tools.execute("resolve_entity", {**args, "kind": "group", "name": "测试群"})
        self.assertEqual(result["matches"], [{"group_id": 10, "name": "测试群"}])
        private = HistoryTools(self.archive, (99, -20, 20), groups=(10, 11))
        self.assertEqual(
            private.execute("resolve_entity", {**args, "kind": "group", "name": "测试群"})[
                "status"
            ],
            "ambiguous",
        )

    async def test_model_can_choose_name_lookup_and_receive_numeric_id(self):
        self.directory()
        llm = LLM(None, Config(), SimpleNamespace(enabled=False))
        args = {"kind": "group", "scope": None, "name": "测试群"}
        llm.step = AsyncMock(
            side_effect=[
                (
                    "",
                    [
                        {
                            "call_id": "resolve",
                            "name": "resolve_entity",
                            "arguments": json.dumps(args),
                        }
                    ],
                    [],
                ),
                ("完成", [], []),
            ]
        )
        await llm.reply(
            [], "查群号", "test", history_tools=HistoryTools(self.archive, (99, 10, 20))
        )
        last = llm.step.call_args_list[1].args[0]
        output = next(
            m.get("output", m.get("content"))
            for m in last
            if m.get("type") == "function_call_output" or m.get("role") == "tool"
        )
        self.assertEqual(json.loads(output)["matches"][0]["group_id"], 10)

    def test_message_ids_are_not_numeric_nicknames_or_account_ids(self):
        self.write_names({"99": "鲸", "20": "1", "30": "W"})
        renderer = Names(self.root, 99)
        value = "(id 1、3、6、18、258、714)，消息ID: 20-22，QQ20 与 Windows"
        parts = renderer.parts(value)
        references = [p for p in parts if p.get("kind") == "message"]
        self.assertEqual(references[0]["ids"], [1, 3, 6, 18, 258, 714])
        self.assertEqual(references[1]["ids"], [20, 21, 22])
        self.assertEqual([p["text"] for p in parts if p.get("kind") == "person"], ["1"])
        self.assertEqual(renderer.plain(value), value.replace("QQ20", "1"))
        self.assertFalse(any(p.get("kind") for p in renderer.parts("18、714、Windows")))
        self.assertFalse(
            any(p.get("kind") == "message" for p in renderer.parts("群id=10 https://host/id123"))
        )
        self.assertEqual(renderer.parts("id 1-10000")[0]["ids"], [])

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
        args = {"scope": 10, "sender": 20}
        llm.step = AsyncMock(
            side_effect=[
                (
                    "",
                    [{"call_id": "a", "name": "search_messages", "arguments": json.dumps(args)}],
                    [],
                ),
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
