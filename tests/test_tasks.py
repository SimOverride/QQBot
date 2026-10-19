"""任务审批、全局调度、实时认知及生成插件发布验证。"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import migration
from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.memory import Memory
from plugins.extensions.manager import Manager as Extensions
from plugins.tasks.generated import Generated, invoke, load, publish
from plugins.tasks.manager import Manager


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(bot_owners={1}, model_trace_enabled=False)
        self.commands = Commands(self.config, Memory(self.config))
        self.extensions = Extensions(self.root, self.commands, [])
        self.manager = Manager(self.root, self.commands, extensions=self.extensions)
        self.commands.tasks = self.manager
        self.admin = AsyncMock(return_value="admin")
        self.member = AsyncMock(return_value="member")
        self.key = (123, 10, 2)
        self.bot = SimpleNamespace(
            send_private_msg=AsyncMock(return_value={}), send_group_msg=AsyncMock(return_value={})
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def request(self, key=None, kind="task", request_id=None):
        return await self.manager.action(
            key or self.key,
            self.admin,
            {"action": "request", "kind": kind, "text": "做一个任务"},
            request_id,
        )

    async def test_concise_creation_and_no_duplicate_start_notice(self):
        reply = await self.commands.run((123, 10, 1), "/任务 执行 整理资料", None)
        self.assertEqual(reply, "好，我会处理这个任务。")
        task = self.manager.store.all()[0]
        for state in ("queued", "running", "testing"):
            task.update(status=state, notification="pending")
            self.manager.store.save(task)
            await self.manager.notify(task, self.bot)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_completion_sends_result_only_and_keeps_details(self):
        task = await self.request((123, 10, 1))
        record = self.manager.store.get(task["id"])
        record.update(status="completed", summary="文件格式 Markdown，验收通过，详细技术报告")
        self.manager.store.save(record)
        self.manager.result_writer = AsyncMock(return_value="整理后的结论是：优先完成登录功能。")
        await self.manager.notify(record, self.bot)
        message = str(self.bot.send_group_msg.call_args.kwargs["message"])
        self.assertEqual(message, "整理后的结论是：优先完成登录功能。")
        self.assertIn("验收", self.manager.store.get(task["id"])["summary"])
        self.assertNotIn(task["id"], message)

    async def test_agent_can_suppress_notification_without_claiming_sent(self):
        task = await self.request((123, 10, 1))
        record = self.manager.store.get(task["id"])
        record.update(status="completed", summary="已完成")
        self.manager.store.save(record)
        self.manager.result_writer = AsyncMock(return_value="")
        await self.manager.notify(record, self.bot)
        self.bot.send_group_msg.assert_not_awaited()
        self.assertEqual(self.manager.store.get(task["id"])["notification"], "silent")

    async def test_result_summary_failure_does_not_dump_technical_report(self):
        task = await self.request((123, 10, 1))
        record = self.manager.store.get(task["id"])
        record.update(status="completed", summary="内部路径，文件格式，测试验收细节")
        self.manager.store.save(record)
        self.manager.result_writer = AsyncMock(side_effect=ValueError())
        await self.manager.notify(record, self.bot)
        message = str(self.bot.send_group_msg.call_args.kwargs["message"])
        self.assertNotIn("验收", message)
        self.assertIn("暂时没能整理", message)

    async def test_application_approval_is_private_owner_only_and_single_use(self):
        application = await self.request()
        self.assertTrue(application["id"].startswith("a_"))
        self.assertFalse(any(r["id"].startswith("t_") for r in self.manager.store.all()))
        await self.manager.tick({"123": self.bot})
        self.bot.send_private_msg.assert_awaited_once()
        self.assertEqual(self.bot.send_private_msg.call_args.kwargs["user_id"], 1)
        command = "/任务 批准 " + application["id"]
        self.assertIn("仅机器人主人", await self.commands.run((123, 10, 1), command, None))
        self.assertIn("仅机器人主人", await self.commands.run((123, -2, 2), command, None))
        self.assertIn("已批准", await self.commands.run((123, -1, 1), command, None))
        self.assertIn("已处理", await self.commands.run((123, -1, 1), command, None))
        self.assertEqual(len([r for r in self.manager.store.all() if r["id"].startswith("t_")]), 1)

    async def test_scope_permissions_dedup_and_owner_bypass(self):
        denied = await self.manager.action(self.key, self.member, {"action": "request"})
        self.assertEqual(denied["status"], "access_denied")
        task = await self.request((123, 10, 1), request_id="12")
        self.assertEqual(task["status"], "queued")
        again = await self.request((123, 10, 1), request_id="12")
        self.assertEqual(task["id"], again["id"])
        for key in ((123, 11, 2), (124, 10, 1), (123, -2, 2)):
            result = await self.manager.action(
                key, self.admin, {"action": "status", "task_id": task["id"]}
            )
            self.assertEqual(result["status"], "not_found")
        self.assertIn(task["id"], self.manager.context((123, -1, 1)))

    async def test_notified_completed_task_is_queryable_but_not_in_context(self):
        task = await self.request((123, 10, 1))
        record = self.manager.store.get(task["id"])
        record.update(status="completed", notification="sent", summary="完整代码" * 1000)
        self.manager.store.save(record)
        self.assertEqual(self.manager.context(self.key), "")
        result = await self.manager.action(
            self.key, self.admin, {"action": "status", "task_id": task["id"]}
        )
        self.assertEqual(result["summary"], record["summary"])
        record["notification"] = "pending"
        self.manager.store.save(record)
        context = self.manager.context(self.key)
        self.assertIn(task["id"], context)
        self.assertNotIn("完整代码", context)

    async def test_supplement_without_approval_and_live_progress(self):
        task = await self.request((123, 10, 1))
        response = await self.manager.action(
            self.key, self.admin, {"action": "supplement", "task_id": task["id"], "text": "加一列"}
        )
        self.assertEqual(len(response["supplements"]), 1)
        self.manager.record(task["id"], "tool/call", {"name": "pwsh", "arguments": "test"})
        self.assertNotIn("pwsh", self.manager.context(self.key))
        self.assertEqual(len(self.manager.store.all()), 1)
        detail = await self.manager.action(
            self.key, self.admin, {"action": "status", "task_id": task["id"]}
        )
        self.assertIn("events", detail)

    async def test_global_concurrency_and_cancel(self):
        gate = asyncio.Event()
        active, maximum = 0, 0

        async def run(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await gate.wait()
            finally:
                active -= 1

        self.manager.runner.run = run
        for group in (10, 11, 12):
            await self.request((123, group, 1))
        await self.manager.tick({"123": self.bot})
        await asyncio.sleep(0)
        self.assertEqual(active, 2)
        self.assertEqual(maximum, 2)
        running = next(iter(self.manager.running))
        await self.manager.action((123, -1, 1), None, {"action": "cancel", "task_id": running})
        await asyncio.sleep(0)
        self.assertEqual(self.manager.store.get(running)["status"], "cancelled")
        gate.set()
        await asyncio.gather(*self.manager.running.values(), return_exceptions=True)
        await self.manager.tick({"123": self.bot})
        await asyncio.gather(*self.manager.running.values(), return_exceptions=True)
        self.assertLessEqual(maximum, 2)

    async def test_recovery_does_not_repeat_running_task(self):
        task = await self.request((123, 10, 1))
        record = self.manager.store.get(task["id"])
        record["status"] = "running"
        self.manager.store.save(record)
        await self.manager.close()
        self.manager = Manager(self.root, self.commands)
        self.assertEqual(self.manager.store.get(task["id"])["status"], "interrupted")

    async def test_notification_failure_never_executes_application(self):
        self.bot.send_private_msg.side_effect = OSError("offline")
        application = await self.request()
        await self.manager.tick({"123": self.bot})
        await self.manager.tick({"123": self.bot})
        self.assertEqual(self.manager.store.get(application["id"])["notification"], "unknown")
        self.assertEqual(self.manager.running, {})
        self.bot.send_private_msg.assert_awaited_once()

    async def test_publish_and_invoke_only_original_group(self):
        task = await self.request((123, 10, 1), kind="plugin")
        task = self.manager.store.get(task["id"])
        task["status"] = "testing"
        self.manager.store.save(task)
        work = self.manager.runner.workspace(task)
        work.mkdir(parents=True)
        (work / "plugin.py").write_text(
            'import json,sys\ndef run(r):\n return {"text":r["text"],"data":{"count":1}}\n'
            'if __name__ == "__main__":\n'
            " print(json.dumps(run(json.loads(sys.stdin.readline()))))\n",
            encoding="utf-8",
        )
        (work / "manifest.json").write_text(
            json.dumps({"title": "回声", "description": "回声测试"}), encoding="utf-8"
        )
        (work / "test_plugin.py").write_text(
            "import unittest,plugin\nclass T(unittest.TestCase):\n"
            ' def test_text(self):\n  self.assertEqual(plugin.run({"text":"x"})["text"],"x")\n'
            " def test_invalid(self):\n  self.assertRaises(KeyError,plugin.run,{})\n",
            encoding="utf-8",
        )
        identity = await publish(self.manager, task)
        self.assertTrue(self.extensions.enabled(123, 10, identity))
        self.assertFalse(self.extensions.enabled(123, 11, identity))
        output = await self.extensions.session(self.key, self.admin).execute(
            "use_" + identity, {"text": "hello"}
        )
        self.assertEqual(output["text"], "hello")
        self.extensions.plugins.clear()
        load(self.extensions, self.root)
        self.assertIsInstance(self.extensions.plugins[identity], Generated)
        task["status"] = "testing"
        self.manager.store.save(task)
        (work / "test_plugin.py").write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            await publish(self.manager, task)

    async def test_migration_preserves_history_and_stops_imported_queue(self):
        task = await self.request((123, 10, 1))
        work = self.manager.runner.workspace(task)
        work.mkdir(parents=True)
        (work / "result.txt").write_text("私有产物", encoding="utf-8")
        output = self.root / "export.zip"
        migration.export_data(self.root, output, 123)
        destination = self.root / "destination"
        destination.mkdir()
        migration.import_data(destination, output, 123)
        from plugins.tasks.store import Store, accounts

        copied = Store(destination)
        try:
            self.assertEqual(copied.get(task["id"])["status"], "interrupted")
            self.assertTrue(copied.events(task["id"]))
        finally:
            copied.close()
        self.assertEqual(accounts(destination / "data/tasks.sqlite3"), {123})
        self.assertEqual(
            (destination / work.relative_to(self.root) / "result.txt").read_text(encoding="utf-8"),
            "私有产物",
        )
        with self.assertRaises(ValueError):
            migration.import_data(destination, output, 124)

    async def test_process_timeout_and_utf8_output(self):
        text = await invoke([sys.executable, "-c", "print('中文输出')"], self.root)
        self.assertIn("中文输出", text)
        with self.assertRaises(TimeoutError):
            await invoke(
                [sys.executable, "-c", "import time;time.sleep(60)"], self.root, timeout=0.1
            )

    async def test_cancel_before_scheduled_worker_starts(self):
        task = await self.request((123, 10, 1))
        self.manager.runner.run = AsyncMock()
        await self.manager.action((123, -1, 1), None, {"action": "cancel", "task_id": task["id"]})
        await self.manager.execute(task["id"])
        self.manager.runner.run.assert_not_awaited()

    async def test_approval_cannot_cross_bot_and_pending_request_cannot_change(self):
        task = await self.request()
        answer = await self.commands.run((124, -1, 1), "/任务 批准 " + task["id"], None)
        self.assertIn("不存在", answer)
        result = await self.manager.action(
            self.key,
            self.admin,
            {"action": "supplement", "task_id": task["id"], "text": "不同的需求"},
        )
        self.assertEqual(result["status"], "application_immutable")


if __name__ == "__main__":
    unittest.main()
