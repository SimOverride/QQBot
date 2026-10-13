"""使用隔离数据库和模拟接口验证认知提取预算、恢复及结构化输出。"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

import migration
from plugins.assistant.config import Config
from plugins.assistant.http import ServiceError
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.service import Assistant
from scripts.retry_profiles import retry


class ProfileRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.cfg = Config(
            memory_profile_min_messages=2,
            model_trace_enabled=False,
            knowledge_cleanup_enabled=False,
        )
        self.archive = LongTermMemory(self.path, self.cfg)
        self.key = (99, 10, 2)
        self.collect(1)
        self.collect(2)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def collect(self, mid, key=None, text="我喜欢编程"):
        self.archive.collect(key or self.key, mid, text, ["text"], 1000 + mid)

    def due(self):
        with self.archive.db:
            self.archive.db.execute("UPDATE profile_retry SET retry_at=0")

    def service(self, extract):
        return Assistant(self.cfg, SimpleNamespace(extract_facts=extract), archive=self.archive)

    async def test_backoff_three_attempts_and_restart_preserves_suspension(self):
        extract = AsyncMock(side_effect=ServiceError("模型未完成回答"))
        service = self.service(extract)
        await service.refresh_profiles()
        row = self.archive.profile_retry(self.key)
        self.assertEqual(row["attempts"], 1)
        self.assertGreaterEqual(row["retry_at"], time.time() + 290)
        for _ in range(5):
            await service.refresh_profiles()
        self.assertEqual(extract.await_count, 1)
        self.due()
        await service.refresh_profiles()
        self.assertGreaterEqual(
            self.archive.profile_retry(self.key)["retry_at"], time.time() + 1790
        )
        self.due()
        await service.refresh_profiles()
        self.assertEqual(extract.await_count, 3)
        self.assertEqual(self.archive.candidates(), [])
        self.assertEqual(self.archive.cursor(self.key), 0)
        self.assertEqual(self.archive.count(self.key), 2)
        self.archive.close()
        self.archive = LongTermMemory(self.path, self.cfg)
        await self.service(extract).refresh_profiles()
        self.assertEqual(extract.await_count, 3)

    async def test_new_messages_do_not_reset_or_expand_failed_batch(self):
        extract = AsyncMock(side_effect=TimeoutError)
        service = self.service(extract)
        await service.refresh_profiles()
        original = self.archive.pending_text(self.key)
        for mid in range(3, 8):
            self.collect(mid, text="我开始学画画")
        self.assertEqual(self.archive.pending_text(self.key), original)
        for _ in range(2):
            self.due()
            await service.refresh_profiles()
        self.collect(8)
        await service.refresh_profiles()
        self.assertEqual(extract.await_count, 3)
        self.assertTrue(all(c.args[0] == original[0] for c in extract.call_args_list))
        self.assertEqual(self.archive.cursor(self.key), 0)

    async def test_waiting_and_suspended_users_do_not_starve_other_users(self):
        other = (99, 10, 3)
        extract = AsyncMock(side_effect=TimeoutError)
        service = self.service(extract)
        await service.refresh_profiles()
        self.collect(3, other)
        self.collect(4, other)
        extract.side_effect = None
        extract.return_value = []
        await service.refresh_profiles()
        self.assertEqual(self.archive.cursor(other), 4)
        self.assertEqual(self.archive.cursor(self.key), 0)
        self.assertEqual(extract.await_count, 2)

    async def test_success_clears_retry_then_processes_later_messages(self):
        extract = AsyncMock(side_effect=[TimeoutError(), [], []])
        service = self.service(extract)
        await service.refresh_profiles()
        self.collect(3)
        self.collect(4)
        self.due()
        await service.refresh_profiles()
        self.assertEqual(self.archive.cursor(self.key), 2)
        self.assertIsNone(self.archive.profile_retry(self.key))
        await service.refresh_profiles()
        self.assertEqual(self.archive.cursor(self.key), 4)
        self.assertEqual(extract.await_count, 3)

    async def test_concurrent_refresh_only_claims_one_request(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def extract(*args):
            entered.set()
            await release.wait()
            return []

        function = AsyncMock(side_effect=extract)
        service = self.service(function)
        first = asyncio.create_task(service.refresh_profiles())
        await entered.wait()
        await service.refresh_profiles()
        self.assertEqual(function.await_count, 1)
        release.set()
        await first

    async def test_explicit_resume_invalidates_old_inflight_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def extract(*args):
            entered.set()
            await release.wait()
            return [{"field": "总体认知", "value": "喜欢编程", "evidence": "我喜欢编程"}]

        service = self.service(extract)
        task = asyncio.create_task(service.refresh_profiles())
        await entered.wait()
        self.assertEqual(retry(self.path, key=self.key)["resumed"], 1)
        release.set()
        await task
        self.assertEqual(self.archive.facts(self.key), [])
        self.assertEqual(self.archive.cursor(self.key), 0)
        self.assertEqual(self.archive.profile_retry(self.key)["attempts"], 0)
        await self.service(AsyncMock(return_value=[])).refresh_profiles()
        self.assertEqual(self.archive.cursor(self.key), 2)

    async def test_queue_timeout_does_not_use_attempt_budget(self):
        self.cfg.queue_timeout_seconds = 0.01
        function = AsyncMock(return_value=[])
        service = self.service(function)
        for _ in range(self.cfg.llm_max_concurrency):
            await service.limits.semaphore.acquire()
        await service.refresh_profiles()
        function.assert_not_awaited()
        self.assertIsNone(self.archive.profile_retry(self.key))

    async def test_clearing_during_failure_never_recreates_retry_state(self):
        async def extract(*args):
            self.archive.delete(*self.key)
            self.collect(3)
            raise TimeoutError

        await self.service(extract).refresh_profiles()
        self.assertIsNone(self.archive.profile_retry(self.key))
        self.assertEqual(self.archive.count(self.key), 1)

    async def test_explicit_resume_and_migration_preserve_batch_budget(self):
        function = AsyncMock(side_effect=TimeoutError)
        service = self.service(function)
        for _ in range(3):
            self.due()
            await service.refresh_profiles()
        source = self.path
        local = Path(self.temp.name) / "local.sqlite3"
        target = LongTermMemory(local, self.cfg)
        target.collect((99, 20, 3), 50, "其他用户", ["text"], 1)
        target.close()
        self.archive.close()
        output = Path(self.temp.name) / "merged.sqlite3"
        migration.merge_memory(local, source, output)
        self.archive = LongTermMemory(output, self.cfg)
        row = self.archive.profile_retry(self.key)
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["last_id"], 3)
        self.assertEqual(self.archive.candidates(), [])
        self.assertEqual(retry(output)["suspended"], 1)
        self.assertEqual(retry(output, all_suspended=True)["resumed"], 1)
        self.assertEqual(self.archive.candidates(), [self.key])


class FactOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_providers_use_json_and_isolated_nonthinking_budget(self):
        for provider in ("deepseek", "openai"):
            with self.subTest(provider=provider):
                seen = []

                def respond(request):
                    payload = json.loads(request.content)
                    seen.append(payload)
                    content = '{"facts":[]}' if len(seen) == 1 else "普通回复"
                    if provider == "openai":
                        return httpx.Response(
                            200,
                            json={
                                "status": "completed",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [{"type": "output_text", "text": content}],
                                    },
                                ],
                            },
                        )
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": content},
                                },
                            ]
                        },
                    )

                cfg = Config(
                    llm_provider=provider,
                    openai_model="gpt-5",
                    llm_thinking_enabled=True,
                    llm_max_output_tokens=128,
                    memory_profile_output_tokens=4096,
                    model_trace_enabled=False,
                )
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    llm = LLM(client, cfg, None)
                    self.assertEqual(await llm.extract_facts("我喜欢编程", "test"), [])
                    await llm.step([], False, "ordinary")
                if provider == "openai":
                    self.assertEqual(seen[0]["text"]["format"]["type"], "json_object")
                    self.assertEqual(seen[0]["max_output_tokens"], 4096)
                    self.assertEqual(seen[0]["reasoning"]["effort"], "none")
                    self.assertNotIn("text", seen[1])
                    self.assertEqual(seen[1]["max_output_tokens"], 128)
                    self.assertEqual(seen[1]["reasoning"]["effort"], "high")
                else:
                    self.assertEqual(seen[0]["response_format"]["type"], "json_object")
                    self.assertEqual(seen[0]["max_tokens"], 4096)
                    self.assertEqual(seen[0]["thinking"]["type"], "disabled")
                    self.assertNotIn("response_format", seen[1])
                    self.assertEqual(seen[1]["max_tokens"], 128)
                    self.assertEqual(seen[1]["thinking"]["type"], "enabled")
                self.assertNotIn("tools", seen[0])

    async def test_invalid_fact_batches_fail_instead_of_advancing(self):
        item = {"field": "总体认知", "value": "编程", "evidence": "喜欢编程"}
        cases = [
            [],
            {"facts": "错误"},
            {"facts": [item] * 7},
            {"facts": [{**item, "value": "长" * 161}]},
            {"facts": [{**item, "evidence": "没有说过的话"}]},
        ]
        cfg = Config(model_trace_enabled=False)
        llm = LLM(None, cfg, None)
        llm.step = AsyncMock()
        for value in cases:
            with self.subTest(value=value):
                llm.step.return_value = (json.dumps(value), [], [])
                with self.assertRaises(ServiceError):
                    await llm.extract_facts("我喜欢编程", "test")
        llm.step.return_value = (json.dumps({"facts": [item]}), [], [])
        self.assertEqual(await llm.extract_facts("我喜欢编程", "test"), [item])
        llm.step.return_value = (
            json.dumps({"facts": [item, item, {**item, "evidence": "没有说过的话"}]}), [], []
        )
        self.assertEqual(await llm.extract_facts("我喜欢编程", "test"), [item])
        other = {**item, "value": "用Python写工具", "evidence": "用Python写工具"}
        llm.step.return_value = (json.dumps({"facts": [item, other]}), [], [])
        self.assertEqual(
            await llm.extract_facts("我喜欢编程，用Python写工具", "test"), [item, other]
        )
