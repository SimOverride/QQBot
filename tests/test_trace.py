"""验证诊断日志开关、数据过滤、链路隔离以及模型接入。"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from plugins.assistant import trace
from plugins.assistant.config import Config
from plugins.assistant.llm import LLM
from plugins.assistant.prompt_store import prompt_text
from plugins.assistant.search import Search


class TraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "model"
        self.patch = patch.object(trace, "TRACE_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cfg = Config(deepseek_api_key="private-key", deepseek_model="test-model")

    def records(self):
        return [
            json.loads(line)
            for p in self.root.glob("*.jsonl")
            for line in p.read_text(encoding="utf-8").splitlines()
        ]

    def test_disabled_does_not_create_files_or_read_prompts(self):
        self.cfg.model_trace_enabled = False
        with patch.object(trace, "sanitize", side_effect=AssertionError):
            trace.emit(self.cfg, "model_context", messages=["私密内容"])
        self.assertFalse(self.root.exists())

    def test_context_omits_fixed_prompts_and_secrets_preserves_dynamic_data(self):
        fixed = prompt_text("search.disabled")
        messages = [
            {"role": "system", "content": fixed + "动态认知"},
            {
                "role": "user",
                "content": "private-key Bearer abcdef data:image/png;base64,AAAA",
                "reasoning_content": "内部推理",
                "authorization": "另一个密钥",
                "arguments": '{"api_key":"未配置的密钥","query":"天气"}',
            },
        ]
        trace.emit(self.cfg, "model_context", messages=messages)
        data = json.dumps(self.records(), ensure_ascii=False)
        for secret in (
            fixed,
            "private-key",
            "abcdef",
            "AAAA",
            "内部推理",
            "另一个密钥",
            "未配置的密钥",
        ):
            self.assertNotIn(secret, data)
        self.assertIn("动态认知", data)
        self.assertIn("天气", data)
        self.assertIn("提示词:search.disabled", data)
        self.assertEqual(messages[0]["content"], fixed + "动态认知")

    async def test_parallel_scopes_are_independent_and_restore_context(self):
        class Worker:
            config = self.cfg

            @trace.scoped("outer")
            async def run(self):
                await asyncio.sleep(0)
                await self.inner()

            @trace.scoped("inner")
            async def inner(self):
                await asyncio.sleep(0)

        await asyncio.gather(Worker().run(), Worker().run())
        records = self.records()
        roots = [r for r in records if r["event"] == "stage_start" and r["stage"] == "outer"]
        self.assertEqual(len({r["trace_id"] for r in roots}), 2)
        for root in roots:
            child = next(
                r
                for r in records
                if r["event"] == "stage_start"
                and r["stage"] == "inner"
                and r["trace_id"] == root["trace_id"]
            )
            self.assertEqual(child["parent_span_id"], root["span_id"])
        self.assertIsNone(trace.CURRENT.get())

    def test_retention_and_write_failure(self):
        self.root.mkdir()
        old = self.root / "trace-2000-01-01.jsonl"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        keep = self.root / "unrelated.txt"
        keep.write_text("保留", encoding="utf-8")
        trace.emit(self.cfg, "test")
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())
        with patch.object(Path, "open", side_effect=OSError("private-key")):
            trace.emit(self.cfg, "test")

    async def test_cancellation_is_recorded_and_scope_restored(self):
        class Worker:
            config = self.cfg

            @trace.scoped("cancel_test")
            async def run(self):
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await Worker().run()
        self.assertEqual(self.records()[-1]["data"]["status"], "cancelled")
        self.assertIsNone(trace.CURRENT.get())

    def test_embedded_fixed_persona_is_folded(self):
        root = Path(trace.__file__).resolve().parents[2]
        persona = (root / "personas" / "默认.txt").read_text(encoding="utf-8-sig").strip()
        trace.emit(
            self.cfg,
            "model_context",
            messages=[
                {
                    "role": "system",
                    "content": "配置：" + json.dumps({"persona": persona}, ensure_ascii=False),
                }
            ],
        )
        data = json.dumps(self.records(), ensure_ascii=False)
        self.assertIn("提示词:personas/默认.txt", data)
        self.assertNotIn(persona.splitlines()[0], data)

    async def test_both_providers_record_usage_context_and_output(self):
        for provider in ("deepseek", "openai"):
            self.cfg.llm_provider = provider
            data = (
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "回答",
                                "reasoning_content": "不可记录",
                            },
                        }
                    ],
                    "usage": {"total_tokens": 23},
                }
                if provider == "deepseek"
                else {
                    "status": "completed",
                    "usage": {"total_tokens": 23},
                    "output": [
                        {"type": "reasoning", "encrypted_content": "不可记录"},
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "回答"}],
                        },
                    ],
                }
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
            ) as client:
                llm = LLM(client, self.cfg, Search(client, self.cfg))
                reply = await llm.reply([], "测试问题", "req")
            self.assertEqual(reply.text, "回答")
        records = self.records()
        contexts = [r for r in records if r["event"] == "model_context"]
        self.assertEqual({r["data"]["provider"] for r in contexts}, {"deepseek", "openai"})
        self.assertEqual(len([r for r in records if r["event"] == "http_usage"]), 2)
        self.assertNotIn("不可记录", json.dumps(records, ensure_ascii=False))

    async def test_search_tool_records_result_and_duration(self):
        self.cfg.search_api_key = SecretStr("search-key")
        responses = iter(
            [
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query":"天气"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
                {"results": [{"title": "天气", "url": "https://example.com", "content": "晴"}]},
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "晴天",
                            },
                        }
                    ]
                },
            ]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))
        ) as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            await llm.reply([], "天气如何", "req")
        result = next(r for r in self.records() if r["event"] == "tool_end")
        self.assertEqual(result["data"]["call_id"], "c1")
        self.assertEqual(result["data"]["status"], "ok")
        self.assertGreaterEqual(result["data"]["elapsed_ms"], 0)
