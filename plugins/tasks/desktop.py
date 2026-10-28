"""定位客户端随附 Harness，读取 ASAR 元数据，不改动个人安装。"""

import json
import os
import struct
import subprocess
from functools import lru_cache
from pathlib import Path


class InstallationError(ValueError):
    """客户端安装或必要运行能力不可用，可向维护者展示的固定诊断。"""


def archive_entry(archive, path):
    with archive.open("rb") as stream:
        _, size, _, length = struct.unpack("<4I", stream.read(16))
        if length > 32 * 1024 * 1024:
            raise ValueError("DSH 客户端归档头超出限制")
        node = json.loads(stream.read(length))
        for part in path.split("/"):
            node = node["files"][part]
        return node, 8 + size


def archive_json(archive, path):
    node, offset = archive_entry(archive, path)
    with archive.open("rb") as stream:
        stream.seek(offset + int(node["offset"]))
        return json.loads(stream.read(node["size"]))


@lru_cache(maxsize=16)
def check_runtime(package, signature):
    """按安装文件状态缓存能力探测；客户端更新后重新检查，不比较版本号。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
    }
    executable = launch(Path(package), env)[0]
    env["QQBOT_DSH_PACKAGE"] = package
    try:
        result = subprocess.run(
            [executable, "--expose-internals", str(Path(__file__).with_name("desktop_check.mjs"))],
            env=env,
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise InstallationError("DSH 运行能力检查超时，请检查客户端安装。") from None
    except OSError:
        raise InstallationError("无法启动 DSH 随附运行时，请检查客户端安装。") from None
    prefix = b"QQBOT_DSH_CHECK "
    lines = [line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)]
    try:
        report = json.loads(lines[-1])
        missing = report["missing"]
        if not isinstance(missing, list) or not all(
            isinstance(name, str)
            and len(name) < 160
            and all(c.isalnum() or c in "@/._:-" for c in name)
            for name in missing
        ):
            raise ValueError
    except (IndexError, ValueError, KeyError, TypeError):
        raise InstallationError("DSH 运行能力检查未完成，请检查客户端安装。") from None
    if missing:
        raise InstallationError("DSH 缺少项目所需运行能力：" + "、".join(missing))
    if result.returncode:
        raise InstallationError("DSH 运行能力检查失败，请检查客户端安装。")


def installation(configured=""):
    candidates = [Path(configured)] if configured else []
    if not configured and os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for branch in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ):
                try:
                    with winreg.OpenKey(hive, branch) as root:
                        for index in range(winreg.QueryInfoKey(root)[0]):
                            try:
                                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                    title = winreg.QueryValueEx(key, "DisplayName")[0]
                                    if "DeepSeek Harness" not in title:
                                        continue
                                    folder = winreg.QueryValueEx(key, "InstallLocation")[0]
                                    candidates.append(Path(folder))
                            except OSError:
                                continue
                except OSError:
                    continue
        candidates.extend(
            [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DeepSeek Harness",
                Path(os.environ.get("PROGRAMFILES", "")) / "DeepSeek Harness",
                Path("D:/DeepSeek Harness"),
            ]
        )
    for root in candidates:
        if root.name.lower().endswith(".exe"):
            root = root.parent
        archive = root / "resources/app.asar"
        if not archive.is_file():
            continue
        executable = root / "DeepSeek Harness.exe"
        if not executable.is_file():
            raise InstallationError("DSH 安装缺少 DeepSeek Harness.exe，请检查客户端安装目录。")
        try:
            runtime = archive_json(archive, "dsh/node_modules/@deepseek-ai/dsh/package.json")
            archive_entry(archive, "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js")
            if not isinstance(runtime, dict):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, struct.error):
            raise InstallationError("DSH 安装缺少有效的 Harness 运行时或 CLI 入口。") from None
        package = (archive / "dsh/node_modules/@deepseek-ai/dsh").resolve()
        archive_stat, executable_stat = archive.stat(), executable.stat()
        check_runtime(
            str(package),
            (
                archive_stat.st_mtime_ns,
                archive_stat.st_size,
                executable_stat.st_mtime_ns,
                executable_stat.st_size,
            ),
        )
        return package
    raise InstallationError("未找到 DSH 客户端，请将 DSH_PACKAGE_DIR 设置为客户端安装目录")


def launch(package, env):
    archive = next(p for p in package.parents if p.name == "app.asar")
    env["ELECTRON_RUN_AS_NODE"] = "1"
    return [
        str(archive.parent.parent / "DeepSeek Harness.exe"),
        "--expose-internals",
        str(archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"),
    ]
