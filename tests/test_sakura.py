"""检查宿主机指令的身份边界、引用过滤和实际下载来源。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plugins.assistant import sakura


class SakuraTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_private_owner_can_launch(self):
        with patch.object(sakura, "launch", return_value="已提交") as launch:
            self.assertIn("权限不足", await sakura.login("/樱花登录 example", False))
            self.assertIn("用法", await sakura.login("/樱花登录 example extra", True))
            launch.assert_not_called()
            with patch.object(sakura.os, "name", "nt"):
                self.assertEqual(await sakura.login("/樱花登录 example", True), "已提交")
            launch.assert_called_once_with("example")

    def test_sensitive_messages_and_replies(self):
        message = "/樱花登录 example"
        self.assertTrue(sakura.sensitive(SimpleNamespace(original_message=message)))
        event = SimpleNamespace(original_message="完成了吗", reply=SimpleNamespace(message=message))
        self.assertTrue(sakura.sensitive(event))
        self.assertFalse(sakura.sensitive(SimpleNamespace(original_message="/后台")))

    @unittest.skipUnless(os.name == "nt", "Windows 启动器")
    def test_login_keeps_key_out_of_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "SakuraFrpLauncher"
            installed.mkdir()
            for name in ("SakuraLauncher.exe", "SakuraFrpService.exe"):
                (installed / name).touch()
            env = {"ProgramFiles": directory, "LOCALAPPDATA": directory}
            with (
                patch.dict(os.environ, env),
                patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=b"")),
                patch.object(subprocess, "Popen") as start,
            ):
                start.return_value.wait.side_effect = subprocess.TimeoutExpired("core", 2)
                self.assertIn("提交", sakura.launch("test-only-key"))
                core, gui = start.call_args_list
                args = core.args[0]
                self.assertNotIn("test-only-key", str(start.call_args_list))
                profile = json.loads(args[2].read_text(encoding="utf-8"))
                self.assertEqual(profile["token"], "test-only-key")
                self.assertEqual(gui.args[0], [installed / "SakuraLauncher.exe"])
                self.assertEqual(core.kwargs["env"]["ProgramData"], str(args[2].parent))
                self.assertEqual(core.kwargs["env"], gui.kwargs["env"])
