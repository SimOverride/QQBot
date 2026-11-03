"""主人私聊专用的 Sakura Frp 登录入口。"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx

COMMAND = "/樱花登录"
BUSY = asyncio.Lock()
API = "https://api.natfrp.com/v4/system/clients"
PACKAGE = r"https://nya\.globalslb\.net/natfrp/client/launcher-windows/[0-9.]+/SakuraLauncher\.exe"


def is_command(text):
    return text.lstrip().startswith(COMMAND)


def sensitive(event):
    reply = getattr(event, "reply", None)
    return COMMAND in str(event.original_message) or COMMAND in str(getattr(reply, "message", ""))


def install(folder, system):
    """只取得官方 Windows 安装程序，校验签名后交由用户完成安装。"""
    release = httpx.get(API, timeout=15).raise_for_status().json()
    url = release["windows"]["archs"]["default"]["url"]
    if not re.fullmatch(PACKAGE, url):
        raise ValueError("发布地址无效")
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".exe", delete=False) as file:
        path = Path(file.name)
    try:
        subprocess.run(
            [
                system / "curl.exe",
                "-q",
                "--fail",
                "--silent",
                "--max-time",
                "120",
                "--max-filesize",
                "134217728",
                "--output",
                path,
                "--",
                url,
            ],
            check=True,
            timeout=130,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # 路径作为数据传入，验证 Windows 信任链、发布者与产品名。
        check = (
            "$s=Get-AuthenticodeSignature -LiteralPath $env:SAKURA_PACKAGE; "
            "if ($s.Status -ne 'Valid' -or "
            "$s.SignerCertificate.GetNameInfo('SimpleName',$false) -ne 'Nya Labs LLC' -or "
            "(Get-Item -LiteralPath $env:SAKURA_PACKAGE).VersionInfo.ProductName.Trim() "
            "-ne 'SakuraFrp 启动器') { exit 1 }"
        )
        shell = system / "WindowsPowerShell/v1.0"
        env = {**os.environ, "PSModulePath": str(shell / "Modules"), "SAKURA_PACKAGE": str(path)}
        subprocess.run(
            [shell / "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", check],
            env=env,
            check=True,
            capture_output=True,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        target = folder / "SakuraSetup.exe"
        path.replace(target)
        os.startfile(target)
    finally:
        path.unlink(missing_ok=True)


def launch(token):
    system = Path(os.environ["SystemRoot"]) / "System32"
    folder = Path(os.environ["LOCALAPPDATA"]) / "QQBot" / "SakuraFrp"
    folder.mkdir(parents=True, exist_ok=True)
    installed = Path(os.environ["ProgramFiles"]) / "SakuraFrpLauncher"
    launcher = installed / "SakuraLauncher.exe"
    core = installed / "SakuraFrpService.exe"
    if not launcher.is_file() or not core.is_file():
        install(folder, system)
        return (
            "已校验并打开官方安装程序。请在宿主机完成默认安装，关闭启动器及核心服务，"
            "再私聊发送登录指令。"
        )
    if not token:
        return "已安装官方启动器，请私聊发送 /樱花登录 访问密钥。"
    running = subprocess.run(
        [system / "tasklist.exe", "/FI", "IMAGENAME eq SakuraFrpService.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        check=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if b'"sakurafrpservice.exe"' in running.stdout.lower():
        return "已有 Sakura Frp 核心服务运行。请先退出启动器并停止核心服务，再重试登录。"
    # 官方核心从独立的用户配置自动登录，保留其他配置项；密钥不进入命令行。
    profile = folder / "config.json"
    config = json.loads(profile.read_text(encoding="utf-8-sig")) if profile.exists() else {}
    config["token"] = token
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        temporary.replace(profile)
    finally:
        temporary.unlink(missing_ok=True)
    process = subprocess.Popen(
        [core, "--config", profile, "--daemon"],
        cwd=installed,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        process.wait(timeout=2)
        return "官方核心启动后已退出，请检查宿主机启动器状态。"
    except subprocess.TimeoutExpired:
        os.startfile(launcher)
    return "已启动官方客户端并提交密钥，登录结果请在宿主机启动器查看。"


async def login(text, private_owner):
    if not private_owner:
        return "权限不足，仅机器人所有者私聊可使用。"
    parts = text.split()
    if not parts or parts[0] != COMMAND or len(parts) > 2:
        return "用法：主人私聊发送 /樱花登录 访问密钥"
    token = parts[1] if len(parts) == 2 else ""
    if token and not re.fullmatch(r"[!-~]{1,256}", token):
        return "访问密钥格式无效。"
    if os.name != "nt":
        return "此功能仅支持 Windows 宿主机。"
    if BUSY.locked():
        return "另一个 Sakura Frp 请求正在执行。"
    async with BUSY:
        try:
            return await asyncio.to_thread(launch, token)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            httpx.HTTPError,
            subprocess.SubprocessError,
        ):
            return "操作失败，请检查官方网络连接、安装包签名、安装权限及本机配置。"
