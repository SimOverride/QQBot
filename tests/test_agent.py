"""主 Agent 的并行读取、共享预算、会话隔离及状态事件回归。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.assistant.budget import Budget
from plugins.assistant.config import Config
from plugins.assistant.dsh import Chat, _turn
from plugins.assistant.events import Events
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_reads_finish_before_write_barrier(self):
        cfg = Config(model_trace_enabled=False, emotes_auto_collect=False)
        with tempfile.TemporaryDirectory() as folder:
            archive = LongTermMemory(Path(folder) / "memory.db", cfg)
            try:
                scope = HistoryTools(archive, (99, 10, 2))
                arrived = 0
                ready = asyncio.Event()

                async def read(args):
                    nonlocal arrived
                    arrived += 1
                    if arrived == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 1)
                    return {"status": "ok"}

                async def send(*args):
                    self.assertEqual(arrived, 2)
                    return {"status": "sent"}

                scope.shares = SimpleNamespace(allowed=True, run=read)
                scope.send_callback = send
                calls = [
                    {"call_id": str(i), "name": "read_share", "arguments": "{}"} for i in range(2)
                ]
                calls.append(
                    {
                        "call_id": "send",
                        "name": "send_group_message",
                        "arguments": json.dumps({"group_id": 10, "text": "完成"}),
                    }
                )
                model = LLM(None, cfg, SimpleNamespace(enabled=False))
                model.step = AsyncMock(side_effect=[("", calls, []), ("完成", [], [])])
                await model.reply([], "查两项再发送", "test", history_tools=scope)
                self.assertTrue(scope.send_attempted)
            finally:
                archive.close()

    async def test_runtime_scope_policy_and_history_reset(self):
        cfg = Config(model_trace_enabled=False)
        with tempfile.TemporaryDirectory() as folder:
            archive = LongTermMemory(Path(folder) / "memory.db", cfg)
            chat = Chat(None, cfg, SimpleNamespace(enabled=False))
            paths = []

            async def reply(*args, **kwargs):
                paths.append(_turn.get().runtime)
                return Reply("完成")

            try:
                with patch.object(LLM, "reply", reply):
                    for key in ((99, 10, 1), (99, 10, 2), (99, -2, 2), (88, 10, 2)):
                        await chat.reply(
                            [], "测试", "test", history_tools=HistoryTools(archive, key)
                        )
                    archive.delete(99, 10, 1)
                    await chat.reply(
                        [], "测试", "test", history_tools=HistoryTools(archive, (99, 10, 2))
                    )
                    cfg.bot_owners = {2}
                    await chat.reply(
                        [], "测试", "test", history_tools=HistoryTools(archive, (99, 10, 2))
                    )
                self.assertEqual(paths[0], paths[1])
                self.assertEqual(len(set(paths)), 5)
            finally:
                archive.close()

    def test_events_are_persistent_and_scope_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Events(root, (99, 10)).publish("send_failed", {"request": "test"})
            self.assertEqual(Events(root, (99, 10)).read()[0]["kind"], "send_failed")
            self.assertEqual(Events(root, (99, 11)).read(), [])
            self.assertEqual(Events(root, (88, 10)).read(), [])

    def test_shared_budget_counts_cached_tokens_and_cost(self):
        budget = Budget(
            Config(
                agent_max_calls=2, agent_max_cost=0.001, agent_input_price=2, agent_output_price=4
            )
        )
        self.assertTrue(budget.take())
        budget.usage(
            {"inputTokens": 100, "cacheReadTokens": 200, "outputTokens": 100, "totalTokens": 400}
        )
        self.assertEqual(budget.tokens, 400)
        self.assertTrue(budget.remaining()["cost_exhausted"])
        self.assertFalse(budget.take())
