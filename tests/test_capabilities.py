"""验证整模块编辑、路径绑定、旧配置迁移及历史权限边界。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console_store import ConsoleStore
from plugins.assistant import prompt_store as store
from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.longterm import LongTermMemory


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()

    def save(self, value):
        (self.root / "data/console.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def test_startup_migration_while_runtime_lock_is_held(self):
        from migration import project_lock

        for value in ({"prompts": {"llm.poke.0": "自定义互动"}}, {"modules": {}}):
            with self.subTest(value=value):
                self.save(value)
                with project_lock(self.root, runtime=True):
                    store.migrate_console(self.root)
                    self.assertNotIn(
                        "prompts",
                        json.loads((self.root / "data/console.json").read_text(encoding="utf-8")),
                    )
                    # 配置迁移结束后，机器人仍独占运行锁。
                    with self.assertRaises(ValueError):
                        with project_lock(self.root, runtime=True):
                            self.fail("运行锁不能被迁移流程释放")

    def test_config_migration_obeys_migration_lock(self):
        from migration import project_lock

        self.save({"prompts": {"llm.poke.0": "自定义互动"}})
        original = (self.root / "data/console.json").read_bytes()
        with project_lock(self.root, name=".migration.lock"):
            with self.assertRaises(ValueError):
                store.migrate_console(self.root)
        self.assertEqual((self.root / "data/console.json").read_bytes(), original)
        self.assertFalse((self.root / "backups").exists())

    def test_legacy_migration_backup_idempotence_and_new_value_priority(self):
        legacy = {
            "prompts": {
                "tool.prompts.1": "带上必要语境后再查询",
                "tool.history_tools.2": "只查获准会话",
                "llm.poke.0": "按语境自然回应拍一拍",
            },
            "modules": {
                "search": {"tools": {"web_search": {"parameters": {"query": "新配置优先"}}}}
            },
            "groups": {"99:10": {"summary": "摘要", "knowledge": "认知"}},
        }
        self.save(legacy)
        original = (self.root / "data/console.json").read_bytes()
        store.migrate_console(self.root)
        actual = json.loads((self.root / "data/console.json").read_text(encoding="utf-8"))
        self.assertNotIn("prompts", actual)
        self.assertEqual(actual["groups"], legacy["groups"])
        self.assertEqual(
            store.module_value("search", self.root)["tools"]["web_search"]["parameters"]["query"],
            "新配置优先",
        )
        for name in ("search_messages", "resolve_entity"):
            self.assertEqual(
                store.module_value("history", self.root)["tools"][name]["parameters"]["scope"],
                "只查获准会话",
            )
        backups = list((self.root / "backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        store.migrate_console(self.root)
        self.assertEqual(list((self.root / "backups").glob("*.json")), backups)

    def test_invalid_legacy_migration_leaves_file_untouched(self):
        self.save({"prompts": {"prompts.system_prompt.0": "缺少日期"}})
        before = (self.root / "data/console.json").read_bytes()
        with self.assertRaises(ValueError):
            store.migrate_console(self.root)
        self.assertEqual((self.root / "data/console.json").read_bytes(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_every_legacy_default_is_retired_without_freezing_old_contract(self):
        mapping = json.loads(store.MIGRATION_PATH.read_text(encoding="utf-8"))
        result = store.normalize_console({"prompts": {k: v["default"] for k, v in mapping.items()}})
        self.assertEqual(result["modules"], {})
        retired = store.normalize_console({"prompts": {"llm.extract_facts.0": ""}})
        self.assertEqual(retired["modules"], {})

    def test_binding_is_by_name_and_nested_parameter_path(self):
        value = store.module_defaults("media")
        value["tools"]["remember_emotes"]["parameters"]["items[].id"] = "只修改这个字段"
        value["tools"]["choose_emote"]["description"] = "新选择说明"
        self.save({"modules": {"media": store.sparse_module("media", value)}})
        functions = [
            store.tool_definition(n) for n in ("remember_emotes", "choose_emote", "inspect_emote")
        ]
        original = copy.deepcopy(functions)
        effective = store.refresh_tools(functions, self.root)
        self.assertEqual(functions, original)
        self.assertEqual(
            effective[0]["parameters"]["properties"]["items"]["items"]["properties"]["id"][
                "description"
            ],
            "只修改这个字段",
        )
        self.assertEqual(effective[1]["description"], "新选择说明")
        self.assertEqual(effective[2]["description"], original[2]["description"])
        for before, after in zip(original, effective):
            self.assertEqual(before["parameters"]["required"], after["parameters"]["required"])

    def test_module_shape_and_placeholders_cannot_be_removed(self):
        value = store.module_defaults("chat")
        value["prompts"]["prompts.system_prompt.0"] = "没有日期"
        with self.assertRaises(ValueError):
            store.validate_module("chat", value)
        value = store.module_defaults("history")
        value["tools"]["search_messages"]["parameters"]["unknown"] = "未知参数"
        with self.assertRaises(ValueError):
            store.validate_module("history", value)
        value = store.module_defaults("history")
        del value["tools"]["list_sessions"]
        with self.assertRaises(ValueError):
            store.validate_module("history", value)

    def test_console_module_save_conflict_undo_and_live_tool_refresh(self):
        console = ConsoleStore(self.root)
        current = console.get("module:search")
        value = copy.deepcopy(current["value"])
        value["tools"]["web_search"]["parameters"]["query"] = "自包含的搜索语句"
        console.put("module:search", value, current["revision"])
        effective = store.refresh_tools([store.tool_definition("web_search")], self.root)[0]
        self.assertEqual(
            effective["parameters"]["properties"]["query"]["description"], "自包含的搜索语句"
        )
        with self.assertRaises(ValueError):
            console.put("module:search", current["value"], current["revision"])
        console.undo(console.changes()[0]["id"])
        self.assertEqual(console.get("module:search")["value"], current["value"])
        with self.assertRaises(ValueError):
            console.get("prompt:tool.prompts.1")


class HistoryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = LongTermMemory(self.root / "memory.db", Config())
        self.addCleanup(self.archive.close)
        for key, text in [
            ((99, -1, 1), "私聊"),
            ((99, 10, 2), "本群"),
            ((99, 11, 3), "其他群"),
            ((98, 10, 2), "其他机器人"),
        ]:
            self.archive.collect(key, 1, text, ["text"], 100)
        self.private = HistoryTools(self.archive, (99, -1, 1), [10])
        self.group = HistoryTools(self.archive, (99, 10, 1), [11])

    def test_default_scope_explicit_scope_and_context_permission(self):
        result = self.private.execute("search_messages", {})
        self.assertEqual([m["text"] for m in result["messages"]], ["私聊"])
        result = self.private.execute("search_messages", {"scope": None})
        self.assertEqual({m["scope"] for m in result["messages"]}, {-1, 10})
        listed = self.private.execute("list_sessions", {})
        self.assertEqual(listed["allowed_scopes"], [-1, 10])
        for row in self.archive.db.execute("SELECT id,bot,grp FROM messages").fetchall():
            result = self.group.execute("read_message_context", {"id": row["id"]})
            self.assertEqual(
                result["status"], "ok" if row["bot"] == 99 and row["grp"] == 10 else "not_found"
            )
        self.assertEqual(
            self.group.execute("search_messages", {"scope": 11})["status"], "access_denied"
        )

    def test_invalid_arguments_and_removed_dispatch_are_rejected(self):
        for name, arguments in [
            ("read_history", {}),
            ("list_sessions", {"query": "多余"}),
            ("search_messages", {"offset": True}),
            ("search_messages", {"offset": -1}),
            ("search_messages", {"scope": True}),
            ("search_messages", {"sender": -1}),
            ("read_message_context", {}),
            ("read_message_context", {"id": -1}),
            ("resolve_entity", {"name": "某人", "kind": "unknown"}),
            ("read_knowledge", {"subject": 0}),
            ("read_knowledge", {"kind": "group", "subject": 1}),
        ]:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.group.execute(name, arguments)["status"], "invalid_arguments")

    def test_knowledge_uses_current_scope_and_known_subjects(self):
        self.assertEqual(self.group.execute("read_knowledge", {})["subject_qq"], 1)
        self.assertEqual(self.group.execute("read_knowledge", {"subject": 2})["status"], "ok")
        self.assertEqual(
            self.group.execute("read_knowledge", {"subject": 3})["status"], "access_denied"
        )
        self.assertEqual(
            self.private.execute("read_knowledge", {"kind": "group"})["status"], "access_denied"
        )
        with patch.object(
            store, "group_knowledge", return_value={"summary": "群摘要", "knowledge": "群认知"}
        ) as getter:
            self.assertEqual(
                self.group.execute("read_knowledge", {"kind": "group"})["summary"], "群摘要"
            )
            getter.assert_called_once_with(99, 10, self.archive.root)
