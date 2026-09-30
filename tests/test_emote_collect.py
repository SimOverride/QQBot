"""Autonomous emote collection without live network or QQ sends."""

import base64
import json
import socket
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from plugins.assistant.config import Config
from plugins.assistant.emote_collect import EmoteCollector
from plugins.assistant.emotes import Emotes
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.http import ServiceError
from plugins.assistant.image_download import _download, public_target
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII="
)


class CollectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Emotes(self.root / "emotes", daily_limit=1)
        self.review = AsyncMock(
            return_value={"eligible": True, "description": "白色图案，情绪不明"}
        )
        self.download = AsyncMock(return_value=PNG)
        self.collector = EmoteCollector(self.library, self.review, self.download)
        self.ident = self.collector.add("https://images.example/a.png", "group:10")

    def tearDown(self):
        self.temp.cleanup()

    async def test_view_save_memory_dedup_and_rate_limit(self):
        self.assertEqual(self.collector.save(self.ident)["status"], "not_reviewed_or_ineligible")
        result, preview = await self.collector.inspect(self.ident)
        self.assertTrue(result["eligible"])
        self.assertEqual(preview[-1]["type"], "image_url")
        saved = self.collector.save(self.ident)
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(self.library.recall(saved["digest"]), "白色图案，情绪不明")
        self.assertEqual(self.collector.save(self.ident)["status"], "already_saved")
        again = EmoteCollector(self.library, self.review, self.download)
        ident = again.add("https://other.example/same.png", "web:other")
        _, previews = await again.inspect(ident)
        self.assertEqual(previews, [])
        self.review.assert_awaited_once()
        self.assertEqual(again.save(ident)["status"], "already_saved")
        self.assertEqual(len(self.library.files()), 1)
        changed = PNG + b"different"
        self.assertEqual(
            self.library.import_image(changed, ".png", "another", "web:x")["status"],
            "library_limit",
        )
        self.assertEqual(len(self.library.files()), 1)

    async def test_rejects_unreviewed_private_and_nonimage(self):
        self.assertEqual(
            (await self.collector.inspect("invented"))[0]["status"], "unknown_candidate"
        )
        self.review.return_value = {"eligible": False, "description": "私人照片，不适合收藏"}
        await self.collector.inspect(self.ident)
        self.assertEqual(self.collector.save(self.ident)["status"], "not_reviewed_or_ineligible")
        self.assertEqual(self.library.files(), {})
        other = self.collector.add("https://images.example/html", "web:html")
        self.download.return_value = b"<html>not an image</html>"
        with self.assertRaises(ServiceError):
            await self.collector.inspect(other)
        self.review.assert_awaited_once()

    async def test_group_planner_can_collect_then_remain_silent(self):
        cfg = Config()
        archive = LongTermMemory(self.root / "archive.db", cfg)
        tools = HistoryTools(archive, (99, 10, 2))
        tools.emotes = self.library

        def call(name, args, cid):
            return ("", [{"name": name, "arguments": json.dumps(args), "call_id": cid}], [])

        images = [{"type": "image_url", "image_url": {"url": "https://gchat.qpic.cn/image.png"}}]
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500))
            ) as client:
                llm = LLM(client, cfg, Search(client, cfg))
                llm.step = AsyncMock(
                    side_effect=[
                        call("emote_candidates", {}, "a"),
                        call("inspect_emote_image", {"id": "image-1"}, "b"),
                        ('{"eligible":true,"description":"卡通反应图，表达惊讶"}', [], []),
                        call("save_emote", {"id": "image-1"}, "c"),
                        ('{"reply":false,"ended":false}', [], []),
                    ]
                )

                # Patch only the network transport used by the candidate registry.
                def factory(library, review):
                    return EmoteCollector(library, review, self.download)

                with patch("plugins.assistant.llm.EmoteCollector", side_effect=factory):
                    decision = await llm.plan_participation(
                        [], {}, {}, {}, 1, False, 99, tools, images=images
                    )
                self.assertFalse(decision["reply"])
                self.assertEqual(len(self.library.files()), 1)
                name = next(iter(self.library.files()))
                self.assertIn("惊讶", self.library.recall(self.library.digest(name)))
        finally:
            archive.close()

    async def test_private_images_are_not_exposed_to_collection(self):
        archive = LongTermMemory(self.root / "archive.db", Config())
        tools = HistoryTools(archive, (99, -2, 2))
        tools.emotes = self.library
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500))
            ) as client:
                llm = LLM(client, Config(), Search(client, Config()))
                llm.step = AsyncMock(
                    side_effect=[
                        ("", [{"name": "emote_candidates", "arguments": "{}", "call_id": "a"}], []),
                        ("没有群聊候选", [], []),
                    ]
                )
                await llm.reply(
                    [],
                    "看看",
                    "test",
                    history_tools=tools,
                    images=[
                        {"type": "image_url", "image_url": {"url": "https://gchat.qpic.cn/private"}}
                    ],
                )
                messages = llm.step.call_args.args[0]
                outputs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
                self.assertEqual(outputs[-1]["candidates"], [])
        finally:
            archive.close()

    async def test_search_returns_real_candidates(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "images": [
                        {"url": "https://example.com/image.png", "description": "卡通鲸鱼"},
                        {"url": "file:///secret", "description": "invalid"},
                    ],
                    "results": [],
                },
            )

        cfg = Config(search_api_key="test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await Search(client, cfg).images("鲸鱼 表情包", "test")
        self.assertEqual(len(result), 1)
        self.assertTrue(requests[0]["include_images"])
        self.assertEqual(result[0]["description"], "卡通鲸鱼")

    async def test_online_search_inspect_save_and_send_selection(self):
        import hashlib

        cfg = Config(search_api_key="test-key")
        archive = LongTermMemory(self.root / "archive.db", cfg)
        tools = HistoryTools(archive, (99, -2, 2))
        tools.emotes = self.library
        saved_name = "收藏_" + hashlib.sha256(PNG).hexdigest() + ".png"

        def call(name, args, cid):
            return ("", [{"name": name, "arguments": json.dumps(args), "call_id": cid}], [])

        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500))
            ) as client:
                search = Search(client, cfg)
                search.images = AsyncMock(
                    return_value=[
                        {
                            "url": "https://example.com/image.png?temporary=secret",
                            "description": "反应表情",
                        }
                    ]
                )
                llm = LLM(client, cfg, search)
                llm.step = AsyncMock(
                    side_effect=[
                        call("search_emote_images", {"query": "鲸鱼娘 开心 表情包"}, "a"),
                        call("inspect_emote_image", {"id": "image-1"}, "b"),
                        ('{"eligible":true,"description":"卡通反应表情，表达开心"}', [], []),
                        call("save_emote", {"id": "image-1"}, "c"),
                        call("choose_emote", {"id": saved_name}, "d"),
                        ("", [], []),
                    ]
                )

                def factory(library, review):
                    return EmoteCollector(library, review, self.download)

                with patch("plugins.assistant.llm.EmoteCollector", side_effect=factory):
                    result = await llm.reply([], "找一个开心的表情", "test", history_tools=tools)
                self.assertEqual(result.emote_id, saved_name)
                search.images.assert_awaited_once()
                import sqlite3

                with closing(sqlite3.connect(self.library.memory_path)) as db:
                    source = db.execute("SELECT source FROM emote_imports").fetchone()[0]
                self.assertEqual(source, "web:https://example.com/image.png")
        finally:
            archive.close()


