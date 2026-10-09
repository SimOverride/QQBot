"""验证 Windows 联合启动，不拉起真实 QQ 或 NapCat。"""

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "仅适用于 Windows 启动入口")
class StartupTests(unittest.TestCase):
    def run_ps(self, body, root, helper="napcat.ps1"):
        # 通过环境变量传递路径，避免将测试路径拼接进命令文本。
        env = dict(os.environ)
        env["QQBOT_TEST_ROOT"] = str(root)
        env["QQBOT_TEST_HELPER"] = str(
            Path(__file__).resolve().parents[1] / "scripts" / helper
        )
        code = "$ErrorActionPreference = 'Stop'; . $env:QQBOT_TEST_HELPER; " + body
        encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            env=env, capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_first_run_persists_and_reloads_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TEST_SECRET=keep-me\n", encoding="utf-8")
            (root / "中文 & 空格").mkdir()
            (root / "中文 & 空格" / "launcher.bat").touch()
            self.run_ps(r"""
function Read-Host { return (Join-Path $env:QQBOT_TEST_ROOT '中文 & 空格\launcher.bat') }
$c = Read-NapCatConfig $env:QQBOT_TEST_ROOT
if ($c.port -ne 6099) { throw '默认端口错误' }
function Read-Host { throw '已有配置不应再次提问' }
$d = Read-NapCatConfig $env:QQBOT_TEST_ROOT
if ($d.launcher -ne $c.launcher) { throw '配置未持久化' }
""", root)
            content = (root / ".env").read_text(encoding="utf-8-sig")
            self.assertFalse((root / ".env").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("TEST_SECRET=keep-me", content)
            self.assertIn("NAPCAT_LAUNCHER=", content)

    def test_admin_launch_wait_and_failure(self):
        self.run_ps(r"""
function Get-CimInstance { return @() }
$script:checks = 0
$script:launched = 0
$script:opened = 0
$script:available = $true
function Invoke-RestMethod {
    $script:checks++
    if ($script:checks -eq 1 -or -not $script:available) { throw '尚未启动' }
    return @{ name = '乱码名称'; service = 'qqbot-console' }
}
function Start-Sleep { }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    if ($FilePath -eq 'http://127.0.0.1:8090') {
        if ($script:checks -lt 2) { throw '未等待后台就绪' }
        $script:opened++
        return
    }
    if ($WindowStyle -ne 'Hidden') { throw '后台进程应隐藏窗口' }
    if ($WorkingDirectory -ne $env:QQBOT_TEST_ROOT) { throw '工作目录错误' }
    $script:launched++
}
Start-Admin -ProjectDirectory $env:QQBOT_TEST_ROOT
if ($script:launched -ne 1 -or $script:opened -ne 1) { throw '启动次数错误' }
$script:available = $false
$failed = $false
try { Start-Admin -ProjectDirectory $env:QQBOT_TEST_ROOT } catch { $failed = $true }
if (-not $failed) { throw '后台不可用时应失败' }
if ($script:opened -ne 1) { throw '后台不可用时不应打开浏览器' }
""", Path.cwd(), "admin.ps1")

    def test_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launcher.bat").touch()
            (root / "invalid.txt").touch()
            for launcher, port in [("missing.bat", 6099), ("invalid.txt", 6099),
                                   ("launcher.bat", 0), ("launcher.bat", "wrong")]:
                with self.subTest(launcher=launcher, port=port):
                    (root / ".env").write_text(
                        f"NAPCAT_LAUNCHER='{launcher}'\nNAPCAT_PORT={port}\n",
                        encoding="utf-8"
                    )
                    self.run_ps(r"""
$failed = $false
try { Read-NapCatConfig $env:QQBOT_TEST_ROOT } catch { $failed = $true }
if (-not $failed) { throw '错误配置未拒绝' }
""", root)

    def test_live_port_skips_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_ps(r"""
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
try {
    function Start-Process { throw '端口已监听时不应启动进程' }
    Start-NapCat ([pscustomobject]@{ launcher = 'unused.bat'; port = $port })
} finally { $listener.Stop() }
if (Test-NapCatPort $port) { throw '关闭端口不应探测成功' }
""", directory)

    def test_batch_launch_parameters_and_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_ps(r"""
$script:calls = 0
function Test-NapCatPort { $script:calls++; return ($script:calls -gt 1) }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    if ($FilePath -ne $env:ComSpec) { throw '未使用系统 cmd' }
    if ($WorkingDirectory -ne 'D:\中文 & 空格') { throw '工作目录错误' }
    if ($ArgumentList -ne '/d /v:off /s /c ""D:\中文 & 空格\launcher.bat""') {
        throw '批处理引号错误'
    }
    if ($WindowStyle -ne 'Normal') { throw '扫码窗口不可见' }
}
Start-NapCat ([pscustomobject]@{ launcher = 'D:\中文 & 空格\launcher.bat'; port = 6099 })
if ($script:calls -lt 2) { throw '未等待监听' }
""", directory)
