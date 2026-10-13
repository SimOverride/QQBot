"""任务能力提示与参与阶段隔离回归，不执行真实任务。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.llm import LLM, Reply
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.service import Assistant
from plugins.extensions.manager import Manager as Extensions
from plugins.tasks.manager import FUNCTION, Manager


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

    async def test_followup_tools_keep_identity_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(bot_owners={1}, model_trace_enabled=False)
            archive = LongTermMemory(root / "memory.db", cfg)
            model = SimpleNamespace(reply=AsyncMock(return_value=Reply("收到")))
            service = Assistant(cfg, model, archive=archive)
            extensions = Extensions(root, service.commands, [])
            manager = Manager(root, service.commands, archive, extensions)
            service.commands.tasks = manager
            service.commands.extensions = extensions
            try:
                for user, role, expected in [(1, "member", "queued"),
                                              (2, "admin", "pending"),
                                              (3, "member", "access_denied")]:
                    await service.handle(
                        99, 10, user, user, "那就做一个新的插件", AsyncMock(),
                        role_lookup=AsyncMock(return_value=role), passive=True,
                    )
                    tools = model.reply.call_args.kwargs["history_tools"]
                    self.assertIsNotNone(tools.tasks)
                    self.assertIsNotNone(tools.extensions)
                    result = await tools.tasks.execute("manage_task", {
                        "action": "request", "kind": "plugin", "task_id": None,
                        "text": "新插件需求",
                    })
                    self.assertEqual(result["status"], expected)
                self.assertEqual(len(manager.store.all()), 2)
            finally:
                await manager.close()
                archive.close()
