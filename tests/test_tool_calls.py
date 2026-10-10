"""验证多工具返回的顺序恢复、协议完整性及副作用边界，不访问真实服务。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
from pydantic import SecretStr

from plugins.assistant.config import Config
from plugins.assistant.emotes import Emotes
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.http import ServiceError
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search
from plugins.assistant.service import Assistant


def history_args(query=""):
    return dict(
        action="search",
        scope=None,
        sender=None,
        query=query,
        start=None,
        end=None,
        anchor=None,
        offset=0,
    )


def call(identifier, name="read_history", arguments=None):
    return {
        "call_id": identifier,
        "name": name,
        "arguments": json.dumps(history_args() if arguments is None else arguments),
    }


def model_response(provider, calls=(), text="完成", reasoning=None):
    if provider == "openai":
        output = [reasoning] if reasoning else []
        output.extend({"type": "function_call", **item} for item in calls)
        if not calls:
            output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
        return {"status": "completed", "output": output}
    message = {"role": "assistant", "content": "" if calls else text}
    if calls:
        message["tool_calls"] = [
            {
                "id": item["call_id"],
                "type": "function",
                "function": {
                    "name": item["name"],
                    "arguments": item["arguments"],
                },
            }
            for item in calls
        ]
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}]}


def tool_results(messages):
    return {
        item.get("call_id", item.get("tool_call_id")): json.loads(
            item.get("output", item.get("content"))
        )
        for item in messages
        if item.get("type") == "function_call_output" or item.get("role") == "tool"
    }


class ToolCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(model_trace_enabled=False, emotes_auto_collect=False)
        self.archive = LongTermMemory(Path(self.temp.name) / "memory.db", self.cfg)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.archive.close)
        self.tools = HistoryTools(self.archive, (99, 10, 2))
        self.archive.collect((99, 10, 2), 1, "编程和音乐", ["text"], 1)

    async def test_both_providers_resume_batch_and_preserve_all_results(self):
        for provider in ("deepseek", "openai"):
            with self.subTest(provider=provider):
                seen = []
                reasoning = {"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}
                responses = iter(
                    [
                        model_response(
                            provider,
                            [
                                call("a", arguments=history_args("编程")),
                                call("b", arguments=history_args("音乐")),
                            ],
                            reasoning=reasoning if provider == "openai" else None,
                        ),
                        model_response(provider, [call("b-next", arguments=history_args("音乐"))]),
                        model_response(provider),
                    ]
                )

                def respond(request):
                    payload = json.loads(request.content)
                    seen.append(payload)
                    if len(seen) == 2:
                        messages = payload["input" if provider == "openai" else "messages"]
                        results = tool_results(messages)
                        self.assertEqual(results["a"]["status"], "ok")
                        self.assertEqual(results["b"]["status"], "not_executed")
                        if provider == "openai":
                            self.assertIn(reasoning, messages)
                    return httpx.Response(200, json=next(responses))

                self.tools.run = Mock(wraps=HistoryTools.run.__get__(self.tools))
                cfg = self.cfg.model_copy(update={"llm_provider": provider, "history_max_calls": 2})
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    llm = LLM(client, cfg, Search(client, cfg))
                    reply = await llm.reply([], "查两件事", "test", history_tools=self.tools)
                self.assertEqual(reply.text, "完成")
                self.assertEqual(
                    [c.args[0]["query"] for c in self.tools.run.call_args_list], ["编程", "音乐"]
                )
                self.assertEqual(len(seen), 3)
                self.assertNotIn("tools", seen[-1])
                results = tool_results(seen[-1]["input" if provider == "openai" else "messages"])
                self.assertEqual(results["b-next"]["status"], "ok")

    async def test_deferred_send_does_not_execute_or_expand_budget(self):
        self.tools.send_callback = AsyncMock(return_value={"status": "sent"})
        self.cfg.history_max_calls = 1
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            call("read"),
                            call("send", "send_group_message", {"group_id": 10, "text": "大家好"}),
                        ],
                        [],
                    ),
                    ("查到了记录，尚未发送。", [], []),
                ]
            )
            reply = await llm.reply([], "查完再说", "test", history_tools=self.tools)
        self.assertEqual(reply.text, "查到了记录，尚未发送。")
        self.tools.send_callback.assert_not_awaited()
        self.assertFalse(self.tools.send_attempted)
        self.assertFalse(llm.step.call_args.args[1])
        self.assertEqual(tool_results(llm.step.call_args.args[0])["send"]["status"], "not_executed")

    async def test_batch_send_respects_existing_once_only_guard(self):
        self.tools.send_callback = AsyncMock(return_value={"status": "sent"})
        send_args = {"group_id": 10, "text": "大家好"}
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            call("a", "send_group_message", send_args),
                            call("b", "send_group_message", send_args),
                        ],
                        [],
                    ),
                    ("", [call("b-next", "send_group_message", send_args)], []),
                    ("已发送一次。", [], []),
                ]
            )
            await llm.reply([], "去群里打招呼", "test", history_tools=self.tools)
        self.tools.send_callback.assert_awaited_once()
        results = tool_results(llm.step.call_args.args[0])
        self.assertEqual(results["b"]["status"], "not_executed")
        self.assertEqual(results["b-next"]["status"], "already_attempted")

    async def test_repeated_batches_do_not_bypass_search_budget(self):
        cfg = self.cfg.model_copy(
            update={"search_api_key": SecretStr("test-key"), "search_max_calls": 1}
        )
        async with httpx.AsyncClient() as client:
            search = Search(client, cfg)
            search.run = AsyncMock(return_value=[])
            llm = LLM(client, cfg, search)
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            call("a", "web_search", {"query": "天气"}),
                            call("b", "web_search", {"query": "新闻"}),
                        ],
                        [],
                    ),
                    ("", [call("b-next", "web_search", {"query": "新闻"})], []),
                    ("资料不足。", [], []),
                ]
            )
            await llm.reply([], "查两件事", "test", history_tools=self.tools)
        search.run.assert_awaited_once_with("天气", "test")
        results = tool_results(llm.step.call_args.args[0])
        self.assertEqual(results["b"]["status"], "not_executed")
        self.assertEqual(results["b-next"]["status"], "unsupported_tool")

    async def test_image_results_precede_preview_and_selection_waits_for_memory(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZk"
            "AAAAASUVORK5CYII="
        )
        for provider in ("deepseek", "openai"):
            with self.subTest(provider=provider):
                emotes = Emotes(Path(self.temp.name) / provider)
                (emotes.root / "image.png").write_bytes(png)
                tools = HistoryTools(self.archive, (99, 10, 2))
                tools.emotes = emotes
                seen = []
                responses = iter(
                    [
                        model_response(
                            provider,
                            [
                                call("list", "list_emotes", {"query": "", "offset": 0}),
                                call("choose", "choose_emote", {"id": "image.png"}),
                            ],
                        ),
                        model_response(
                            provider,
                            [
                                call(
                                    "remember",
                                    "remember_emotes",
                                    {
                                        "items": [{"id": "image.png", "description": "白色图案"}],
                                    },
                                )
                            ],
                        ),
                        model_response(
                            provider, [call("choose-next", "choose_emote", {"id": "image.png"})]
                        ),
                        model_response(provider, text=""),
                    ]
                )

                def respond(request):
                    payload = json.loads(request.content)
                    seen.append(payload)
                    if len(seen) == 2:
                        self.assertIsNone(tools.selected_emote)
                        self.assertIsNone(emotes.recall(emotes.digest("image.png")))
                        messages = payload["input" if provider == "openai" else "messages"]
                        self.assertEqual(tool_results(messages)["choose"]["status"], "not_executed")
                        self.assertEqual(
                            [
                                t.get("name", t.get("function", {}).get("name"))
                                for t in payload["tools"]
                            ],
                            ["remember_emotes"],
                        )
                        self.assertIsInstance(messages[-1]["content"], list)
                        self.assertEqual(list(tool_results(messages[-3:-1])), ["list", "choose"])
                    return httpx.Response(200, json=next(responses))

                cfg = self.cfg.model_copy(update={"llm_provider": provider})
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    llm = LLM(client, cfg, Search(client, cfg))
                    reply = await llm.reply([], "发个图", "test", history_tools=tools)
                self.assertEqual(reply.emote_id, "image.png")
                self.assertEqual(emotes.recall(emotes.digest("image.png")), "白色图案")

    async def test_malformed_batch_is_rejected_before_first_side_effect(self):
        self.tools.send_callback = AsyncMock(return_value={"status": "sent"})
        first = call("a", "send_group_message", {"group_id": 10, "text": "大家好"})
        for invalid in (None, call("a"), call(""), call(7)):
            with self.subTest(invalid=invalid):
                async with httpx.AsyncClient() as client:
                    llm = LLM(client, self.cfg, Search(client, self.cfg))
                    llm.step = AsyncMock(return_value=("", [first, invalid], []))
                    with self.assertRaisesRegex(ServiceError, "工具调用格式无效"):
                        await llm.reply([], "去群里打招呼", "test", history_tools=self.tools)
        self.tools.send_callback.assert_not_awaited()

    async def test_planner_recovers_multiple_calls_and_can_stay_quiet(self):
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    ("", [call("a"), call("b")], []),
                    ('{"reply":false,"ended":false}', [], []),
                ]
            )
            decision = await llm.plan_participation([], {}, {}, {}, 1, False, 99, self.tools)
        self.assertFalse(decision["reply"])
        self.assertEqual(tool_results(llm.step.call_args.args[0])["b"]["status"], "not_executed")

    async def test_service_sends_answer_instead_of_internal_batch_error(self):
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    ("", [call("a"), call("b")], []),
                    ("查到了。", [], []),
                ]
            )
            service = Assistant(self.cfg, llm, archive=self.archive)
            send = AsyncMock()
            await service.handle(99, 10, 2, 100, "查一下历史", send, history_tools=self.tools)
        send.assert_awaited_once_with("查到了。")
        self.assertEqual(self.archive.recent((99, 10, 2))[0][-1]["content"], "查到了。")
