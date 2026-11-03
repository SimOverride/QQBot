"""主人私聊专用的 Sakura Frp 登录入口。"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

import httpx

from admin.console_endpoint import HOST, PORT, external_origin

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
    # 核心在解析 --config 前已按 ProgramData 初始化工作目录，界面需使用相同环境。
    env = {**os.environ, "ProgramData": str(folder)}
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [core, "--config", profile, "--daemon"],
            cwd=installed,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            subprocess.Popen([launcher], cwd=installed, env=env)
        else:
            errors.seek(0)
            detail = errors.read(16384).decode("utf-8", errors="replace")
            if "签名" in detail or "文件不存在" in detail:
                return "官方核心报告安装文件缺失或签名无效，请使用官方安装程序修复。"
            if "Access is denied" in detail:
                return "官方核心无法访问工作目录，请检查当前 Windows 用户的写入权限。"
            return f"官方核心已退出（退出码 {code}），请查看配置目录中的 SakuraFrpService/Logs。"
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


async def configure_console(root, tunnel_id):
    """通过官方接口配置主人选中的 TCP 隧道，复核后原子发布后台入口。"""
    if BUSY.locked():
        return "另一项 Sakura Frp 操作正在进行，请稍后重试。"
    async with BUSY:
        try:
            profile = Path(os.environ["LOCALAPPDATA"]) / "QQBot/SakuraFrp/config.json"
            token = json.loads(profile.read_text(encoding="utf-8-sig"))["token"]
            if not token:
                return "请先通过主人私聊完成 /樱花登录。"
            async with httpx.AsyncClient(
                base_url="https://api.natfrp.com/v4/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            ) as client:

                async def lookup():
                    rows = (await client.get("tunnels")).raise_for_status().json()
                    return next((row for row in rows if row["id"] == tunnel_id), None)

                row = await lookup()
                if not row or row["type"] != "tcp" or row["status"] != 0:
                    return "该账号没有这条可用的 TCP 隧道，请核对 ID 和隧道状态。"
                nodes = (await client.get("nodes")).raise_for_status().json()
                node = nodes[str(row["node"])]
                url = external_origin(f"https://{node['host']}:{int(row['remote'])}")
                extra = {}
                for line in row["extra"].splitlines():
                    if not line.strip():
                        continue
                    key, sep, value = line.partition("=")
                    if not sep or key.strip() in extra:
                        return "已有高级配置无法安全合并，请先在网页端核对。"
                    extra[key.strip()] = value.strip()
                desired = dict(extra, auto_https="auto")
                if not (extra.get("auth_pass") or extra.get("auth_totp")):
                    desired["auth_pass"] = secrets.token_urlsafe(32)
                if extra.get("auth_mode") == "standalone" and not extra.get("auth_pass"):
                    return "该隧道使用独立认证，请先在网页端设置访问密码。"
                changed = row["local_ip"] != HOST or row["local_port"] != PORT or desired != extra
                if changed:
                    if row["online"]:
                        return "请先停止这条隧道，再发送配置指令，让新认证配置在下次启动时生效。"
                    payload = {
                        "id": tunnel_id,
                        "local_ip": HOST,
                        "local_port": PORT,
                        "extra": "\n".join(f"{k} = {v}" for k, v in desired.items()),
                    }
                    (await client.post("tunnel/edit", json=payload)).raise_for_status()
                    row = await lookup()
                verified = dict(
                    line.split("=", 1) for line in row["extra"].splitlines() if "=" in line
                )
                options = {k.strip(): v.strip() for k, v in verified.items()}
                if (
                    row["local_ip"] != HOST
                    or row["local_port"] != PORT
                    or options.get("auto_https") != "auto"
                    or not (options.get("auth_pass") or options.get("auth_totp"))
                    or options.get("plugin_host_header_rewrite")
                    or options.get("auto_https_mode", "") not in ("", "http", "passthrough")
                ):
                    return "隧道目标、HTTPS 或认证配置未能核实，未发布后台入口。"
                url = external_origin(
                    f"https://{nodes[str(row['node'])]['host']}:{int(row['remote'])}"
                )
            directory = root / "data"
            directory.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
                temporary = Path(file.name)
            try:
                temporary.write_text(url, encoding="ascii")
                temporary.replace(directory / "sakura-console.url")
            finally:
                temporary.unlink(missing_ok=True)
            state = "隧道在线。" if row["online"] else "请在宿主机 Sakura 启动器启用这条隧道。"
            return f"已配置后台入口：{url}。{state}首次访问需完成 Sakura 访问认证。"
        except FileNotFoundError:
            return "未找到 Sakura 登录配置，请先在主人私聊完成 /樱花登录。"
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            return "未能完成配置，请核对 Sakura 登录、隧道编辑权限和网络后重试。"