class DownloadTests(unittest.TestCase):
    def resolve(self, address):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def test_private_addresses_and_credentials_rejected(self):
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "224.0.0.1"):
            with patch(
                "plugins.assistant.image_download.socket.getaddrinfo", return_value=self.resolve(ip)
            ):
                with self.assertRaises(ServiceError):
                    public_target("https://example.com/image")
        for url in (
            "file:///secret",
            "https://user:password@example.com/x",
            "http://example.com:22/x",
        ):
            with self.assertRaises(ServiceError):
                public_target(url)

    def test_redirect_to_private_network_is_rejected(self):
        from unittest.mock import MagicMock

        response = MagicMock(status=302)
        response.getheader.return_value = "http://127.0.0.1/secret"
        connection = MagicMock()
        connection.getresponse.return_value = response

        def resolve(host, *args, **kwargs):
            return self.resolve("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34")

        with (
            patch("plugins.assistant.image_download.socket.getaddrinfo", side_effect=resolve),
            patch(
                "plugins.assistant.image_download.http.client.HTTPSConnection",
                return_value=connection,
            ),
        ):
            with self.assertRaises(ServiceError):
                _download("https://example.com/image", 1000)
        connection.close.assert_called_once()

    def test_oversized_stream_rejected_even_without_header(self):
        from unittest.mock import MagicMock

        response = MagicMock(status=200)
        response.getheader.return_value = None
        response.read.return_value = b"x" * 11
        connection = MagicMock()
        connection.getresponse.return_value = response
        with (
            patch(
                "plugins.assistant.image_download.socket.getaddrinfo",
                return_value=self.resolve("93.184.216.34"),
            ),
            patch(
                "plugins.assistant.image_download.http.client.HTTPSConnection",
                return_value=connection,
            ),
        ):
            with self.assertRaises(ServiceError):
                _download("https://example.com/image", 10)
        connection.close.assert_called_once()
