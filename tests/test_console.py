"""后台端到端接口与运行时生效验证，不调用真实模型。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import migration
from admin_server import create_app
from console_store import ConsoleStore, atomic_json
from plugins.assistant import prompt_store
from plugins.assistant.config import Config
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory import Memory
from plugins.assistant.prompts import system_prompt


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("personas", "styles", "emotes", "data"):
            (self.root / folder).mkdir()
        for folder in ("personas", "styles"):
            (self.root / folder / "默认.txt").write_text("默认内容", encoding="utf-8")
        (self.root / ".env").write_text(
            "LLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=test-key\nDEEPSEEK_MODEL=test-model\n",
            encoding="utf-8",
        )
        atomic_json(self.root / "group_chat.json", {"default_activity": 50, "groups": {}})
        self.archive = LongTermMemory(self.root / migration.MEMORY, Config())
        self.addCleanup(self.archive.close)
        self.archive.collect((123, 10, 20), 1, "喜欢游戏", ["text"], 100)
        self.archive.save((123, 10, 20), 1, "喜欢游戏", "知道了")
        self.store = ConsoleStore(self.root)
        self.patch = patch.object(prompt_store, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.app = create_app(self.root)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.addCleanup(self.client.close)
        self.headers = {"X-Console-Token": self.app.state.token}

    def test_session_service_identifier(self):
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "qqbot-console")

    def save(self, resource, value):
        before = self.store.get(resource)
        return self.store.put(resource, value, before["revision"])

    def test_profile_create_delete_restore_and_references(self):
        for kind in ("persona", "style"):
            result = self.client.post(
                "/api/profiles",
                headers=self.headers,
                json={"kind": kind, "name": "新模板", "value": "新的内容"},
            )
            self.assertEqual(result.status_code, 200, result.text)
            item = result.json()
            self.assertEqual(self.store.get(kind + ":新模板")["value"], "新的内容")
            self.assertEqual(
                self.client.post(
                    "/api/profiles",
                    headers=self.headers,
                    json={"kind": kind, "name": "新模板", "value": "覆盖"},
                ).status_code,
                400,
            )
            atomic_json(
                self.root / "group_chat.json", {"groups": {"10": {kind: {"name": "新模板"}}}}
            )
            response = self.client.post(
                "/api/resource/delete",
                headers=self.headers,
                json={"resource": item["resource"], "revision": item["revision"]},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("正在使用", response.text)
            atomic_json(self.root / "group_chat.json", {"groups": {}})
            self.store.delete_file(item["resource"], item["revision"])
            record = next(
                r
                for r in self.store.changes()
                if r.get("action") == "delete" and r["resource"] == item["resource"]
            )
            restored = self.store.undo(record["id"])
            self.assertEqual(restored["value"], "新的内容")
            with self.assertRaisesRegex(ValueError, "同名"):
                self.store.undo(record["id"])
        default = self.store.get("persona:默认")
        with self.assertRaisesRegex(ValueError, "默认"):
            self.store.delete_file(default["resource"], default["revision"])

    def test_file_mutations_validation_conflicts_and_migration(self):
        for name in ("../越界", "CON", "a/b", "a:b", ".env", "a\\b", "a*"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create_file("persona", name, "内容")
        for value in ("", " " * 10, "字" * 4001, {}):
            with self.assertRaises(ValueError):
                self.store.create_file("persona", "新模板", value)
        item = self.store.create_file("persona", "新模板", "内容")
        self.store.put(item["resource"], "新的内容", item["revision"])
        with self.assertRaisesRegex(ValueError, "重新载入"):
            self.store.delete_file(item["resource"], item["revision"])
        with self.assertRaises(ValueError):
            self.store.delete_file("prompt:prompts.system_prompt.0", "invalid")
        atomic_json(self.root / migration.JOURNAL, {})
        with self.assertRaisesRegex(ValueError, "未恢复"):
            self.store.create_file("style", "新模板", "内容")
        with self.assertRaisesRegex(ValueError, "未恢复"):
            self.store.delete_file(item["resource"], item["revision"])

    def test_undo_created_profile_and_runtime_selection(self):
        from plugins.assistant.groupchat import GroupSettings
        from plugins.assistant.personality import ProfileStore

        item = self.store.create_file("style", "新风格", "新增的表达风格")
        record = next(r for r in self.store.changes() if r.get("action") == "create")
        settings = GroupSettings(self.root / "group_chat.json")
        profiles = ProfileStore(self.root / "personas/默认.txt", self.root / "styles/默认.txt")
        profiles.group_settings = settings
        self.assertIn("新风格", profiles.choices("style"))
        settings.set_profile(10, "style", "新风格")
        self.assertEqual(profiles.effective(10)["style"], "新增的表达风格")
        with self.assertRaisesRegex(ValueError, "正在使用"):
            self.store.undo(record["id"])
        settings.set_profile(10, "style", "默认")
        self.store.undo(record["id"])
        self.assertNotIn("新风格", profiles.choices("style"))
        with self.assertRaisesRegex(ValueError, "移除"):
            settings.set_profile(10, "style", "新风格")
        with self.assertRaises(ValueError):
            self.store.get(item["resource"])

    def test_emote_upload_delete_restore_and_limits(self):
        data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3foAAAAASUVORK5CYII="
        )
        self.assertEqual(
            self.client.post("/api/emotes/upload?name=test.png", content=data).status_code, 403
        )
        for name, content in (
            ("bad.png", b"not an image"),
            ("bad.gif", data),
            ("../bad.png", data),
        ):
            response = self.client.post(
                "/api/emotes/upload", params={"name": name}, content=content, headers=self.headers
            )
            self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post(
            "/api/emotes/upload?name=test.png", content=data, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        self.assertEqual(self.client.get("/api/image?name=test.png").content, data)
        (self.root / "emotes/copy.png").write_bytes(data)
        item = self.store.put(item["resource"], {"description": "共用认知"}, item["revision"])
        self.store.delete_file(item["resource"], item["revision"])
        self.assertEqual(self.store.get("emote:copy.png")["value"]["description"], "共用认知")
        record = next(r for r in self.store.changes() if r.get("action") == "delete")
        self.store.undo(record["id"])
        self.assertEqual((self.root / "emotes/test.png").read_bytes(), data)
        with patch("admin_server.config_for", return_value=Config(emotes_max_bytes=1024)):
            response = self.client.post(
                "/api/emotes/upload?name=large.png",
                content=data + b"x" * 1024,
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 413)

    def test_local_boundary_and_csrf(self):
        self.assertEqual(self.client.get("/api/inventory").status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "attacker.test"}).status_code, 403)
        self.assertEqual(
            self.client.get(
                "/api/session", headers={"Origin": "https://attacker.test"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post("/api/export", json={}).status_code, 403)
        self.assertNotIn("test-key", self.client.get("/api/inventory").text)
        self.assertEqual(self.client.get("/api/image?name=../.env").status_code, 400)

    def test_person_pinned_and_stale_revision(self):
        key = "person:123:20"
        old = self.store.get(key)
        value = old["value"]
        value["总体认知"] = "绘画"
        self.store.put(key, value, old["revision"])
        self.archive.update_facts(
            (123, 10, 20),
            1,
            "喜欢游戏",
            [{"field": "总体认知", "value": "游戏", "evidence": "喜欢游戏"}],
        )
        self.assertEqual(self.store.get(key)["value"]["总体认知"], "绘画")
        with self.assertRaisesRegex(ValueError, "重新载入"):
            self.store.put(key, value, old["revision"])

    def test_person_inventory_unifies_names_and_scopes(self):
        self.archive.collect(
            (123, -20, 20),
            2,
            "我叫小明",
            ["text"],
            101,
            metadata={"sender_name": "小明", "sender_nickname": "小明"},
        )
        self.archive.collect((123, 11, 20), 3, "你好", ["text"], 102)
        people = self.store.inventory()["people"]
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["name"], "小明")
        self.assertEqual(people[0]["scopes"], 3)
        current = self.store.get("person:123:20")
        self.assertEqual(set(current["value"]["会话印象"]), {"10", "11", "-20"})

    def test_two_layers_replace_legacy_and_keep_scene_isolation(self):
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,10,20,'兴趣','游戏','旧资料',1,100)"
            )
        self.archive.collect((123, -20, 20), 2, "私聊", ["text"], 101)
        old = self.store.get("person:123:20")
        self.assertIn("兴趣：游戏", old["value"]["会话印象"]["10"]["补充认知"])
        value = old["value"]
        value["总体认知"] = "游戏"
        value["会话印象"]["10"]["补充认知"] = "组织者"
        value["会话印象"]["-20"] = "只在私聊倾诉"
        saved = self.store.put(old["resource"], value, old["revision"])
        self.assertEqual(
            self.archive.db.execute(
                "SELECT COUNT(*) FROM facts WHERE grp<>0 AND field='兴趣'"
            ).fetchone()[0],
            0,
        )
        current = self.archive.facts((123, 10, 20))
        other = self.archive.facts((123, 11, 20))
        self.assertIn("补充认知：组织者", [r["value"] for r in current])
        self.assertNotIn("只在私聊倾诉", [r["value"] for r in current])
        self.assertNotIn("组织者", [r["value"] for r in other])
        self.assertIn("游戏", [r["value"] for r in other])
        self.assertEqual(saved["value"], value)
        self.store.undo(self.store.changes()[0]["id"])
        self.assertIn(
            "兴趣：游戏",
            self.store.get(old["resource"])["value"]["会话印象"]["10"]["补充认知"],
        )

    def test_person_suggestion_uses_all_own_scopes_without_other_private(self):
        self.archive.collect((123, -20, 20), 2, "本人私聊", ["text"], 101)
        self.archive.collect((123, -30, 30), 3, "其他人私聊", ["text"], 102)
        resource = "person:123:20"
        before = self.store.get(resource)
        proposed = json.loads(json.dumps(before["value"]))
        proposed["总体认知"] = "游戏"
        fake = AsyncMock(
            return_value=(json.dumps({"value": proposed, "explanation": "依据10/1"}), [], [])
        )
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": resource,
                    "revision": before["revision"],
                    "chat": [{"role": "user", "content": "重新整理"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.dumps(fake.call_args.args[0], ensure_ascii=False)
        self.assertIn("本人私聊", sent)
        self.assertNotIn("其他人私聊", sent)
        self.assertNotIn("其他机器人", sent)
        self.assertEqual(self.store.get(resource)["value"], before["value"])
        self.assertEqual(self.store.proposal(resource)["value"], proposed)
        self.save(resource, proposed)
        self.assertIsNone(self.store.proposal(resource))

    def test_person_shape_validation_and_concurrent_new_scope(self):
        old = self.store.get("person:123:20")
        invalid = json.loads(json.dumps(old["value"]))
        invalid["会话印象"]["10"] = {"未知字段": "无效"}
        with self.assertRaises(ValueError):
            self.store.validate(old["resource"], invalid)
        self.archive.collect((123, 11, 20), 2, "新群", ["text"], 101)
        with self.assertRaisesRegex(ValueError, "重新载入"):
            self.store.put(old["resource"], old["value"], old["revision"])

    def test_history_changes_invalidate_knowledge_proposals(self):
        person = self.store.get("person:123:20")
        group = self.store.get("group:123:10")
        for old in (person, group):
            atomic_json(
                self.store.proposal_path(old["resource"]),
                {
                    **old,
                    "history": self.store.history_version(old["resource"]),
                },
            )
        self.archive.collect((123, 10, 20), 2, "新增消息", ["text"], 102)
        self.assertIsNotNone(self.store.proposal(person["resource"]))
        self.assertIsNotNone(self.store.proposal(group["resource"]))
        self.save("message:1", {"content": "更正后的资料"})
        for old in (person, group):
            with self.assertRaisesRegex(ValueError, "重新载入"):
                self.store.put(old["resource"], old["value"], old["revision"])

    def test_malformed_proposal_is_repaired_once_without_saving(self):
        old = self.store.get("person:123:20")
        fake = AsyncMock(
            side_effect=[
                ('{"value":{}},"explanation":"格式错误"}', [], []),
                (json.dumps({"value": old["value"], "explanation": "资料不足"}), [], []),
            ]
        )
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": old["resource"],
                    "revision": old["revision"],
                    "chat": [{"role": "user", "content": "重新整理"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fake.await_count, 2)
        self.assertEqual(self.store.get(old["resource"])["revision"], old["revision"])

    def test_group_knowledge_is_scoped_and_runtime_visible(self):
        key = "group:123:10"
        value = self.store.get(key)["value"]
        value.update(summary="游戏开发群", knowledge="每周五联机", activity=60)
        self.save(key, value)
        current = json.loads(self.archive.context((123, 10, 20), "游戏", set()))
        other = json.loads(self.archive.context((123, 11, 20), "游戏", set()))
        self.assertEqual(current["group_knowledge"]["summary"], "游戏开发群")
        self.assertEqual(other["group_knowledge"], {})
        self.assertEqual(self.store.get(key)["value"]["activity"], 60)

    def test_prompt_hot_reload_and_placeholder_validation(self):
        key = "prompt:prompts.system_prompt.0"
        old = self.store.get(key)
        self.save(key, "当前日期 {today}，新的总体规则。")
        self.assertIn("新的总体规则", system_prompt(False))
        with self.assertRaisesRegex(ValueError, "占位符"):
            self.save(key, "删除日期")
        change = self.store.changes()[0]
        self.store.undo(change["id"])
        self.assertEqual(self.store.get(key)["value"], old["value"])

    def test_message_updates_persistent_turn_and_memory(self):
        memory = Memory(Config(), self.archive)
        memory.save((123, 10, 20), "过期内存", "旧回答")
        self.save("message:1", {"content": "喜欢绘画"})
        self.assertEqual(memory.history((123, 10, 20))[0]["content"], "喜欢绘画")

    def test_model_suggestion_never_saves_without_confirmation(self):
        resource = "group:123:10"
        before = self.store.get(resource)
        proposed = dict(before["value"], summary="大家喜欢游戏")
        fake = AsyncMock(
            return_value=(json.dumps({"value": proposed, "explanation": "根据群记录整理"}), [], [])
        )
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": resource,
                    "revision": before["revision"],
                    "chat": [{"role": "user", "content": "整理摘要"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get(resource)["value"], before["value"])
        response = self.client.post(
            "/api/resource",
            headers=self.headers,
            json={"resource": resource, "revision": before["revision"], "value": proposed},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get(resource)["value"], proposed)

    def test_invalid_model_output_is_not_saved(self):
        old = self.store.get("persona:默认")
        with patch.object(LLM, "step", AsyncMock(return_value=("不是JSON", [], []))):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": "persona:默认",
                    "revision": old["revision"],
                    "chat": [{"role": "user", "content": "改写"}],
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get("persona:默认")["revision"], old["revision"])

    def test_preview_has_actual_messages_without_api_call(self):
        with patch("httpx.AsyncClient.post", side_effect=AssertionError("不得调用模型")):
            response = self.client.post(
                "/api/preview", headers=self.headers, json={"group": 10, "user": 20, "text": "你好"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["messages"][-1]["content"], "你好")
        self.assertEqual(response.json()["messages"][0]["role"], "system")
        self.assertTrue(response.json()["tools"])
        self.assertNotIn("test-key", response.text)

    def test_export_import_includes_console_configuration(self):
        person = self.store.get("person:123:20")["value"]
        person["总体认知"] = "游戏"
        person["会话印象"]["10"]["角色"] = "组织者"
        self.save("person:123:20", person)
        self.save("prompt:search.enabled", "按需检索事实")
        group = self.store.get("group:123:10")["value"]
        self.save("group:123:10", dict(group, summary="群摘要"))
        package = self.root / "export.zip"
        migration.export_data(self.root, package)
        target = self.root / "new"
        target.mkdir()
        migration.import_data(target, package, 123)
        restored = ConsoleStore(target)
        self.assertEqual(restored.get("person:123:20")["value"], person)
        self.assertEqual(restored.get("prompt:search.enabled")["value"], "按需检索事实")
        self.assertEqual(restored.get("group:123:10")["value"]["summary"], "群摘要")

    def test_mutations_blocked_during_migration(self):
        old = self.store.get("persona:默认")
        with migration.project_lock(self.root, name=".migration.lock"):
            with self.assertRaises(ValueError):
                self.store.put("persona:默认", "不能写入", old["revision"])

    def test_platform_names_group_filters_and_legacy_prose(self):
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,0,20,'称呼','模型总结的别名','旧资料',NULL,100)"
            )
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,0,20,'兴趣','摄影','旧资料',NULL,101)"
            )
        atomic_json(
            self.root / "data/contacts.json",
            {
                "123": {
                    "users": {"20": {"name": "当前QQ昵称"}},
                    "groups": {
                        "10": {"name": "游戏交流群", "members": [20]},
                        "11": {"name": "摄影群", "members": [20]},
                    },
                }
            },
        )
        data = self.store.inventory()
        person = data["people"][0]
        self.assertEqual(person["name"], "当前QQ昵称")
        self.assertEqual(person["groups"], [10, 11])
        self.assertEqual(person["sessions"], [{"scope": 10, "name": "游戏交流群"}])
        self.assertEqual({g["name"] for g in data["groups"]}, {"游戏交流群", "摄影群"})
        value = self.store.get(person["resource"])["value"]
        self.assertIsInstance(value["总体认知"], str)
        self.assertIn("称呼：模型总结的别名", value["总体认知"])
        self.assertIn("兴趣：摄影", value["总体认知"])
        (self.root / "data/contacts.json").unlink()
        self.archive.collect(
            (123, 10, 20),
            8,
            "你好",
            ["text"],
            110,
            metadata={"sender_name": "群名片", "sender_nickname": "平台昵称"},
        )
        self.assertEqual(self.store.inventory()["people"][0]["name"], "平台昵称")

    def test_unverified_name_never_becomes_person_title(self):
        self.archive.collect(
            (123, 10, 20),
            99,
            "测试",
            ["text"],
            110,
            metadata={"sender_name": "历史别名"},
        )
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,0,20,'称呼','总结昵称','旧资料',NULL,100)"
            )
        self.assertEqual(self.store.inventory()["people"][0]["name"], "昵称待同步")
        self.assertEqual(self.store.get("person:123:20")["title"], "昵称待同步 · QQ 20")

    def test_scene_edit_preserves_other_sources_and_scoped_suggestion(self):
        self.archive.collect((123, -20, 20), 2, "私聊专用资料", ["text"], 101)
        self.archive.update_facts(
            (123, 10, 20),
            1,
            "喜欢游戏",
            [{"field": "总体认知", "value": "喜欢游戏", "evidence": "喜欢游戏"}],
        )
        old = self.store.get("person:123:20")
        source = self.archive.db.execute("SELECT * FROM facts WHERE grp=0").fetchone()
        scene = dict(old["value"]["会话印象"]["10"], 互动习惯="群内参与讨论")
        proposal = {"value": {"会话印象": {"10": scene}}, "explanation": "依据群发言"}
        fake = AsyncMock(return_value=(json.dumps(proposal), [], []))
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": old["resource"],
                    "revision": old["revision"],
                    "scope": "10",
                    "chat": [{"role": "user", "content": "整理本群印象"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("私聊专用资料", json.dumps(fake.call_args.args[0], ensure_ascii=False))
        value = response.json()["value"]
        self.assertEqual(value["总体认知"], old["value"]["总体认知"])
        self.store.put(old["resource"], value, old["revision"])
        self.assertEqual(
            dict(self.archive.db.execute("SELECT * FROM facts WHERE grp=0").fetchone()),
            dict(source),
        )

    def test_group_categories_preserve_sources_and_pin_independently(self):
        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,10,20,'角色','组织者','本人说明',1,100)"
            )
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,10,20,'会话印象','原有整段印象','本人说明',1,101)"
            )
        before = self.store.get("person:123:20")
        scene = before["value"]["会话印象"]["10"]
        self.assertEqual(set(scene), {"角色", "互动习惯", "互动关系", "补充认知"})
        self.assertEqual(scene["角色"], "组织者")
        self.assertEqual(scene["补充认知"], "原有整段印象")
        source = dict(self.archive.db.execute("SELECT * FROM facts WHERE field='角色'").fetchone())
        scene["互动习惯"] = "固定修正"
        self.store.put(before["resource"], before["value"], before["revision"])
        self.assertEqual(
            source,
            dict(self.archive.db.execute("SELECT * FROM facts WHERE field='角色'").fetchone()),
        )
        self.archive.update_facts(
            (123, 10, 20),
            1,
            "喜欢游戏",
            [
                {"field": "互动习惯", "value": "不应覆盖", "evidence": "喜欢游戏"},
                {"field": "互动关系", "value": "一起游戏", "evidence": "喜欢游戏"},
            ],
        )
        current = self.store.get(before["resource"])["value"]["会话印象"]["10"]
        self.assertEqual(current["互动习惯"], "固定修正")
        self.assertEqual(current["互动关系"], "一起游戏")
        self.archive.remember((123, 10, 20), "角色", "参与者")
        self.assertEqual(
            self.store.get(before["resource"])["value"]["会话印象"]["10"]["互动习惯"], "固定修正"
        )

        self.archive.forget((123, 10, 20), "补充认知")
        scene = self.store.get(before["resource"])["value"]["会话印象"]["10"]
        self.assertEqual(scene["补充认知"], "")
        self.assertEqual(scene["角色"], "参与者")
