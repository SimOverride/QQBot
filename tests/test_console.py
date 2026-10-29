"""后台端到端接口与运行时生效验证，不调用真实模型。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import migration
from admin.console_store import ConsoleStore, atomic_json
from admin_server import create_app
from plugins.assistant import prompt_store
from plugins.assistant.config import Config
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.prompts import system_prompt
from plugins.assistant.sessions import epoch
from plugins.assistant.vault import FIELDS, Vault, source
from tests.memory_support import write_memory


class ConsoleTests(unittest.TestCase):
    def test_reset_all_requires_confirmation_and_preserves_data(self):
        before = epoch(self.root, 123, 10)
        files = {}
        for name in (
            "personas/默认.txt",
            "data/task-marker.json",
            "data/agent/123/10/old/session.json",
            "data/agent-events/123/10.json",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("[]", encoding="utf-8")
            files[path] = path.read_bytes()
        messages = self.archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(
            self.client.post("/api/agent/reset", json={"confirm": "reset_all"}).status_code, 403
        )
        self.assertEqual(
            self.client.post("/api/agent/reset", json={}, headers=self.headers).status_code, 400
        )
        self.assertEqual(epoch(self.root, 123, 10), before)
        result = self.client.post(
            "/api/agent/reset", json={"confirm": "reset_all"}, headers=self.headers
        )
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(epoch(self.root, 123, 10), before)
        self.assertNotEqual(epoch(self.root, 88, -20)[0], "initial")
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], messages
        )
        for path, content in files.items():
            self.assertEqual(path.read_bytes(), content)
        previous = epoch(self.root, 123, 10)
        self.assertEqual(
            self.client.post(
                "/api/agent/reset", json={"confirm": "reset_all"}, headers=self.headers
            ).status_code,
            200,
        )
        self.assertNotEqual(epoch(self.root, 123, 10), previous)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("personas", "emotes", "data"):
            (self.root / folder).mkdir()
        for folder in ("personas",):
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

    def test_emote_experience_is_shared_and_old_restore_endpoint_removed(self):
        from plugins.assistant.emotes import Emotes
        from tests.test_emote_collect import PNG

        library = Emotes(self.root / "emotes")
        (library.root / "test.png").write_bytes(PNG)
        note = Vault(self.root, 123).put(
            {
                "kind": "emote",
                "subject": 20,
                "field": "表情反馈",
                "image_digest": library.digest("test.png"),
                "value": "该用户认为严肃讨论不适用",
                "fixed": True,
            },
            manual=True,
        )
        response = self.client.get("/api/resource", params={"id": "emote:test.png"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["experiences"][0]["id"], note["id"])
        self.assertNotIn("group_states", response.json())
        self.assertEqual(
            self.client.post("/api/emotes/restore", json={}, headers=self.headers).status_code, 404
        )

    def test_message_reference_preview_is_ordered_and_scoped(self):
        self.archive.collect((123, 10, 123), 9, "机器人回复", ["text"], 110, related_user=20)
        self.archive.collect((123, 11, 30), 8, "其他会话", ["text"], 90)
        with self.store.db() as db:
            rows = db.execute("SELECT id,grp FROM messages ORDER BY id").fetchall()
        local = [r["id"] for r in rows if r["grp"] == 10]
        foreign = next(r["id"] for r in rows if r["grp"] == 11)
        response = self.client.get(
            "/api/referenced-messages",
            params={
                "resource": "person:123:20",
                "scope": 10,
                "ids": ",".join(map(str, [*reversed(local), foreign, 999999])),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual({r["id"] for r in data["messages"]}, set(local))
        self.assertEqual(
            [r["time"] for r in data["messages"]], sorted(r["time"] for r in data["messages"])
        )
        self.assertEqual(set(data["missing"]), {foreign, 999999})
        self.assertTrue(any(r["is_self"] for r in data["messages"]))
        self.assertTrue(all(r["scope"] == 10 for r in data["messages"]))
        for params in (
            {"ids": "1", "scope": 11},
            {"ids": "bad"},
            {"ids": ",".join(map(str, range(1, 102)))},
            {"ids": str(2**63)},
        ):
            result = self.client.get(
                "/api/referenced-messages", params={"resource": "person:123:20", **params}
            )
            self.assertEqual(result.status_code, 400)

    def test_session_service_identifier(self):
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "qqbot-console")

    def test_unified_persona_and_removed_style(self):
        self.assertNotIn("style", self.store.get("group:123:10")["value"])
        response = self.client.post(
            "/api/profiles",
            headers=self.headers,
            json={"kind": "style", "name": "旧风格", "value": "内容"},
        )
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(ValueError):
            self.store.get("style:默认")
        content = "角色与说话方式" * 1000
        item = self.store.create_file("persona", "合并人设", content)
        self.assertEqual(item["value"], content)
        self.assertFalse(migration.allowed("styles/默认.txt"))

    def save(self, resource, value):
        before = self.store.get(resource)
        return self.store.put(resource, value, before["revision"])

    def test_profile_create_delete_restore_and_references(self):
        for kind in ("persona",):
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
            atomic_json(self.root / "group_chat.json", {"default_persona": "新模板", "groups": {}})
            response = self.client.post(
                "/api/resource/delete",
                headers=self.headers,
                json={"resource": item["resource"], "revision": item["revision"]},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("全局", response.text)
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
        for value in ("", " " * 10, "字" * 12001, {}):
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
            self.store.create_file("persona", "新模板", "内容")
        with self.assertRaisesRegex(ValueError, "未恢复"):
            self.store.delete_file(item["resource"], item["revision"])

    def test_undo_created_profile_and_runtime_selection(self):
        from plugins.assistant.groupchat import GroupSettings
        from plugins.assistant.personality import ProfileStore

        item = self.store.create_file("persona", "新人设", "新增的人设与表达方式")
        record = next(r for r in self.store.changes() if r.get("action") == "create")
        settings = GroupSettings(self.root / "group_chat.json")
        profiles = ProfileStore(self.root / "personas/默认.txt")
        profiles.group_settings = settings
        self.assertIn("新人设", profiles.choices("persona"))
        settings.write({"default_persona": "新人设", "groups": {}})
        self.assertEqual(profiles.effective(10)["persona"], "新增的人设与表达方式")
        self.assertEqual(profiles.effective(-20)["persona"], "新增的人设与表达方式")
        with self.assertRaisesRegex(ValueError, "全局"):
            self.store.undo(record["id"])
        settings.write({"default_persona": "默认", "groups": {}})
        self.store.undo(record["id"])
        self.assertNotIn("新人设", profiles.choices("persona"))
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
        write_memory(
            self.archive,
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
        self.assertEqual(set(current["value"]), set(FIELDS))
        self.assertIsInstance(current["value"]["会话印象"], str)

    def test_legacy_upgrade_and_cross_scope_visibility(self):
        from memory_upgrade import upgrade

        with self.archive.db:
            self.archive.db.execute(
                "INSERT INTO facts VALUES(123,10,20,'总体认知','游戏','旧资料',1,100)"
            )
        self.archive.close()
        upgrade(self.root, apply=True)
        self.archive = LongTermMemory(self.root / migration.MEMORY, Config())
        self.addCleanup(self.archive.close)
        old = self.store.get("person:123:20")
        self.assertIn("游戏", old["value"]["总体认知"])
        value = {**old["value"], "角色": "组织者", "会话印象": "曾在私聊倾诉"}
        saved = self.store.put(old["resource"], value, old["revision"])
        for scope in (10, 11, -20):
            values = [r["value"] for r in self.archive.facts((123, scope, 20))]
            self.assertIn("组织者", values)
            self.assertIn("曾在私聊倾诉", values)
        self.assertEqual(saved["value"], value)
        self.store.undo(self.store.changes()[0]["id"])
        self.assertEqual(self.store.get(old["resource"])["value"], old["value"])

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

    def test_person_shape_validation_and_concurrent_memory_update(self):
        old = self.store.get("person:123:20")
        invalid = {**old["value"], "会话印象": {"10": "旧结构"}}
        with self.assertRaises(ValueError):
            self.store.validate(old["resource"], invalid)
        self.archive.remember((123, 11, 20), "总体认知", "新认知")
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
        with self.archive.db:
            self.archive.db.execute("UPDATE messages SET content=? WHERE id=1", ("更正后的资料",))
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
        self.assertEqual(set(value), {"min_participants"})
        self.store.create_memory(123, "context", 0, "10:summary", "游戏开发群")
        self.store.create_memory(123, "context", 0, "10:knowledge", "每周五联机")
        current = json.loads(self.archive.context((123, 10, 20), "游戏", set()))
        other = json.loads(self.archive.context((123, 11, 20), "游戏", set()))
        self.assertEqual(current["group_knowledge"]["summary"], "游戏开发群")
        self.assertEqual(other["group_knowledge"], {})
        self.assertIn("游戏开发群", [n["value"] for n in other["unified_memory"]])
        self.assertNotIn("activity", self.store.get(key)["value"])

    def test_prompt_hot_reload_and_placeholder_validation(self):
        key = "module:chat"
        old = self.store.get(key)
        value = json.loads(json.dumps(old["value"]))
        value["prompts"]["prompts.system_prompt.0"] = "当前日期 {today}，新的总体规则。"
        self.save(key, value)
        self.assertIn("新的总体规则", system_prompt(False))
        with self.assertRaisesRegex(ValueError, "占位符"):
            value["prompts"]["prompts.system_prompt.0"] = "删除日期"
            self.save(key, value)
        change = self.store.changes()[0]
        self.store.undo(change["id"])
        self.assertEqual(self.store.get(key)["value"], old["value"])

    def test_module_inventory_and_model_suggestion_keep_contract_readonly(self):
        inventory = self.client.get("/api/inventory").json()
        self.assertEqual(len(inventory["modules"]), 8)
        self.assertNotIn("prompts", inventory)
        before = self.store.get("module:history")
        proposed = json.loads(json.dumps(before["value"]))
        proposed["tools"]["search_messages"]["description"] = "根据上下文查找必要记录"
        fake = AsyncMock(
            return_value=(json.dumps({"value": proposed, "explanation": "修改说明"}), [], [])
        )
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": "module:history",
                    "revision": before["revision"],
                    "chat": [{"role": "user", "content": "简化历史查询说明"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(fake.call_args.args[0][1]["content"])
        self.assertEqual(sent["current"], before["value"])
        self.assertIn("parameters", sent["module_contract"]["tools"]["search_messages"])
        self.assertEqual(self.store.get("module:history")["value"], before["value"])
        proposed["tools"]["search_messages"]["schema"] = {"required": []}
        with self.assertRaises(ValueError):
            self.save("module:history", proposed)

    def test_message_management_removed(self):
        self.assertEqual(self.client.get("/api/messages").status_code, 404)
        with self.assertRaisesRegex(ValueError, "不支持的资源"):
            self.store.get("message:1")
        self.assertTrue(self.store.messages())

    def test_model_suggestion_never_saves_without_confirmation(self):
        created = self.store.create_memory(123, "context", 0, "10:summary", "游戏交流群")
        resource = created["resource"]
        before = self.store.get(resource)
        proposed = dict(before["value"], content="大家喜欢游戏")
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
        person["角色"] = "组织者"
        self.save("person:123:20", person)
        search = self.store.get("module:search")["value"]
        search["prompts"]["search.enabled"] = "按需检索事实"
        self.save("module:search", search)
        background = self.store.create_memory(123, "context", 0, "10:summary", "群背景")
        package = self.root / "export.zip"
        migration.export_data(self.root, package)
        target = self.root / "new"
        target.mkdir()
        migration.import_data(target, package, 123)
        restored = ConsoleStore(target)
        self.assertEqual(restored.get("person:123:20")["value"], person)
        self.assertEqual(
            restored.get("module:search")["value"]["prompts"]["search.enabled"], "按需检索事实"
        )
        self.assertEqual(restored.get(background["resource"])["value"]["content"], "群背景")

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
        self.assertNotIn("sessions", person)
        self.assertEqual({g["name"] for g in data["groups"]}, {"游戏交流群", "摄影群"})
        from memory_upgrade import upgrade

        self.archive.close()
        upgrade(self.root, apply=True)
        self.archive = LongTermMemory(self.root / migration.MEMORY, Config())
        self.addCleanup(self.archive.close)
        notes = Vault(self.root, 123).list(subject=20, history=True)
        self.assertIn("模型总结的别名", [n["value"] for n in notes])
        self.assertIn("摄影", [n["value"] for n in notes])
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
        from memory_upgrade import upgrade

        self.archive.close()
        upgrade(self.root, apply=True)
        self.archive = LongTermMemory(self.root / migration.MEMORY, Config())
        self.addCleanup(self.archive.close)
        self.assertEqual(self.store.get("person:123:20")["title"], "昵称待同步 · QQ 20")

    def test_unified_suggestion_preserves_unchanged_fields(self):
        self.archive.collect((123, -20, 20), 2, "私聊资料", ["text"], 101)
        write_memory(
            self.archive,
            (123, 10, 20),
            1,
            "喜欢游戏",
            [{"field": "总体认知", "value": "喜欢游戏", "evidence": "喜欢游戏"}],
        )
        old = self.store.get("person:123:20")
        vault = Vault(self.root, 123)
        original = vault.field(20, "总体认知")
        proposal = {"value": {**old["value"], "互动习惯": "参与讨论"}, "explanation": "依据发言"}
        fake = AsyncMock(return_value=(json.dumps(proposal), [], []))
        with patch.object(LLM, "step", fake):
            response = self.client.post(
                "/api/suggest",
                headers=self.headers,
                json={
                    "resource": old["resource"],
                    "revision": old["revision"],
                    "chat": [{"role": "user", "content": "整理统一认知"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("私聊资料", json.dumps(fake.call_args.args[0], ensure_ascii=False))
        self.store.put(old["resource"], response.json()["value"], old["revision"])
        self.assertEqual(vault.field(20, "总体认知"), original)

    def test_unified_categories_preserve_sources_and_pin_independently(self):
        vault = Vault(self.root, 123)
        row = self.archive.db.execute("SELECT * FROM messages WHERE id=1").fetchone()
        original = vault.set_field(20, "角色", "组织者", sources=[source(row)])
        before = self.store.get("person:123:20")
        before["value"]["互动习惯"] = "固定修正"
        self.store.put(before["resource"], before["value"], before["revision"])
        self.assertEqual(vault.field(20, "角色"), original)
        write_memory(
            self.archive,
            (123, 10, 20),
            1,
            "喜欢游戏",
            [
                {"field": "互动习惯", "value": "不应覆盖", "evidence": "喜欢游戏"},
                {"field": "互动关系", "value": "一起游戏", "evidence": "喜欢游戏"},
            ],
        )
        current = self.store.get(before["resource"])["value"]
        self.assertEqual(current["互动习惯"], "固定修正")
        self.assertEqual(current["互动关系"], "一起游戏")
        self.archive.remember((123, 11, 20), "角色", "参与者")
        self.archive.forget((123, -20, 20), "补充认知")
        current = self.store.get(before["resource"])["value"]
        self.assertEqual(current["角色"], "参与者")
        self.assertEqual(current["补充认知"], "")
        self.assertEqual(current["互动习惯"], "固定修正")
