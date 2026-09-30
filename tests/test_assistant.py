import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from pydantic import ValidationError

from plugins.assistant import addressed
from plugins.assistant.config import Config
from plugins.assistant.http import ServiceError
from plugins.assistant.limits import Limits
from plugins.assistant.llm import LLM, Reply, render_reply
from plugins.assistant.memory import Memory
from plugins.assistant.search import Search, SearchResult
from plugins.assistant.service import Assistant


def config(**kwargs):
    return Config(**{"deepseek_api_key": "test-secret",
                     "deepseek_model": "test-model", **kwargs})


def completion(text="回答", calls=None):
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {
        "role": "assistant", "content": text, "tool_calls": calls,
    }}]}


def tool(query="今日天气", name="web_search", arguments=None):
    return {"id": "call_1", "type": "function", "function": {
        "name": name, "arguments": arguments if arguments is not None
        else json.dumps({"query": query}),
    }}


class ConfigAndRoutingTests(unittest.TestCase):
    def test_runtime_validation(self):
        config().validate_runtime()
        for cfg in (Config(), config(deepseek_api_key=""), config(llm_provider="openai")):
            with self.assertRaises(ValueError):
                cfg.validate_runtime()
        with self.assertRaises(ValidationError):
            config(llm_max_concurrency=0)
        self.assertNotIn("test-secret", repr(config()))

    def test_explicit_at_uses_original_segments(self):
        bot = SimpleNamespace(self_id="99")
        event = SimpleNamespace(group_id=10, user_id=1, to_me=True,
                                original_message=Message("你好"))
        self.assertFalse(addressed(bot, event, config()))
        event.original_message = MessageSegment.at(99) + Message("你好")
        self.assertTrue(addressed(bot, event, config()))
        for field, value in (("group_id", 11), ("user_id", 99)):
            changed = SimpleNamespace(**{**vars(event), field: value})
            self.assertEqual(addressed(bot, changed, config()), field == "group_id")
        event.original_message = MessageSegment.at("all") + Message("你好")
        self.assertFalse(addressed(bot, event, config()))

    def test_render_bounds_and_verified_sources(self):
        cfg = config(reply_chunk_chars=300, reply_max_messages=3)
        reply = Reply("观点[1][99] https://invented.invalid\n" * 100,
                      [SearchResult("来源", "https://example.com", "资料")])
        chunks = render_reply(reply, cfg)
        text = "".join(chunks)
        self.assertLessEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertNotIn("[99]", text)
        self.assertNotIn("invented.invalid", text)
        self.assertIn("https://example.com", text)


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolation_expiry_and_complete_turn_trimming(self):
        memory = Memory(config(session_max_turns=2, session_max_chars=8))
        key = (99, 10, 1)
        for i in range(3):
            memory.save(key, str(i), "ok")
        self.assertEqual([m["content"] for m in memory.history(key)], ["1", "ok", "2", "ok"])
        self.assertEqual(memory.history((99, 10, 2)), [])
        self.assertEqual(memory.history((99, 11, 1)), [])
        memory.sessions[key].touched -= 2000
        self.assertEqual(memory.history(key), [])

    async def test_clear_waits_for_generation_and_lock_cleanup(self):
        memory = Memory(config())
        key = (99, 10, 1)
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def generate():
            async with memory.locked(key):
                entered.set()
                await finish.wait()
                memory.save(key, "问题", "答案")

        async def clear():
            async with memory.locked(key):
                memory.clear(key)

        task = asyncio.create_task(generate())
        await entered.wait()
        clearing = asyncio.create_task(clear())
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(task, clearing)
        self.assertEqual(memory.history(key), [])
        self.assertEqual(memory.locks, {})

    async def test_lock_timeout_does_not_split_lock(self):
        memory = Memory(config(queue_timeout_seconds=0.01))
        key = (1, 2, 3)
        async with memory.locked(key):
            with self.assertRaises(TimeoutError):
                async with memory.locked(key):
                    self.fail("must not acquire")
            self.assertEqual(memory.locks[key][1], 1)
        self.assertEqual(memory.locks, {})

    async def test_limits_cleanup_and_release(self):
        limits = Limits(config(llm_max_concurrency=1, queue_timeout_seconds=0.01))
        self.assertFalse(limits.duplicate((1,)))
        self.assertTrue(limits.duplicate((1,)))
        self.assertFalse(limits.cooling((1,)))
        self.assertTrue(limits.cooling((1,)))
        async with limits.slot():
            with self.assertRaises(TimeoutError):
                async with limits.slot():
                    self.fail("must not acquire")
        async with limits.slot():
            pass
        limits.seen[(1,)] = limits.cooldowns[(1,)] = time.monotonic() - 1
        limits.cleanup()
        self.assertFalse(limits.seen or limits.cooldowns)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def run_model(self, responses, cfg=None):
        cfg = cfg or config()
        requests = []

        def handler(request):
            requests.append(request)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            llm = LLM(client, cfg, Search(client, cfg))
            reply = await llm.reply([], "帮我看看最近进展", "test")
        return reply, requests

    async def test_chat_without_search(self):
        reply, requests = await self.run_model([completion()])
        self.assertEqual(reply.text, "回答")
        body = json.loads(requests[0].content)
        self.assertNotIn("tools", body)
        self.assertEqual(requests[0].url.host, "api.deepseek.com")
        self.assertNotIn("test-secret", str(body))

    async def test_deepseek_autonomous_search(self):
        reply, requests = await self.run_model([
            completion(None, [tool()]),
            {"results": [{"title": "来源", "url": "https://example.com", "content": "资料"}]},
            completion("总结[1]"),
        ], config(search_api_key="search-secret"))
        self.assertEqual(len(reply.sources), 1)
        search_body = json.loads(requests[1].content)
        self.assertEqual(search_body["query"], "今日天气")
        self.assertNotIn("messages", search_body)
        model_body = json.loads(requests[2].content)
        self.assertEqual(model_body["messages"][-1]["role"], "tool")
        self.assertEqual(model_body["messages"][-1]["tool_call_id"], "call_1")

    async def test_openai_responses_preserves_reasoning_items(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
        reply, requests = await self.run_model([
            {"status": "completed", "output": [reasoning, {
                "type": "function_call", "call_id": "call_1", "name": "web_search",
                "arguments": '{"query":"天气"}',
            }]},
            {"results": [{"title": "来源", "url": "https://example.com", "content": "资料"}]},
            {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "总结[1]"}]}]},
        ], config(llm_provider="openai", openai_api_key="openai-secret", openai_model="gpt-test",
                  search_api_key="search-secret"))
        self.assertEqual(reply.text, "总结[1]")
        self.assertEqual(requests[0].url.path, "/v1/responses")
        body = json.loads(requests[2].content)
        self.assertFalse(body["store"])
        self.assertIn(reasoning, body["input"])
        self.assertEqual(body["input"][-1]["type"], "function_call_output")

    async def test_bad_tool_calls_never_reach_search(self):
        for call in (tool(name="shell"), tool(arguments="not-json"), tool(query=""),
                     tool(query="a" * 501), tool(arguments='{"query":"x","other":1}')):
            with self.subTest(call=call), self.assertRaises(ServiceError):
                await self.run_model([completion(None, [call])], config(search_api_key="key"))

    async def test_search_failure_and_no_results(self):
        for result in ({"results": []}, httpx.Response(500), {"unexpected": True}):
            with self.subTest(result=result), self.assertRaises(ServiceError):
                await self.run_model([completion(None, [tool()]), result],
                                     config(search_api_key="key"))

    async def test_summary_failure_returns_sources_without_saving(self):
        reply, requests = await self.run_model([
            completion(None, [tool()]),
            {"results": [{"title": "来源", "url": "https://example.com", "content": "资料"}]},
            httpx.Response(503),
        ], config(search_api_key="key"))
        self.assertFalse(reply.save)
        self.assertEqual(len(reply.sources), 1)
        self.assertEqual(len(requests), 3)

    async def test_search_budget_stops_tool_loop(self):
        reply, requests = await self.run_model([
            completion(None, [tool()]),
            {"results": [{"title": "来源", "url": "https://example.com", "content": "资料"}]},
            completion(None, [tool()]),
        ], config(search_api_key="key", search_max_calls=1))
        self.assertFalse(reply.save)
        self.assertEqual(len(requests), 3)
        self.assertNotIn("tools", json.loads(requests[-1].content))

    async def test_provider_errors_are_safe(self):
        for response in (httpx.Response(401, text="secret"), httpx.Response(429),
                         httpx.Response(200, text="not json"), {}, completion(""),
                         httpx.ReadTimeout("secret")):
            with self.subTest(response=response), self.assertRaises(ServiceError) as caught:
                await self.run_model([response])
            self.assertNotIn("secret", str(caught.exception))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("答案")))
        self.service = Assistant(config(), self.llm)
        self.send = AsyncMock()

    async def handle(self, text, mid=1, **kwargs):
        await self.service.handle(99, kwargs.get("group", 10), kwargs.get("user", 1),
                                  mid, text, self.send, kwargs.get("role_lookup"))

    async def test_natural_intent_does_not_require_command(self):
        await self.handle("帮我狠狠吐槽一下这个方案")
        self.assertEqual(self.llm.reply.call_args.args[1], "帮我狠狠吐槽一下这个方案")
        self.assertEqual(len(self.service.memory.history((99, 10, 1))), 2)

    async def test_unauthorized_and_self_never_call_model(self):
        await self.handle("你好", group=0)
        await self.handle("你好", user=99)
        self.llm.reply.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_dedup_cooldown_and_clear(self):
        await self.handle("你好")
        await self.handle("你好")
        self.assertEqual(self.send.await_count, 1)
        await self.handle("下一条", mid=2)
        self.assertEqual(self.llm.reply.await_count, 1)
        await self.handle("/清空", mid=3, role_lookup=AsyncMock(return_value="admin"))
        self.assertEqual(self.service.memory.history((99, 10, 1)), [])
        self.assertEqual(self.service.memory.locks, {})

    async def test_help_ping_empty_and_long_input(self):
        for i, text in enumerate(("/帮助", "/ping", " ", "x" * 4001)):
            await self.handle(text, mid=i)
        self.llm.reply.assert_not_awaited()
        self.assertEqual(self.send.await_count, 4)

    async def test_send_failure_does_not_save_or_retry(self):
        self.send.side_effect = RuntimeError("send failed")
        await self.handle("你好")
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.service.memory.history((99, 10, 1)), [])
        self.assertEqual(self.service.limits.pending, 0)

    async def test_send_timeout_does_not_retry(self):
        self.send.side_effect = TimeoutError()
        await self.handle("你好")
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.service.memory.history((99, 10, 1)), [])

    async def test_workflow_deadline_releases_resources(self):
        async def slow_reply(*args):
            await asyncio.sleep(1)

        self.service.config.request_timeout_seconds = 0.01
        self.llm.reply.side_effect = slow_reply
        await self.handle("你好")
        self.assertEqual(self.service.memory.locks, {})
        self.assertEqual(self.service.limits.pending, 0)
        self.assertIn("超时", self.send.call_args.args[0])

    async def test_command_boundary_does_not_clear_history(self):
        self.service.memory.save((99, 10, 1), "旧问题", "旧回答")
        await self.handle("清空这个方案的缺点是什么意思")
        self.assertEqual(len(self.llm.reply.call_args.args[0]), 2)
        self.assertEqual(len(self.service.memory.history((99, 10, 1))), 4)

    async def test_model_failure_releases_resources(self):
        self.llm.reply.side_effect = ServiceError("模型超时")
        await self.handle("你好")
        self.assertEqual(self.service.memory.history((99, 10, 1)), [])
        self.assertEqual(self.service.memory.locks, {})
        self.assertEqual(self.service.limits.pending, 0)
        self.assertEqual(self.send.call_args.args[0], "模型超时")


if __name__ == "__main__":
    unittest.main()
