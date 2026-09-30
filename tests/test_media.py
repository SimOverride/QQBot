"""Image understanding and local emote tests; no live API or QQ traffic."""

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import extract_text
from plugins.assistant.config import Config
from plugins.assistant.emotes import Emotes
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.http import ServiceError
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search
from plugins.assistant.service import Assistant
from plugins.assistant.vision import Vision, image_segments, platform_url

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII="
)


def picture(url="https://multimedia.nt.qq.com.cn/a"):
    return MessageSegment("image", {"file": "image-id", "url": url})


def event():
    return SimpleNamespace(
        original_message=Message([picture("https://gchat.qpic.cn/a")]),
        reply=None,
        message_id=1,
        user_id=2,
    )


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_needs_no_image_input(self):
        vision = Vision(Config())
        self.assertEqual(
            await vision.prepare(None, SimpleNamespace(original_message=Message("hi"))), []
        )

    async def test_native_payload_uses_chat_model_and_key(self):
        current = event()
        current.reply = SimpleNamespace(message=Message([picture("https://gchat.qpic.cn/b")]))
        for provider in ("deepseek", "openai"):
            requests = []

            def respond(request):
                requests.append(json.loads(request.content))
                self.assertEqual(request.headers["authorization"], "Bearer chat-key")
                if provider == "openai":
                    return httpx.Response(
                        200,
                        json={
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": "鲸鱼"}],
                                }
                            ],
                        },
                    )
                return httpx.Response(
                    200,
                    json={"choices": [{"finish_reason": "stop", "message": {"content": "鲸鱼"}}]},
                )

            cfg = Config(
                llm_provider=provider,
                deepseek_model="deepseek-flash",
                deepseek_api_key="chat-key",
                openai_model="test-openai",
                openai_api_key="chat-key",
            )
            images = await Vision(cfg).prepare(None, current)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                llm = LLM(client, cfg, Search(client, cfg))
                reply = await llm.reply([], "这是什么", "test", images=images)
            self.assertEqual(reply.text, "鲸鱼")
            self.assertEqual(len(requests), 1)
            payload = requests[0]
            self.assertEqual(payload["model"], cfg.model)
            parts = payload["input" if provider == "openai" else "messages"][-1]["content"]
            expected = "input_image" if provider == "openai" else "image_url"
            self.assertEqual(sum(p["type"] == expected for p in parts), 2)
            self.assertIn("引用消息", json.dumps(parts, ensure_ascii=False))

    async def test_resolve_missing_url_and_limit(self):
        current = event()
        current.original_message = Message([picture("") for _ in range(4)])
        bot = SimpleNamespace(get_image=AsyncMock(return_value={"url": "https://gchat.qpic.cn/a"}))
        parts = await Vision(Config(vision_max_images=1)).prepare(bot, current)
        bot.get_image.assert_awaited_once()
        self.assertIn("3张图片未附上", parts[-1]["text"])

    async def test_failure_does_not_attach_unavailable_image(self):
        current = event()
        current.original_message = Message([picture("")])
        bot = SimpleNamespace(get_image=AsyncMock(side_effect=RuntimeError))
        parts = await Vision(Config()).prepare(bot, current)
        self.assertEqual(len(parts), 1)
        self.assertIn("取图失败", parts[0]["text"])

    def test_urls_and_picture_only_routing(self):
        for value in (
            "file:///secret",
            "http://127.0.0.1/a",
            "https://qpic.cn.evil.test/a",
            "https://user:pass@gchat.qpic.cn/a",
        ):
            self.assertIsNone(platform_url(value))
        self.assertIsNotNone(platform_url("https://gchat.qpic.cn/a"))
        self.assertIn("图片", extract_text(SimpleNamespace(self_id="99"), event()))
        self.assertEqual(image_segments(event())[0][0], "当前消息")


class EmoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.emotes = Emotes(self.root / "emotes")
        (self.emotes.root / "开心.png").write_bytes(PNG)
        (self.emotes.root / "readme.txt").write_text("not an image")
        self.cfg = Config(user_cooldown_seconds=0.001)
        self.archive = LongTermMemory(self.root / "db.sqlite", self.cfg)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def test_list_path_boundary_and_payload(self):
        result = self.emotes.list({"query": "开心", "offset": 0})
        self.assertEqual(result["emotes"][0]["id"], "开心.png")
        segment = self.emotes.message("开心.png")
        self.assertEqual(segment.type, "image")
        self.assertEqual(base64.b64decode(segment.data["file"][9:]), PNG)
        for name in ("../secret.png", str(self.root / "secret.png"), "missing.png"):
            with self.assertRaises(ServiceError):
                self.emotes.message(name)
        (self.emotes.root / "bad.png").write_text("not an image")
        with self.assertRaises(ServiceError):
            self.emotes.message("bad.png")
        (self.emotes.root / "开心.png").unlink()
        self.assertEqual(self.emotes.list({"query": "开心", "offset": 0})["status"], "no_matches")

    async def test_selection_can_return_image_only(self):
        tools = HistoryTools(self.archive, (99, 10, 2))
        tools.emotes = self.emotes
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))

            def call(name, args, ident):
                return ("", [{"name": name, "arguments": json.dumps(args), "call_id": ident}], [])

            llm.step = AsyncMock(
                side_effect=[
                    call("choose_emote", {"id": "开心.png"}, "bad"),
                    call("list_emotes", {"query": "开心", "offset": 0}, "list"),
                    call(
                        "remember_emotes",
                        {
                            "items": [
                                {
                                    "id": "开心.png",
                                    "description": "白色小图，情绪不明确，不应据文件名认定开心。",
                                }
                            ]
                        },
                        "remember",
                    ),
                    call("choose_emote", {"id": "开心.png"}, "choose"),
                    ("", [], []),
                ]
            )
            reply = await llm.reply([], "发个开心表情", "test", history_tools=tools)
        self.assertEqual(reply.text, "")
        self.assertEqual(reply.emote_id, "开心.png")
        self.assertEqual(llm.step.await_count, 5)
        final_messages = llm.step.call_args.args[0]
        previews = [
            part
            for msg in final_messages
            if isinstance(msg.get("content"), list)
            for part in msg["content"]
            if part.get("type") == "image_url"
        ]
        self.assertEqual(previews, [])  # Image replaced by durable memory after learning.
        self.assertIn("白色小图", self.emotes.recall(self.emotes.digest("开心.png")))

    async def test_service_sends_and_saves_image_only(self):
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("", emote_id="开心.png")))
        service = Assistant(self.cfg, llm, archive=self.archive)
        service.emotes = self.emotes
        send, send_emote = AsyncMock(), AsyncMock()
        await service.handle(99, 10, 2, 1, "发个开心表情", send, send_emote=send_emote)
        send.assert_not_awaited()
        send_emote.assert_awaited_once_with("开心.png")
        self.assertIn("已发送表情包", service.memory.history((99, 10, 2))[-1]["content"])

    async def test_image_send_failure_never_saved_as_success(self):
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("", emote_id="开心.png")))
        service = Assistant(self.cfg, llm, archive=self.archive)
        await service.handle(
            99, 10, 2, 2, "发个表情", AsyncMock(), send_emote=AsyncMock(side_effect=RuntimeError)
        )
        self.assertEqual(service.memory.history((99, 10, 2)), [])

    async def test_native_images_reach_service_but_are_not_archived(self):
        current = event()
        self.archive.collect((99, 10, 2), 1, "[图片]", ["image"], time.time())
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("蓝色鲸鱼")))
        service = Assistant(self.cfg, llm, archive=self.archive)
        parts = await Vision(self.cfg).prepare(None, current)
        await service.handle(
            99, 10, 2, 1, "[图片]", AsyncMock(), image_reader=AsyncMock(return_value=parts)
        )
        self.assertEqual(llm.reply.call_args.kwargs["images"], parts)
        self.assertNotIn("https", str(service.memory.history((99, 10, 2))))
        row = self.archive.db.execute("SELECT content FROM messages").fetchone()
        self.assertEqual(row[0], "[图片]")

    def test_memory_survives_restart_rename_and_invalidates_replacement(self):
        digest = self.emotes.digest("开心.png")
        self.emotes.remember(
            [{"id": "开心.png", "description": "白色图案，适合困惑的场景"}], {"开心.png": digest}
        )
        renamed = self.emotes.root / "renamed.png"
        (self.emotes.root / "开心.png").rename(renamed)
        reopened = Emotes(self.emotes.root)
        result = reopened.list({"query": "困惑", "offset": 0})
        self.assertEqual(result["emotes"][0]["id"], "renamed.png")
        self.assertEqual(result["emotes"][0]["memory_status"], "known")
        renamed.write_bytes(PNG + b"changed")
        self.assertIsNone(reopened.recall(reopened.digest("renamed.png")))
        with self.assertRaises(ServiceError):
            reopened.remember(
                [{"id": "renamed.png", "description": "old"}], {"renamed.png": digest}
            )
        renamed.unlink()
        self.assertEqual(reopened.list({"query": "困惑", "offset": 0})["emotes"], [])

    async def test_remembered_emote_never_attaches_image_again(self):
        self.emotes.remember(
            [{"id": "开心.png", "description": "白色图案"}],
            {"开心.png": self.emotes.digest("开心.png")},
        )
        tools = HistoryTools(self.archive, (99, 10, 2))
        tools.emotes = self.emotes
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: self.fail("unexpected API call"))
        ) as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            {
                                "name": "list_emotes",
                                "arguments": json.dumps({"query": "白色", "offset": 0}),
                                "call_id": "a",
                            }
                        ],
                        [],
                    ),
                    (
                        "",
                        [
                            {
                                "name": "choose_emote",
                                "arguments": json.dumps({"id": "开心.png"}),
                                "call_id": "b",
                            }
                        ],
                        [],
                    ),
                    ("", [], []),
                ]
            )
            reply = await llm.reply([], "发个白色的图", "test", history_tools=tools)
        self.assertEqual(reply.emote_id, "开心.png")
        self.assertNotIn("data:image", json.dumps(llm.step.call_args.args[0]))
        self.assertEqual(llm.step.await_count, 3)
