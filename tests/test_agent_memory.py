"""验证主 agent 记忆、群停用、反馈证据与发送边界。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

import migration
from console_store import ConsoleStore
from plugins.assistant.config import Config
from plugins.assistant.emote_state import current, execute, states
from plugins.assistant.emotes import Emotes
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM, Reply, render_reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory_tools import execute as remember
from plugins.assistant.prompt_store import group_knowledge
from plugins.assistant.service import Assistant
from tests.memory_support import write_memory
from tests.test_emote_collect import PNG


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(model_trace_enabled=False, user_cooldown_seconds=0.001)
        self.archive = LongTermMemory(self.root / "data/memory.sqlite3", self.cfg)
        self.library = Emotes(self.root / "emotes")
        (self.library.root / "a.png").write_bytes(PNG)
        self.library.remember(
            [{"id": "a.png", "description": "反应图"}], {"a.png": self.library.digest("a.png")}
        )
        self.session = HistoryTools(self.archive, (99, 10, 2))
        self.session.emotes = self.library
        self.archive.collect(
            (99, 10, 2), 100, "这张图不适合本群，以后别用了", ["text"], time.time()
        )
        self.archive.collect((99, 20, 2), 200, "另一群反馈", ["text"], time.time())

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def emote(self, action, revision=0, reason="本群明确要求停用", ids=None):
        return execute(
            self.session,
            dict(
                action=action,
                id="a.png",
                revision=revision,
                reason=reason,
                message_ids=ids or [100],
            ),
        )

    def memory(self, action, revision="", value="喜欢编程", subject=2, field="补充认知"):
        ident = self.archive.db.execute("SELECT id FROM messages WHERE message_id=100").fetchone()[
            0
        ]
        return remember(
            self.session,
            dict(
                action=action,
                subject=subject,
                field=field,
                revision=revision,
                value=value,
                sources=[ident],
            ),
        )

    def test_disable_is_group_scoped_and_conflict_checked(self):
        self.assertEqual(self.emote("disable", ids=[200])["status"], "invalid_feedback")
        result = self.emote("disable")
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.library.list({"query": "", "offset": 0}, 99, 10)["emotes"], [])
        self.assertEqual(len(self.library.list({"query": "", "offset": 0}, 99, 20)["emotes"]), 1)
        self.assertEqual(len(self.library.list({"query": "", "offset": 0}, 88, 10)["emotes"]), 1)
        self.assertEqual(self.emote("remember", reason="适合调侃")["status"], "conflict")
        self.assertTrue((self.library.root / "a.png").exists())
        with self.assertRaises(ValueError):
            self.emote("delete")

    def test_deleted_feedback_is_not_reintroduced_to_agent(self):
        saved = self.emote("remember", reason="适合轻松聊天")
        self.assertEqual(self.emote("read")["memory"], "适合轻松聊天")
        self.emote("disable", revision=saved["updated"])
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE message_id=100")
        read = self.emote("read")
        self.assertTrue(read["disabled"])
        self.assertEqual(read["memory"], "")
        self.assertEqual(read["evidence"], "[]")
        self.assertNotIn("history", read)

    def test_backend_exposes_reason_and_restores_with_revision(self):
        saved = self.emote("disable")
        store = ConsoleStore(self.root)
        item = store.get("emote:a.png")
        self.assertEqual(item["group_states"][0]["reason"], "本群明确要求停用")
        with self.assertRaises(ValueError):
            store.restore_emote("emote:a.png", 99, 10, 0)
        restored = store.restore_emote("emote:a.png", 99, 10, saved["updated"])
        self.assertFalse(restored["group_states"][0]["disabled"])
        self.assertIn("本群明确要求停用", restored["group_states"][0]["history"])

    async def test_disabled_selection_is_checked_again_before_send(self):
        self.emote("disable")
        service = Assistant(
            self.cfg,
            SimpleNamespace(reply=AsyncMock(return_value=Reply("", media=[{"emote_id": "a.png"}]))),
            archive=self.archive,
        )
        service.emotes = self.library
        send = AsyncMock()
        await service.handle(99, 10, 2, 101, "发图", AsyncMock(), send_emote=send)
        send.assert_not_awaited()
        self.assertEqual(self.library.usage(99, 10), {})

    async def test_multi_image_order_and_sent_usage(self):
        (self.library.root / "b.png").write_bytes(PNG + b"second")
        service = Assistant(
            self.cfg,
            SimpleNamespace(
                reply=AsyncMock(
                    return_value=Reply(
                        "",
                        media=[{"emote_id": "a.png"}, {"image_data": PNG}, {"emote_id": "b.png"}],
                    )
                )
            ),
            archive=self.archive,
        )
        service.emotes = self.library
        sent = []

        async def emote(ident):
            sent.append(ident)

        async def image(data):
            sent.append("web")

        await service.handle(
            99, 10, 2, 101, "发几张", AsyncMock(), send_emote=emote, send_image=image
        )
        self.assertEqual(sent, ["a.png", "web", "b.png"])
        self.assertEqual(sum(r["use_count"] for r in self.library.usage(99, 10).values()), 2)

    def test_emote_migration_preserves_isolation_and_newer_restore(self):
        self.emote("disable")
        output = self.root / "merged.sqlite3"
        migration.merge_emotes(self.library.memory_path, self.root / "absent", output)
        other = Emotes(self.root / "other")
        other.memory_path = output
        self.assertTrue(current(other, 99, 10, self.library.digest("a.png"))["disabled"])
        self.assertFalse(current(other, 99, 20, self.library.digest("a.png"))["disabled"])
        self.assertEqual(len(states(other)), 1)

    def test_person_memory_write_revision_and_pinned_values(self):
        read = self.memory("read")
        self.assertEqual(self.memory("write", read["revision"])["status"], "saved")
        self.assertEqual(self.memory("write", read["revision"])["status"], "conflict")
        self.archive.remember((99, 10, 2), "补充认知", "人工指定")
        read = self.memory("read")
        self.assertTrue(read["fixed"])
        self.assertEqual(self.memory("write", read["revision"])["status"], "fixed")
        self.assertEqual(self.memory("read")["value"], "人工指定")

    def test_group_memory_invalidates_when_evidence_deleted(self):
        read = self.memory("read", subject=0, field="summary")
        self.assertEqual(
            self.memory("write", read["revision"], "群里讨论图片", subject=0, field="summary")[
                "status"
            ],
            "saved",
        )
        self.assertEqual(group_knowledge(99, 10, self.root)["summary"], "群里讨论图片")
        with self.archive.db:
            self.archive.db.execute("DELETE FROM messages WHERE message_id=100")
        self.assertEqual(group_knowledge(99, 10, self.root), {})

    def test_person_memory_rejects_other_person_evidence(self):
        read = self.memory("read", subject=3)
        self.assertEqual(
            self.memory("write", read["revision"], subject=3)["status"], "invalid_evidence"
        )

    def test_explicit_forget_cannot_be_recreated_from_old_messages(self):
        self.archive.forget((99, 10, 2), "补充认知")
        read = self.memory("read")
        self.assertTrue(read["fixed"])
        self.assertEqual(read["value"], "")
        self.assertEqual(self.memory("write", read["revision"])["status"], "fixed")
        self.assertEqual(self.archive.facts((99, 10, 2)), [])

    async def test_inspection_is_independent_and_more_than_two_can_be_saved(self):
        from plugins.assistant.emote_collect import EmoteCollector

        review = AsyncMock(return_value={"eligible": True, "description": "审核描述"})
        download = AsyncMock(side_effect=[PNG + bytes([i]) for i in range(5)])
        collector = EmoteCollector(self.library, review, download)
        for i in range(5):
            ident = collector.add(f"https://example.com/{i}.png", "web:example")
            result, _ = await collector.inspect(ident)
            self.assertEqual(result["status"], "inspected")
        review.assert_not_awaited()
        for ident in collector.candidates:
            self.assertTrue(await collector.ensure_review(ident))
            result = collector.save(ident, "主agent的实际内容描述")
            self.assertEqual(result["status"], "saved")
            self.assertEqual(self.library.recall(result["digest"]), "主agent的实际内容描述")
        self.assertEqual(review.await_count, 5)

    async def test_network_candidates_can_be_reopened_in_same_scope(self):
        from plugins.assistant.emote_collect import EmoteCollector

        path = self.root / "candidates.json"
        review = AsyncMock()
        first = EmoteCollector(self.library, review, cache_path=path)
        ident = first.add("https://example.com/reaction.png", "web:example", "反应图")
        reopened = EmoteCollector(
            self.library, review, AsyncMock(return_value=PNG), cache_path=path
        )
        self.assertEqual(reopened.listing()["candidates"][0]["id"], ident)
        self.assertEqual((await reopened.inspect(ident))[0]["status"], "inspected")
        review.assert_not_awaited()

    async def test_topic_api_uses_lightweight_json_without_tools(self):
        seen = []

        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": ('{"messages":[]}'),
                            },
                        }
                    ]
                },
            )

        cfg = Config(deepseek_api_key="test", deepseek_model="test", model_trace_enabled=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await LLM(client, cfg, None).assess_topics({"incoming": [], "topics": []})
        self.assertEqual(result, [])
        self.assertNotIn("tools", seen[0])
        self.assertEqual(seen[0]["max_tokens"], 8192)
        self.assertEqual(seen[0]["thinking"], {"type": "disabled"})

    def test_long_text_and_code_have_no_total_truncation(self):
        cfg = Config(reply_chunk_chars=300)
        text = "完整正文" * 4000
        chunks = render_reply(Reply(text), cfg)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        code = "```python\n" + "\n".join(f"print({i})" for i in range(200)) + "\n```"
        chunks = render_reply(Reply(code), cfg)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        for i in range(200):
            self.assertTrue(any(f"print({i})" in c for c in chunks))
        self.assertTrue(all(c.count("```") == 2 for c in chunks))

    def test_global_read_does_not_expose_other_group(self):
        write_memory(
            self.archive,
            (99, 20, 2),
            2,
            "另一群反馈",
            [{"field": "总体认知", "value": "另一群秘密", "evidence": "另一群反馈"}],
        )
        self.assertNotIn("秘密", str(self.memory("read", field="总体认知")))
