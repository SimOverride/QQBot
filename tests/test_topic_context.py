"""验证话题切换及命令结果记忆，不调用真实模型。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.memory import Memory
from plugins.assistant.service import Assistant


class TopicContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_discussion_follows_old_task(self):
        model = LLM(None, Config(), SimpleNamespace(enabled=False))
        model.step = AsyncMock(return_value=("你说的是下周六调休吗？", [], []))
        await model.reply(
            [{"role": "user", "content": "创建定时任务"},
             {"role": "assistant", "content": "请确认"}],
            "is that true?", "test",
            memory_context="当前群聊：下周六还有调休吗；reply_focus：核实调休",
        )
        messages = model.step.call_args.args[0]
        self.assertIn("历史问答背景", messages[1]["content"])
        self.assertIn("下周六还有调休", messages[-2]["content"])
        self.assertEqual(messages[-1]["content"], "is that true?")
        self.assertFalse(any(m["role"] == "assistant" for m in messages))

    async def test_command_result_survives_restart_and_send_failure_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            archive = LongTermMemory(Path(directory) / "memory.db", cfg)
            try:
                service = Assistant(cfg, SimpleNamespace(), archive=archive)
                service.commands.run = AsyncMock(return_value="已创建定时任务")
                service.commands.extensions = SimpleNamespace(is_command=lambda _: True)
                await service.handle(99, 10, 2, 100, "/定时 确认 abc", AsyncMock())
                history = Memory(cfg, archive).history((99, 10, 2))
                self.assertEqual(history[-1]["content"], "已创建定时任务")
                self.assertEqual(history[-2]["content"], "/定时 确认 abc")
                await service.handle(
                    99, 10, 2, 101, "/定时 确认 def",
                    AsyncMock(side_effect=RuntimeError("发送失败")),
                )
                self.assertEqual(Memory(cfg, archive).history((99, 10, 2)), history)
            finally:
                archive.close()
