"""后台端到端接口与运行时生效验证，不调用真实模型。"""

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

    def save(self, resource, value):
        before = self.store.get(resource)
        return self.store.put(resource, value, before["revision"])

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
        key = "person:123:10:20"
        old = self.store.get(key)
        value = dict(old["value"], 兴趣="绘画")
        self.store.put(key, value, old["revision"])
        self.archive.update_facts(
            (123, 10, 20),
            1,
            "喜欢游戏",
            [{"field": "兴趣", "value": "游戏", "evidence": "喜欢游戏"}],
        )
        self.assertEqual(self.store.get(key)["value"]["兴趣"], "绘画")
        with self.assertRaisesRegex(ValueError, "重新载入"):
            self.store.put(key, value, old["revision"])

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
        self.save("prompt:search.enabled", "按需检索事实")
        group = self.store.get("group:123:10")["value"]
        self.save("group:123:10", dict(group, summary="群摘要"))
        package = self.root / "export.zip"
        migration.export_data(self.root, package)
        target = self.root / "new"
        target.mkdir()
        migration.import_data(target, package, 123)
        restored = ConsoleStore(target)
        self.assertEqual(restored.get("prompt:search.enabled")["value"], "按需检索事实")
        self.assertEqual(restored.get("group:123:10")["value"]["summary"], "群摘要")

    def test_mutations_blocked_during_migration(self):
        old = self.store.get("persona:默认")
        with migration.project_lock(self.root, name=".migration.lock"):
            with self.assertRaises(ValueError):
                self.store.put("persona:默认", "不能写入", old["revision"])
