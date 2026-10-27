"""验证预算耗尽的正式桥接收尾与受控错误提示，不访问真实模型。"""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.budget import Budget
from plugins.assistant.config import Config
from plugins.assistant.dsh import Turn
from plugins.assistant.http import ServiceError
from plugins.tasks.desktop import installation, launch
from plugins.tasks.runner import environment


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def create_turn(self, mode):
        try:
            package = installation()
        except ValueError:
            self.skipTest("未安装适配的 DSH 客户端")
        cfg = Config(model_trace_enabled=False, agent_max_tokens=120000)
        turn = Turn(cfg, "budget-test")
        turn.budget = Budget(cfg)
        env = environment()
        executable = launch(package, env)[0]
        env["QQBOT_DSH_PACKAGE"] = str(package)
        turn.process = await asyncio.create_subprocess_exec(
            executable,
            "--expose-internals",
            str(Path(__file__).with_name("dsh_chat_check.mjs")),
            mode,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.addAsyncCleanup(turn.close)
        return turn

    async def test_real_bridge_wraps_up_after_cumulative_token_limit(self):
        turn = await self.create_turn("tokens")
        messages = [{"role": "system", "content": "测试"}, {"role": "user", "content": "查询"}]
        functions = [{"name": "probe", "description": "测试查询", "parameters": {"type": "object"}}]
        async with asyncio.timeout(20):
            for _ in range(2):
                _, calls, output = await turn.step(messages, True, functions)
                self.assertEqual(len(calls), 1)
                messages.extend(output)
                messages.append(
                    {"role": "tool", "tool_call_id": calls[0]["call_id"], "content": "已有结果"}
                )
            self.assertEqual(turn.budget.tokens, 124902)
            messages[0]["content"] += "工具已禁用"
            text, calls, _ = await turn.step(messages, False, functions)
        self.assertIn("收尾", text)
        self.assertEqual(calls, [])

    async def test_real_bridge_budget_limits_and_timeout(self):
        for mode, resource, message in (
            ("cost", "cost", None),
            ("calls", "calls", None),
            ("repeat", "tokens", "资源预算"),
            ("time", "deadline", "处理时间"),
        ):
            with self.subTest(mode=mode):
                turn = await self.create_turn(mode)
                if resource == "cost":
                    turn.budget.max_cost = turn.budget.cost = 1
                elif resource == "deadline":
                    turn.budget.deadline = 0
                else:
                    setattr(turn.budget, resource, getattr(turn.budget, "max_" + resource))
                messages = [
                    {"role": "system", "content": "测试"},
                    {"role": "user", "content": "收尾"},
                ]
                async with asyncio.timeout(20):
                    if message:
                        with self.assertRaisesRegex(ServiceError, message):
                            await turn.step(messages, False, [])
                    else:
                        text, calls, _ = await turn.step(messages, False, [])
                        self.assertIn("收尾", text)
                        self.assertEqual(calls, [])
                await turn.close()

    async def test_error_codes_never_expose_diagnostics(self):
        for code, expected in (
            ("BUDGET_EXHAUSTED", "资源预算"),
            ("STEP_LIMIT", "步骤"),
            ("REQUEST_TIMEOUT", "处理时间"),
            ("VISION_UNSUPPORTED", "不支持图片"),
            ("EXECUTION_FAILED", "诊断日志"),
            (None, "诊断日志"),
        ):
            with self.subTest(code=code):
                turn = Turn(Config(model_trace_enabled=False), "test")
                stream = asyncio.StreamReader()
                event = {"type": "error", "code": code, "message": "敏感请求正文及凭据"}
                stream.feed_data(b"QQBOT_CHAT " + json.dumps(event).encode() + b"\n")
                turn.process = SimpleNamespace(
                    stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock()),
                    stdout=stream,
                )
                with self.assertRaises(ServiceError) as caught:
                    await turn.step([{"role": "system", "content": "测试"}], False, [])
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("敏感", str(caught.exception))
                self.assertNotIn("检查本机安装", str(caught.exception))
