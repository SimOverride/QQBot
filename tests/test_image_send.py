"""搜索图片直发与群复读提示，不联网、不发送真实 QQ 消息。"""

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant.config import Config
from plugins.assistant.emote_collect import EmoteCollector
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search
from plugins.assistant.service import Assistant
from plugins.assistant.vision import Vision
from tests.test_emote_collect import PNG


def call(name, args, ident):
    return "", [{"name": name, "arguments": json.dumps(args), "call_id": ident}], []


class ImageSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_inspect_choose_without_library_or_collection(self):
        with ExitStack() as stack:
            folder = stack.enter_context(tempfile.TemporaryDirectory())
            config = Config(search_api_key="test", emotes_auto_collect=False)
            archive = stack.enter_context(
                closing(LongTermMemory(Path(folder) / "archive.db", config))
            )
            tools = HistoryTools(archive, (99, -2, 2))
            async with httpx.AsyncClient() as client:
                search = Search(client, config)
                search.images = AsyncMock(
                    return_value=[
                        {"url": "https://example.com/diagram.png", "description": "结构示意图"}
                    ]
                )
                model = LLM(client, config, search)
                model.step = AsyncMock(
                    side_effect=[
                        call("search_images", {"query": "结构示意图"}, "1"),
                        call(
                            "inspect_image",
                            {
                                "id": "candidate-"
                                + hashlib.sha256(b"https://example.com/diagram.png").hexdigest()[
                                    :24
                                ]
                            },
                            "2",
                        ),
                        call(
                            "choose_image",
                            {
                                "id": "candidate-"
                                + hashlib.sha256(b"https://example.com/diagram.png").hexdigest()[
                                    :24
                                ]
                            },
                            "3",
                        ),
                        ('{"eligible":true,"description":"结构示意图"}', [], []),
                        ("这是结构示意图", [], []),
                    ]
                )

                def factory(library, review, **kwargs):
                    return EmoteCollector(library, review, AsyncMock(return_value=PNG))

                with patch("plugins.assistant.tools.EmoteCollector", side_effect=factory):
                    reply = await model.reply([], "用图解释", "test", history_tools=tools)
                self.assertEqual(reply.image_data, PNG)
                self.assertIsNone(reply.emote_id)
                self.assertFalse((Path(folder) / "emotes").exists())
                offered = model.step.call_args.args[3]
                self.assertIn("choose_image", {item["name"] for item in offered})
                self.assertNotIn("save_emote", {item["name"] for item in offered})

    async def test_choose_requires_safe_web_source(self):
        review = AsyncMock(return_value={"eligible": True, "description": "图解"})
        collector = EmoteCollector(None, review, AsyncMock(return_value=PNG))
        web = collector.add("https://example.com/a", "web:example")
        group = collector.add("https://gchat.qpic.cn/a", "group:10")
        self.assertIsNone(collector.choose(web))
        self.assertIsNone(collector.choose("invented"))
        await collector.inspect(group)
        self.assertIsNone(collector.choose(group))
        review.return_value = {"eligible": False, "description": "隐私资料"}
        await collector.inspect(web)
        self.assertIsNone(collector.choose(web))
        self.assertFalse(await collector.ensure_review(web))
        self.assertIsNone(collector.choose(web))
        self.assertEqual(collector.inspected[web]["data"], PNG)

    async def test_service_sends_actual_bytes_and_records_only_after_success(self):
        model = SimpleNamespace(reply=AsyncMock(return_value=Reply("说明", image_data=PNG)))
        service = Assistant(Config(), model)
        sender = AsyncMock()
        await service.handle(99, 10, 1, 1, "解释", AsyncMock(), send_image=sender)
        sender.assert_awaited_once_with(PNG)
        self.assertIn("已发送搜索图片", str(service.memory.history((99, 10, 1))))

    async def test_failed_send_is_not_recorded_as_success(self):
        model = SimpleNamespace(reply=AsyncMock(return_value=Reply("", image_data=PNG)))
        service = Assistant(Config(), model)
        sender = AsyncMock(side_effect=RuntimeError("发送失败"))
        await service.handle(99, 10, 1, 1, "解释", AsyncMock(), send_image=sender)
        sender.assert_awaited_once_with(PNG)
        self.assertEqual(service.memory.history((99, 10, 1)), [])


class RepetitionTests(unittest.IsolatedAsyncioTestCase):
    def event(self, user, group=10, file="A" * 32 + ".png"):
        segment = MessageSegment.image("https://gchat.qpic.cn/a?rkey=temporary")
        segment.data["file"] = file
        segment.data["url"] = "https://gchat.qpic.cn/a?rkey=temporary"
        return SimpleNamespace(
            user_id=user,
            group_id=group,
            message_id=user,
            original_message=Message(segment),
            reply=None,
        )

    async def test_distinct_users_scope_and_expiry(self):
        vision, bot = Vision(Config()), SimpleNamespace(self_id="99")
        with patch("plugins.assistant.vision.time.monotonic", return_value=1000):
            vision.observe(bot, self.event(1))
            vision.observe(bot, self.event(1))
            self.assertNotIn("复读", str(await vision.prepare(bot, self.event(1))))
            vision.observe(bot, self.event(2, group=20))
            self.assertNotIn("复读", str(await vision.prepare(bot, self.event(1))))
            vision.observe(bot, self.event(2))
            self.assertIn("复读", str(await vision.prepare(bot, self.event(2))))
            self.assertNotIn(
                "复读", str(await vision.prepare(SimpleNamespace(self_id="98"), self.event(2)))
            )
        with patch("plugins.assistant.vision.time.monotonic", return_value=1601):
            self.assertNotIn("复读", str(await vision.prepare(bot, self.event(2))))

    def test_identifying_query_not_discarded(self):
        def image(query):
            segment = MessageSegment.image("https://gchat.qpic.cn/download?" + query)
            segment.data.pop("file", None)
            segment.data["url"] = "https://gchat.qpic.cn/download?" + query
            return segment

        self.assertEqual(
            Vision.image_key(image("fileid=1&rkey=a")), Vision.image_key(image("fileid=1&rkey=b"))
        )
        self.assertNotEqual(
            Vision.image_key(image("fileid=1")), Vision.image_key(image("fileid=2"))
        )
