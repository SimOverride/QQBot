"""验证执行附件与客户端分离，保留旧引用和浏览图片，不访问真实模型。"""

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.assistant.config import Config
from plugins.assistant.dsh import Turn
from plugins.tasks.desktop import installation, launch
from plugins.tasks.runner import environment


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_keeps_attachments_in_own_persistent_or_temporary_home(self):
        with tempfile.TemporaryDirectory() as directory:
            for persistent in (True, False):
                with self.subTest(persistent=persistent):
                    turn = Turn(Config(model_trace_enabled=False), "test")
                    turn.view = {"home": str(Path(directory) / "client"), "id": "test"}
                    turn.runtime = Path(directory) / "runtime" if persistent else None
                    stream = asyncio.StreamReader()
                    stream.feed_eof()
                    process = SimpleNamespace(pid=1, stderr=stream)
                    with (
                        patch("plugins.assistant.dsh.Runner"),
                        patch("plugins.assistant.dsh.launch", return_value=["runtime"]),
                        patch("plugins.assistant.dsh.ProcessJob"),
                        patch("plugins.assistant.dsh.stop_process", AsyncMock()),
                        patch(
                            "plugins.assistant.dsh.asyncio.create_subprocess_exec",
                            AsyncMock(return_value=process),
                        ) as spawn,
                    ):
                        try:
                            await turn.start()
                            args = spawn.await_args
                            patch_file = Path(args.args[args.args.index("--patch") + 1])
                            entries = json.loads(patch_file.read_text(encoding="utf-8"))
                            home = args.kwargs["env"]["DSH_HOME"]
                            attachment = next(
                                item for item in entries if item.get("id") == "attachment-local"
                            )
                            self.assertEqual(attachment["config"]["dshHome"], home)
                            self.assertNotEqual(home, turn.view["home"])
                            if persistent:
                                self.assertEqual(Path(home), turn.runtime)
                        finally:
                            await turn.close()

    async def test_native_images_survive_denied_client_chmod(self):
        try:
            package = installation()
        except ValueError:
            self.skipTest("未安装适配的 DSH 客户端")
        env = environment()
        executable = launch(package, env)[0]
        env["QQBOT_DSH_PACKAGE"] = str(package)
        with tempfile.TemporaryDirectory(prefix="qqbot-attachments-test-") as directory:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    executable,
                    "--expose-internals",
                    str(Path(__file__).with_name("dsh_attachments_check.mjs")),
                    directory,
                ],
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
