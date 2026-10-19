"""定位客户端随附 Harness，读取 ASAR 元数据，不改动个人安装。"""

import json
import os
import struct
from pathlib import Path

VERSION = "0.2.1-rc.2"


def archive_json(archive, path):
    with archive.open("rb") as stream:
        _, size, _, length = struct.unpack("<4I", stream.read(16))
        if length > 32 * 1024 * 1024:
            raise ValueError("DSH 客户端归档头超出限制")
        node = json.loads(stream.read(length))
        for part in path.split("/"):
            node = node["files"][part]
        stream.seek(8 + size + int(node["offset"]))
        return json.loads(stream.read(node["size"]))


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
        version = archive_json(archive, "package.json").get("version")
        runtime = archive_json(archive, "dsh/node_modules/@deepseek-ai/dsh/package.json")
        if version != VERSION or runtime.get("version") != VERSION:
            raise ValueError(f"请使用 DSH 客户端 {VERSION}；当前适配器不自动切换版本")
        if not (root / "DeepSeek Harness.exe").is_file():
            continue
        return archive / "dsh/node_modules/@deepseek-ai/dsh"
    raise ValueError("未找到 DSH 客户端，请将 DSH_PACKAGE_DIR 设置为客户端安装目录")


def launch(package, env):
    archive = next(p for p in package.parents if p.name == "app.asar")
    env["ELECTRON_RUN_AS_NODE"] = "1"
    return [
        str(archive.parent.parent / "DeepSeek Harness.exe"),
        "--expose-internals",
        str(archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"),
    ]
