"""独立后台的在线、断线、停止与过期状态。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from admin_server import create_app
from console_store import atomic_json
from plugins.assistant.runtime import Runtime, status


class RuntimeTests(unittest.TestCase):
    def test_avatar_uses_fixed_service_and_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Runtime(root).publish(["123"])
            calls = []

            def handle(request):
                calls.append(request)
                return httpx.Response(200, content=b"image", headers={"content-type": "image/png"})

            client_type = httpx.AsyncClient

            def client(**kwargs):
                return client_type(transport=httpx.MockTransport(handle), **kwargs)

            with TestClient(create_app(root), base_url="http://127.0.0.1:8090") as browser:
                with patch("admin_server.httpx.AsyncClient", client):
                    self.assertEqual(browser.get("/api/bot-avatar?qq=999").content, b"image")
                    self.assertEqual(browser.get("/api/bot-avatar").status_code, 200)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].url.host, "q1.qlogo.cn")
                self.assertEqual(calls[0].url.params["nk"], "123")

    def test_lifecycle_and_cached_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(status(root)["state"], "stopped")
            runtime = Runtime(root)
            runtime.publish([])
            self.assertEqual(status(root)["state"], "waiting")
            runtime.publish(["123"])
            atomic_json(
                root / "data/contacts.json", {"123": {"users": {"123": {"name": "测试机器人"}}}}
            )
            online = status(root)
            self.assertEqual(online["state"], "connected")
            self.assertEqual(online["nickname"], "测试机器人")
            expired = status(root, now=runtime.started + 30)
            self.assertEqual(expired["state"], "stale")
            self.assertEqual(expired["connected_count"], 0)
            runtime.publish([])
            self.assertEqual(status(root)["state"], "waiting")
            self.assertEqual(status(root)["qq"], "123")
            runtime.publish([], stopped=True)
            self.assertEqual(status(root)["state"], "stopped")
            self.assertIsNone(status(root)["uptime"])
            self.assertEqual(status(root)["nickname"], "测试机器人")
