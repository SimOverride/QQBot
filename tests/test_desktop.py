"""客户端按安装结构和运行能力接入，不绑定版本字符串。"""

import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plugins.assistant.config import Config
from plugins.assistant.dsh import Turn
from plugins.assistant.http import ServiceError
from plugins.tasks.desktop import InstallationError, check_runtime, installation


def archive(root, client="3.0", runtime="2.0", cli=True):
    """生成仅供定位测试读取的最小归档，不安装或执行假客户端。"""
    entries = {
        "package.json": json.dumps({"version": client}).encode(),
        "dsh/node_modules/@deepseek-ai/dsh/package.json": json.dumps({"version": runtime}).encode(),
    }
    if cli:
        entries["dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"] = b""
    header = {"files": {}}
    payload = b""
    for name, data in entries.items():
        node = header
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"offset": str(len(payload)), "size": len(data)}
        payload += data
    encoded = json.dumps(header).encode()
    path = root / "resources/app.asar"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4I", 4, 8 + len(encoded), 0, len(encoded)) + encoded + payload)
    (root / "DeepSeek Harness.exe").touch()
    return path


class DesktopTests(unittest.TestCase):
    def setUp(self):
        check_runtime.cache_clear()
        self.addCleanup(check_runtime.cache_clear)

    def test_different_or_absent_versions_do_not_block_compatible_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for client, runtime in (("0.2.1-rc.3", "0.2.1-rc.3"), ("4.0", "3.1"), (None, None)):
                with self.subTest(client=client, runtime=runtime):
                    path = archive(root, client, runtime)
                    with patch("plugins.tasks.desktop.check_runtime") as check:
                        package = installation(str(root))
                        self.assertEqual(package, path / "dsh/node_modules/@deepseek-ai/dsh")
                        check.assert_called_once()
                    with patch("plugins.tasks.desktop.check_runtime"):
                        self.assertEqual(installation(str(root / "DeepSeek Harness.exe")), package)

    def test_required_entry_is_checked_instead_of_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive(root, cli=False)
            with patch("plugins.tasks.desktop.check_runtime") as check:
                with self.assertRaisesRegex(InstallationError, "CLI 入口"):
                    installation(str(root))
                check.assert_not_called()
            archive(root)
            (root / "DeepSeek Harness.exe").unlink()
            with self.assertRaisesRegex(InstallationError, "缺少 DeepSeek Harness.exe"):
                installation(str(root))

    def test_capability_success_is_cached_until_installation_changes(self):
        result = SimpleNamespace(returncode=0, stdout=b'QQBOT_DSH_CHECK {"missing":[]}', stderr=b"")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = archive(root)
            with patch("plugins.tasks.desktop.subprocess.run", return_value=result) as run:
                installation(str(root))
                installation(str(root))
                self.assertEqual(run.call_count, 1)
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
                installation(str(root))
                self.assertEqual(run.call_count, 2)
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["ELECTRON_RUN_AS_NODE"], "1")
                self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_missing_capability_is_specific_and_raw_diagnostics_stay_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive(root)
            results = (
                (
                    SimpleNamespace(
                        returncode=1,
                        stdout=(
                            b'QQBOT_DSH_CHECK {"missing":['
                            b'"@deepseek-ai/dsh-agent:installModelSelection"]}'
                        ),
                        stderr=b"private",
                    ),
                    "installModelSelection",
                ),
                (SimpleNamespace(returncode=1, stdout=b"private", stderr=b"private"), "检查未完成"),
            )
            for result, message in results:
                with patch("plugins.tasks.desktop.subprocess.run", return_value=result):
                    with self.assertRaisesRegex(InstallationError, message) as caught:
                        installation(str(root))
                    self.assertNotIn("private", str(caught.exception))
            with patch(
                "plugins.tasks.desktop.subprocess.run",
                side_effect=subprocess.TimeoutExpired("private", 20),
            ):
                with self.assertRaisesRegex(InstallationError, "超时"):
                    installation(str(root))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_exposes_controlled_installation_diagnostic(self):
        turn = Turn(Config(model_trace_enabled=False), "test")
        with patch(
            "plugins.assistant.dsh.Runner.package",
            side_effect=InstallationError("DSH 缺少项目所需运行能力：test"),
        ):
            with self.assertRaisesRegex(ServiceError, "缺少项目所需运行能力"):
                await turn.start()
        self.assertIsNone(turn.process)
