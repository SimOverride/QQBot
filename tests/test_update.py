"""验证更新授权、临时仓库快进、失败停机及监护进程单次消费。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import updater
from plugins.assistant.maintenance import Maintenance


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 聊天授权测试不得检查或更新真实工作仓库。
        self.check = patch("updater.check", return_value="origin").start()
        self.addCleanup(patch.stopall)

    def session(self, user=1):
        return Maintenance(SimpleNamespace(bot_owners={1}), (9, 10, user))

    async def test_owner_tool_available_without_text_gate(self):
        session = self.session()
        self.assertEqual(session.functions()[0]["name"], "update_bot")
        self.assertFalse(session.requested)
        with patch.dict(os.environ, {"QQBOT_UPDATE_TOKEN": "a" * 48}):
            self.assertEqual((await session.execute({}))["status"], "scheduled")
        session = self.session(user=2)
        self.assertEqual(session.functions(), [])
        self.assertEqual((await session.execute({}))["status"], "access_denied")
        self.check.assert_called_once()

    async def test_supervisor_required_and_no_arbitrary_commands(self):
        session = self.session()
        with patch.dict(os.environ, {"QQBOT_UPDATE_TOKEN": ""}):
            self.assertEqual((await session.execute({}))["status"], "unavailable")
        self.assertEqual((await session.execute({"command": "anything"}))["status"],
                         "invalid_arguments")
        self.assertFalse(session.requested)

    async def test_only_successful_notice_commits_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            token = "a" * 48
            with patch.dict(os.environ, {"QQBOT_UPDATE_TOKEN": token}), \
                 patch("plugins.assistant.maintenance.ROOT", root), \
                 patch("plugins.assistant.maintenance.signal.raise_signal") as stop:
                session = self.session()
                await session.execute({})
                self.assertEqual(list((root / "data").iterdir()), [])
                with self.assertRaises(OSError):
                    await session.finish(AsyncMock(side_effect=OSError))
                stop.assert_not_called()
                self.assertEqual(list((root / "data").iterdir()), [])
                await session.finish(AsyncMock())
                self.assertEqual((root / "data" / f"update-{token}.request").read_text(), token)
                stop.assert_called_once()

    async def test_superseded_turn_cannot_stop_bot(self):
        session = self.session()
        with patch.dict(os.environ, {"QQBOT_UPDATE_TOKEN": "a" * 48}), \
             patch("plugins.assistant.maintenance.signal.raise_signal") as stop:
            await session.execute({})
            send = AsyncMock()
            await session.finish(send, lambda: False)
            send.assert_not_called()
            current = Mock(side_effect=[True, False])
            await session.finish(send, current)
            stop.assert_not_called()

    async def test_precheck_failure_keeps_bot_running(self):
        self.check.side_effect = RuntimeError("仓库不干净")
        session = self.session()
        with patch.dict(os.environ, {"QQBOT_UPDATE_TOKEN": "a" * 48}):
            self.assertEqual((await session.execute({}))["status"], "blocked")
            self.assertFalse(session.requested)

    async def test_registry_executes_with_permission_recheck(self):
        from plugins.assistant.modules import Modules

        session = self.session()
        modules = Modules(SimpleNamespace(maintenance=session))
        self.assertIn("update_bot", [f["name"] for f in modules.functions()])
        session.config.bot_owners.clear()
        self.assertEqual((await modules.execute("update_bot", {}, []))["status"], "access_denied")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.source = self.base / "source"
        self.root = self.base / "target"
        self.call(self.base, "init", "--bare", str(self.remote))
        self.call(self.base, "clone", str(self.remote), str(self.source))
        self.configure(self.source)
        (self.source / ".gitignore").write_text("data/\n")
        (self.source / "requirements.txt").write_text("")
        self.commit(self.source, "initial")
        self.call(self.source, "push", "-u", "origin", "HEAD")
        self.call(self.base, "clone", str(self.remote), str(self.root))
        self.configure(self.root)

    def call(self, root, *args):
        return updater.git(root, *args)

    def configure(self, root):
        self.call(root, "config", "user.email", "test@example.invalid")
        self.call(root, "config", "user.name", "测试")

    def commit(self, root, value):
        (root / "version.txt").write_text(value)
        self.call(root, "add", ".")
        self.call(root, "commit", "-m", value)

    def upstream_change(self):
        self.commit(self.source, "new")
        self.call(self.source, "push")

    def invoke(self, pip_failure=False):
        real_run = updater.run

        def run(root, command, timeout=120):
            if command[0] == "git":
                return real_run(root, command, timeout)
            if pip_failure:
                raise RuntimeError("不输出的私密内容")
            return ""

        with patch.object(updater, "run", side_effect=run), \
             patch.object(updater, "admin") as admin:
            success = updater.update(self.root)
        return success, admin

    def test_fast_forward_and_dependency_sync(self):
        self.upstream_change()
        success, admin = self.invoke()
        self.assertTrue(success)
        self.assertEqual(self.call(self.root, "rev-parse", "HEAD"),
                         self.call(self.source, "rev-parse", "HEAD"))
        self.assertEqual([c.args[1] for c in admin.call_args_list], ["stop", "start"])

    def test_dirty_and_untracked_are_rejected(self):
        for path in ("version.txt", "unknown.txt"):
            with self.subTest(path=path):
                file = self.root / path
                file.write_text("local change")
                success, admin = self.invoke()
                self.assertFalse(success)
                admin.assert_not_called()
                if path == "unknown.txt":
                    file.unlink()
                else:
                    self.call(self.root, "restore", path)

    def test_divergence_and_ahead_rejected_without_reset(self):
        self.commit(self.root, "local")
        original = self.call(self.root, "rev-parse", "HEAD")
        for diverged in (False, True):
            if diverged:
                self.upstream_change()
            success, admin = self.invoke()
            self.assertFalse(success)
            admin.assert_not_called()
            self.assertEqual(self.call(self.root, "rev-parse", "HEAD"), original)

    def test_missing_upstream_and_detached_rejected(self):
        self.call(self.root, "branch", "--unset-upstream")
        self.assertFalse(self.invoke()[0])
        self.call(self.root, "checkout", "--detach")
        self.assertFalse(self.invoke()[0])

    def test_dependency_failure_does_not_start_services(self):
        self.upstream_change()
        success, admin = self.invoke(pip_failure=True)
        self.assertFalse(success)
        self.assertEqual([c.args[1] for c in admin.call_args_list], ["stop"])
        state = (self.root / "data/update-status.json").read_text(encoding="utf-8")
        self.assertIn("同步依赖", state)
        self.assertNotIn("私密", state)

    def test_migration_lock_blocks_update(self):
        with updater.project_lock(self.root, name=".migration.lock"):
            success, admin = self.invoke()
        self.assertFalse(success)
        admin.assert_not_called()


class SupervisorTests(unittest.TestCase):
    def test_failed_update_and_manual_stop_never_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def launch(*args, **kwargs):
                token = kwargs["env"]["QQBOT_UPDATE_TOKEN"]
                (root / "data" / f"update-{token}.request").write_text(token)
                return Mock(wait=Mock(return_value=0))

            with patch.object(updater.subprocess, "Popen", side_effect=launch), \
                 patch.object(updater, "update", return_value=False) as update:
                self.assertEqual(updater.supervise(root), 1)
                update.assert_called_once()
            with patch.object(updater.subprocess, "Popen", return_value=Mock(
                wait=Mock(side_effect=[KeyboardInterrupt, 0])
            )), patch.object(updater, "update") as update:
                self.assertEqual(updater.supervise(root), 130)
                update.assert_not_called()

    def test_request_consumed_once_and_next_exit_not_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def launch(*args, **kwargs):
                token = kwargs["env"]["QQBOT_UPDATE_TOKEN"]
                if not calls:
                    (root / "data" / f"update-{token}.request").write_text(token)
                calls.append(token)
                return Mock(wait=Mock(return_value=0))

            with patch.object(updater.subprocess, "Popen", side_effect=launch), \
                 patch.object(updater, "update", return_value=True) as update:
                self.assertEqual(updater.supervise(root), 75)
                self.assertEqual(updater.supervise(root), 0)
                update.assert_called_once_with(root)
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            self.assertEqual(list((root / "data").glob("*.request")), [])

    def test_graceful_signal_is_local_to_bot_process(self):
        # 实际执行当前 Uvicorn 的信号捕获流程，确保不会把 Ctrl+C 发给监护进程。
        code = """
import asyncio, signal
import uvicorn
async def app(scope, receive, send):
    pass
async def main():
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', port=0, log_level='critical'))
    asyncio.get_running_loop().call_later(0.4, signal.raise_signal, signal.SIGINT)
    try:
        await server.serve()
    except KeyboardInterrupt:
        pass
    assert server.should_exit
try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
"""
        result = subprocess.run([updater.sys.executable, "-c", code], capture_output=True,
                                timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
