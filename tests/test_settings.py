"""设置、密钥替换、默认继承与插件生命周期使用隔离目录验证。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from dotenv import dotenv_values
from fastapi.testclient import TestClient

import migration
from admin_server import create_app
from console_settings import Settings
from console_store import ConsoleStore
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupSettings
from plugins.assistant.llm import LLM
from plugins.assistant.personality import ProfileStore
from plugins.extensions.store import Store


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("personas",):
            (self.root / directory).mkdir()
            (self.root / directory / "默认.txt").write_text("默认正文", encoding="utf-8")
        (self.root / "personas/温和.txt").write_text("温和正文", encoding="utf-8")
        (self.root / ".env").write_text(
            "# 保留注释\nHOST=127.0.0.1\nDEEPSEEK_API_KEY=old-private-key\nDEEPSEEK_MODEL=test\n",
            encoding="utf-8",
        )
        self.settings = Settings(self.root)
        self.client = TestClient(create_app(self.root), base_url="http://127.0.0.1:8090")
        self.addCleanup(self.client.close)
        self.headers = {"X-Console-Token": self.client.get("/api/session").json()["token"]}

    def values(self):
        data = self.settings.env()
        return data, {f["key"]: f["value"] for g in data["groups"] for f in g["fields"]}

    def seed_plugins(self):
        store = Store(self.root)
        value = store.read()
        value["bots"]["123"] = {
            "schedule": {"enabled": True, "scope": None, "groups": {"10": True}, "data": {}},
            "gen_0123456789abcdef": {
                "enabled": True,
                "scope": ["10"],
                "groups": {"10": True},
                "data": {
                    "10": {
                        "_runtime": {
                            "title": "测试插件",
                            "description": "说明",
                            "version": "a" * 64,
                        },
                        "state": {},
                    }
                },
            },
        }
        store.write(value)
        folder = self.root / "data/generated/gen_0123456789abcdef" / ("a" * 64)
        folder.mkdir(parents=True)
        (folder / "plugin.py").write_text("# 测试文件", encoding="utf-8")
        return store

    def test_env_secrets_version_validation_and_preservation(self):
        data, values = self.values()
        self.assertNotIn("old-private-key", json.dumps(data))
        values["session_max_turns"] = 16
        values["llm_thinking_enabled"] = True
        result = self.client.post(
            "/api/settings",
            headers=self.headers,
            json={
                "values": values,
                "secrets": {"deepseek_api_key": "new-private-key"},
                "revision": data["revision"],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("new-private-key", result.text)
        saved = dotenv_values(self.root / ".env")
        self.assertEqual(saved["DEEPSEEK_API_KEY"], "new-private-key")
        self.assertEqual(saved["HOST"], "127.0.0.1")
        self.assertIn("# 保留注释", (self.root / ".env").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.settings.save_env(values, {}, data["revision"])
        data, values = self.values()
        values["session_max_turns"] = 0
        with self.assertRaises(ValueError):
            self.settings.save_env(values, {}, data["revision"])
        self.assertEqual(self.settings.env_revision(), data["revision"])
        self.assertEqual(self.client.post("/api/settings", json={}).status_code, 403)

    def test_default_persona_inherit_and_protect(self):
        before = self.settings.defaults()
        self.settings.save_defaults("温和", before["revision"])
        groups = GroupSettings(self.root / "group_chat.json")
        self.assertNotIn("activity", groups.get(10))
        profiles = ProfileStore(self.root / "personas/默认.txt")
        profiles.group_settings = groups
        self.assertEqual(profiles.effective(10)["persona"], "温和正文")
        self.assertEqual(profiles.effective(-20)["persona"], "温和正文")
        store = ConsoleStore(self.root)
        item = store.get("persona:温和")
        with self.assertRaisesRegex(ValueError, "全局默认"):
            store.delete_file(item["resource"], item["revision"])

    def test_group_toggle_does_not_invalidate_other_group(self):
        store = self.seed_plugins()
        before = store.switch_time(123, "schedule", 20)
        self.settings.switch(123, "schedule", False, self.settings.plugins()["revision"], 10)
        self.assertEqual(store.switch_time(123, "schedule", 20), before)
        self.assertGreater(store.switch_time(123, "schedule", 10), before)

    def test_plugin_switch_scope_cas_and_offline_batch_delete(self):
        store = self.seed_plugins()
        stale = store.read()
        listing = self.settings.plugins(123, 10)
        self.settings.switch(123, "schedule", False, listing["revision"])
        self.assertFalse(store.read()["bots"]["123"]["schedule"]["enabled"])
        with self.assertRaisesRegex(ValueError, "已变化"):
            store.write(stale)
        listing = self.settings.plugins()
        with self.assertRaisesRegex(ValueError, "不适用"):
            self.settings.switch(123, "gen_0123456789abcdef", False, listing["revision"], 11)
        self.settings.switch(123, "gen_0123456789abcdef", False, listing["revision"], 10)
        listing = self.settings.plugins()
        selected = [{"bot": "123", "id": row["id"]} for row in listing["items"]]
        with migration.project_lock(self.root):
            with self.assertRaises(ValueError):
                self.settings.delete_plugins(selected, listing["revision"])
        self.assertEqual(len(self.settings.plugins()["items"]), 2)
        self.settings.delete_plugins(selected, listing["revision"])
        self.assertEqual(self.settings.plugins()["items"], [])
        self.assertFalse((self.root / "data/generated/gen_0123456789abcdef").exists())
        self.assertTrue(list((self.root / "backups/plugins").glob("*/plugins.json")))


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_toggle_changes_provider_payload(self):
        for provider in ("deepseek", "openai"):
            for enabled in (False, True):
                config = Config(
                    llm_provider=provider,
                    deepseek_model="test",
                    openai_model="gpt-5.2",
                    llm_thinking_enabled=enabled,
                    model_trace_enabled=False,
                )
                response = (
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "完成"},
                            }
                        ]
                    }
                    if provider == "deepseek"
                    else {"status": "completed", "output": []}
                )
                async with httpx.AsyncClient() as client:
                    llm = LLM(client, config, None)
                    with patch(
                        "plugins.assistant.llm.post_json", AsyncMock(return_value=response)
                    ) as call:
                        await llm.step([], False, "test", [])
                    payload = call.call_args.args[3]
                    if provider == "deepseek":
                        self.assertEqual(
                            payload["thinking"]["type"], "enabled" if enabled else "disabled"
                        )
                    else:
                        self.assertEqual(
                            payload["reasoning"]["effort"], "high" if enabled else "none"
                        )
