"""聊天与表情优化回归；全部使用隔离数据与模拟平台。"""

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from nonebot.adapters.onebot.v11 import Message, MessageSegment

import migration
from admin.console_store import ConsoleStore
from plugins.assistant.config import Config
from plugins.assistant.emotes import Emotes
from plugins.assistant.forward import ForwardReader
from plugins.assistant.groupchat import GroupConversation, GroupSettings, GroupState
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.http import ServiceError
from plugins.assistant.llm import LLM, Reply, render_reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search
from plugins.assistant.service import Assistant
from plugins.assistant.vision import Vision, limit_images

PNG = b"\x89PNG\r\n\x1a\n" + b"data"


class Improvements(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Emotes(self.root / "emotes")
        for i, name in enumerate(("a.png", "b.png")):
            (self.root / "emotes" / name).write_bytes(PNG + bytes([i]))
            self.library.remember(
                [{"id": name, "description": "画面：猫；文字：无；情绪：无奈；适用场景：接梗"}],
                {name: self.library.digest(name)},
            )

    def test_synonyms_repetition_and_scope(self):
        self.library.record_sent("a.png", 99, 10)
        rows = self.library.list({"query": "猫 无语", "offset": 0}, 99, 10)["emotes"]
        self.assertEqual(rows[0]["id"], "b.png")
        self.assertEqual(rows[1]["use_count"], 1)
        self.assertEqual(self.library.usage(99, 20), {})
        self.assertEqual(self.library.usage(100, 10), {})

    def test_description_proposal_and_conflict(self):
        digest = self.library.digest("a.png")
        old = self.library.recall(digest)
        self.library.remember([{"id": "a.png", "description": "修正后的内容"}], {"a.png": digest})
        self.assertEqual(self.library.recall(digest), old)
        store = ConsoleStore(self.root)
        value = store.get("emote:a.png")
        self.assertEqual(value["proposal"]["value"]["description"], "修正后的内容")
        with closing(sqlite3.connect(self.library.memory_path)) as db, db:
            db.execute("UPDATE emote_memory SET description='人工更新' WHERE digest=?", (digest,))
        self.assertNotIn("proposal", store.get("emote:a.png"))

    def test_migration_usage_idempotent(self):
        self.library.record_sent("a.png", 99, 10)
        incoming = self.root / "incoming.db"
        with (
            closing(sqlite3.connect(self.library.memory_path)) as source,
            closing(sqlite3.connect(incoming)) as target,
        ):
            source.backup(target)
        output = self.root / "merged.db"
        migration.merge_emotes(self.library.memory_path, incoming, output)
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM emote_usage").fetchone()[0], 1)

    async def test_image_origin(self):
        event = SimpleNamespace(
            message_id=20,
            original_message=Message(),
            reply=SimpleNamespace(
                message_id=10,
                message=Message([MessageSegment("image", {"url": "https://gchat.qpic.cn/x"})]),
            ),
        )
        parts = await Vision(Config()).prepare(None, event)
        self.assertIn("消息ID=10", parts[0]["text"])

    async def test_forward_limits_and_isolation(self):
        event = SimpleNamespace(
            original_message=Message([MessageSegment("forward", {"id": "one"})])
        )
        bot = SimpleNamespace(
            get_forward_msg=AsyncMock(
                return_value={
                    "messages": [
                        {
                            "sender": {"user_id": 5, "nickname": "甲"},
                            "content": [
                                {"type": "text", "data": {"text": "资料"}},
                                {"type": "forward", "data": {"id": "two"}},
                            ],
                        }
                    ]
                }
            )
        )
        reader = ForwardReader(bot, event)
        self.assertEqual((await reader.run({"id": "foreign"}))["status"], "access_denied")
        result = await reader.run({"id": "one"})
        self.assertEqual(result["messages"][0]["sender"], "5")
        await reader.run({"id": "one"})
        self.assertEqual(bot.get_forward_msg.await_count, 1)
        await reader.run({"id": "two"})
        self.assertEqual(reader.allowed["two"], 1)
        bot.get_forward_msg.side_effect = RuntimeError()
        reader.allowed["broken"] = 0
        self.assertEqual((await reader.run({"id": "broken"}))["status"], "unavailable")
        reader.allowed["deep"] = 8
        self.assertEqual((await reader.run({"id": "deep"}))["status"], "limit")

    def test_message_boundaries_preserve_code(self):
        cfg = Config()
        self.assertEqual(render_reply(Reply("一句\n<分条>\n第二句"), cfg), ["一句", "第二句"])
        code = "```text\n<分条>\n```"
        self.assertEqual(render_reply(Reply(code), cfg), [code])
        text = "a" * 240 + "\n<分条>\n" + "b" * 240 + "\n<分条>\n" + "c" * 240
        chunks = render_reply(Reply(text), Config(reply_chunk_chars=300, reply_max_messages=3))
        self.assertEqual("".join(chunks).replace("\n", ""), "a" * 240 + "b" * 240 + "c" * 240)
        self.assertLessEqual(len(chunks), 3)

    async def test_batch_same_sender(self):
        settings = GroupSettings(self.root / "group_chat.json")
        archive = LongTermMemory(self.root / "data/memory.sqlite3", Config())
        self.addCleanup(archive.close)
        controller = GroupConversation(
            SimpleNamespace(archive=archive, config=archive.config), None, settings
        )
        self.addAsyncCleanup(controller.close)
        controller.respond = AsyncMock()
        bot = SimpleNamespace(self_id="99")
        first = SimpleNamespace(group_id=10, user_id=2, message_id=1)
        second = SimpleNamespace(group_id=10, user_id=2, message_id=2)
        task = asyncio.create_task(controller.receive(bot, first, "看看这个", True))
        await asyncio.sleep(0.01)
        await controller.receive(bot, second, "哪里错了", False)
        await task
        controller.respond.assert_awaited_once()
        self.assertEqual(controller.respond.call_args.args[2], "看看这个\n哪里错了")

    async def test_quote_and_superseded_parts(self):
        config = Config(user_cooldown_seconds=0.001, model_trace_enabled=False)
        archive = LongTermMemory(self.root / "data/memory.sqlite3", config)
        self.addCleanup(archive.close)
        archive.collect((99, 10, 2), 11, "旧问题", ["text"], time.time())

        async def reply(*args, **kwargs):
            await kwargs["history_tools"].response.execute(
                {
                    "action": "reply",
                    "quote_message_id": 11,
                    "topic": "",
                    "active": True,
                }
            )
            return Reply("先说一句\n<分条>\n再补一句")

        llm = SimpleNamespace(reply=AsyncMock(side_effect=reply))
        service = Assistant(config, llm, archive=archive)
        controller = GroupConversation(
            service,
            SimpleNamespace(refresh_group=AsyncMock(), for_scope=lambda *a: {}),
            GroupSettings(self.root / "groups.json"),
        )
        self.addAsyncCleanup(controller.close)
        state = GroupState()

        async def sent(**kwargs):
            state.versions[2] = 1
            return {"message_id": 100}

        bot = SimpleNamespace(self_id="99", send_group_msg=AsyncMock(side_effect=sent))
        event = SimpleNamespace(group_id=10, user_id=2, message_id=12)
        await controller.respond(bot, event, "看看", state, True)
        self.assertEqual(bot.send_group_msg.await_count, 1)
        self.assertEqual(bot.send_group_msg.call_args.kwargs["message"][0].data["id"], "11")
        self.assertEqual(archive.count((99, 10, 99)), 1)

    def test_stale_description_rejected(self):
        digest = self.library.digest("a.png")
        with self.assertRaises(ServiceError):
            self.library.remember(
                [{"id": "a.png", "description": "过期解读"}], {"a.png": digest}, {digest: "旧版本"}
            )
        with closing(sqlite3.connect(self.library.memory_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM emote_proposals").fetchone()[0], 0)

    async def test_model_reinspect_updates_unified_description_with_history(self):
        from plugins.assistant.vault import Vault

        image_digest = self.library.digest("a.png")
        Vault(self.root, 99).put(self.library.description_data(image_digest, "无奈的猫"))
        config = Config(model_trace_enabled=False)
        archive = LongTermMemory(self.root / "memory.db", config)
        self.addCleanup(archive.close)
        tools = HistoryTools(archive, (99, 10, 2))
        tools.emotes = self.library

        def call(name, args):
            return ("", [{"call_id": name, "name": name, "arguments": json.dumps(args)}], [])

        async with httpx.AsyncClient() as client:
            llm = LLM(client, config, Search(client, config))
            llm.step = AsyncMock(
                side_effect=[
                    call("list_emotes", {"query": "a.png", "offset": 0}),
                    call("inspect_emote", {"id": "a.png"}),
                    call(
                        "remember_emotes", {"items": [{"id": "a.png", "description": "修正内容"}]}
                    ),
                    call("choose_emote", {"id": "a.png"}),
                    ("", [], []),
                ]
            )
            result = await llm.reply([], "重新看看", "test", history_tools=tools)
        self.assertEqual(result.emote_id, "a.png")
        note = self.library.description_note(99, self.library.digest("a.png"))
        self.assertEqual(note["value"], "修正内容")
        self.assertIn("无奈", note["history"][-1]["value"])
        self.assertEqual(self.library.recall(self.library.digest("a.png"), 99), "修正内容")

    def test_batch_image_limit_keeps_origin(self):
        parts = []
        for number in range(3):
            parts.extend(
                [
                    {"type": "text", "text": str(number)},
                    {"type": "image_url", "image_url": {"url": str(number)}},
                ]
            )
        result = limit_images(parts, 2)
        self.assertEqual([x["text"] for x in result[:-1] if x["type"] == "text"], ["0", "1"])
        self.assertEqual(sum(x["type"] == "image_url" for x in result), 2)
        self.assertIn("1张", result[-1]["text"])
