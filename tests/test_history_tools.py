import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.search import Search


def args(**overrides):
    return (
        dict(
            action="search",
            scope=None,
            sender=None,
            query="",
            start=None,
            end=None,
            anchor=None,
            offset=0,
            **{},
        )
        | overrides
    )


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.archive = LongTermMemory(Path(self.tmp.name) / "memory.db", self.cfg)
        self.tools = HistoryTools(self.archive, (99, -1, 1), [10])
        self.stamp = self.tools.timestamp("2026-09-29")
        for i in range(30):
            self.archive.collect((99, 10, 99), i, f"昨天的消息{i}", ["text"], self.stamp + i)
        self.archive.collect((99, -2, 2), 40, "别人的私聊", ["text"], self.stamp)
        self.archive.collect((98, 10, 99), 41, "其他机器人", ["text"], self.stamp)
        self.archive.collect((99, 11, 99), 42, "无权群聊", ["text"], self.stamp)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def test_time_paging_and_isolation(self):
        first = self.tools.run(args(start="2026-09-29", end="2026-09-30", sender=99))
        self.assertEqual(len(first["messages"]), 12)
        second = self.tools.run(args(offset=first["next_offset"]))
        self.assertFalse(
            {r["id"] for r in first["messages"]} & {r["id"] for r in second["messages"]}
        )
        self.assertTrue(all(r["scope"] == 10 for r in second["messages"]))
        self.assertEqual(self.tools.run(args(scope=-2))["status"], "access_denied")
        self.assertEqual(self.tools.run(args(start="2026-09-30"))["status"], "no_matches")
        group = HistoryTools(self.archive, (99, 10, 1), [11])
        self.assertEqual(group.run(args(scope=11))["status"], "access_denied")

    def test_around_and_invalid_dates(self):
        found = self.tools.run(args(query="消息15"))["messages"][0]
        nearby = self.tools.run(args(action="around", anchor=found["id"]))["messages"]
        self.assertEqual(len(nearby), 11)
        self.assertEqual(nearby[5]["id"], found["id"])
        self.assertEqual(self.tools.run(args(start="yesterday"))["status"], "invalid_arguments")
        self.assertEqual(self.tools.run(args(action="around", anchor=31))["status"], "not_found")
        self.archive.delete(99, 10)
        self.assertEqual(self.tools.run(args())["status"], "no_matches")

    async def test_send_scope_and_once(self):
        callback = AsyncMock(return_value={"status": "sent"})
        self.tools.send_callback = callback
        self.assertEqual(
            (await self.tools.send({"group_id": 11, "text": "嗨"}))["status"], "access_denied"
        )
        self.assertEqual((await self.tools.send({"group_id": 10, "text": "嗨"}))["status"], "sent")
        self.assertEqual(
            (await self.tools.send({"group_id": 10, "text": "嗨"}))["status"], "already_attempted"
        )
        callback.assert_awaited_once()

    async def test_provider_roundtrip_without_web(self):
        for provider in ("deepseek", "openai"):
            seen = []

            def respond(request):
                payload = json.loads(request.content)
                seen.append(payload)
                if len(seen) == 1:
                    functions = payload["tools"]
                    self.assertEqual(len(functions), 1)
                    call = {"name": "read_history", "arguments": json.dumps(args(sender=99))}
                    if provider == "openai":
                        return httpx.Response(
                            200,
                            json={
                                "status": "completed",
                                "output": [{"type": "function_call", "call_id": "c", **call}],
                            },
                        )
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "finish_reason": "tool_calls",
                                    "message": {
                                        "role": "assistant",
                                        "content": "",
                                        "tool_calls": [
                                            {"id": "c", "type": "function", "function": call}
                                        ],
                                    },
                                }
                            ]
                        },
                    )
                self.assertIn("昨天的消息", json.dumps(payload, ensure_ascii=False))
                if provider == "openai":
                    return httpx.Response(
                        200,
                        json={
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": "查到了"}],
                                }
                            ],
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "查到了"},
                            }
                        ]
                    },
                )

            cfg = Config(llm_provider=provider, history_max_calls=1)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                llm = LLM(client, cfg, Search(client, cfg))
                reply = await llm.reply([], "昨天说了什么", "test", history_tools=self.tools)
            self.assertEqual(reply.text, "查到了")
            self.assertNotIn("tools", seen[1])

    async def test_planner_can_investigate_and_stay_quiet(self):
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [{"call_id": "c", "name": "read_history", "arguments": json.dumps(args())}],
                        [],
                    ),
                    ('{"reply":false,"ended":false}', [], []),
                ]
            )
            result = await llm.plan_participation([], {}, {}, {}, 1, False, 99, self.tools)
            self.assertFalse(result["reply"])
            self.assertEqual(llm.step.await_count, 2)

    async def test_send_review_fails_closed(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            for response, allowed in (
                ("{}", False),
                ("not json", False),
                ("[]", False),
                ("null", False),
                ('{"allowed":"true"}', False),
                ('{"allowed":true}', True),
            ):
                llm.step = AsyncMock(return_value=(response, [], []))
                self.assertEqual(
                    await llm.approve_group_send(
                        "去测试群露露脸", {"id": 10, "name": "测试群"}, "大家好"
                    ),
                    allowed,
                )

    async def test_send_review_context_and_reasons(self):
        async with httpx.AsyncClient() as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            history = [
                {"role": "user", "content": "去987654321群打个招呼"},
                {"role": "assistant", "content": "确认发送吗？"},
            ]
            llm.step = AsyncMock(return_value=('{"allowed":true,"reason":"ok"}', [], []))
            result = await llm.review_group_send(
                "发吧", {"id": 987654321, "name": "测试群"}, "大家好", history
            )
            self.assertTrue(result["allowed"])
            payload = json.loads(llm.step.call_args.args[0][1]["content"])
            self.assertEqual(payload["history"], history)
            self.assertEqual(payload["request"], "发吧")
            for reason in ("ambiguous_target", "content_mismatch", "privacy", "no_request"):
                llm.step.return_value = (json.dumps({"allowed": False, "reason": reason}), [], [])
                result = await llm.review_group_send("发图", {"id": 10}, "文字", history)
                self.assertEqual(result, {"allowed": False, "reason": reason})
            llm.step.return_value = ('{"allowed":true}', [{"name": "unexpected"}], [])
            result = await llm.review_group_send("发吧", {"id": 10}, "你好")
            self.assertEqual(result, {"allowed": False, "reason": "invalid_review"})

    async def test_empty_search_can_be_revised(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            llm = LLM(client, self.cfg, Search(client, self.cfg))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            {
                                "call_id": "a",
                                "name": "read_history",
                                "arguments": json.dumps(args(query="不存在")),
                            }
                        ],
                        [],
                    ),
                    (
                        "",
                        [
                            {
                                "call_id": "b",
                                "name": "read_history",
                                "arguments": json.dumps(args(sender=99)),
                            }
                        ],
                        [],
                    ),
                    ("找到自己的发言了", [], []),
                ]
            )
            reply = await llm.reply([], "昨天说了什么", "test", history_tools=self.tools)
            self.assertEqual(reply.text, "找到自己的发言了")
            self.assertEqual(llm.step.await_count, 3)
