"""任务能力提示与参与阶段隔离回归，不执行真实任务。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM
from plugins.assistant.longterm import LongTermMemory
from plugins.tasks.manager import FUNCTION


class TaskRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_capability_only_in_execution_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(model_trace_enabled=False, emotes_auto_collect=False)
            archive = LongTermMemory(Path(directory) / "memory.db", cfg)
            try:
                tools = HistoryTools(archive, (99, 10, 2))
                tools.tasks = SimpleNamespace(functions=lambda: [FUNCTION])
                model = LLM(None, cfg, SimpleNamespace(enabled=False))
                model.step = AsyncMock(return_value=("{}", [], []))
                await model.plan_participation([], {}, {}, {}, 1, True, 99, tools)
                args = model.step.call_args.args
                self.assertIn("不得加入能力有无", args[0][0]["content"])
                self.assertNotIn("manage_task", [f["name"] for f in args[3]])
                await model.reply(
                    [], "后台写贪吃蛇并给试玩链接", "test",
                    memory_context="reply_focus：没有后台能力", history_tools=tools,
                )
                args = model.step.call_args.args
                self.assertIn("manage_task", [f["name"] for f in args[3]])
                self.assertIn("应调用manage_task", args[0][0]["content"])
                self.assertIn("由执行器核实托管条件", args[0][0]["content"])
            finally:
                archive.close()
