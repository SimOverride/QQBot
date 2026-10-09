"""供独立本机后台读取的机器人心跳，不包含配置密钥或聊天内容。"""

import json
import time
from pathlib import Path

from .knowledge import read_directory


class Runtime:
    def __init__(self, root: Path):
        self.path = root / "data/runtime.json"
        self.started = time.time()
        self.accounts = []

    def publish(self, connected, stopped=False):
        connected = sorted(str(qq) for qq in connected)
        if connected:
            self.accounts = connected
        value = {
            "started": self.started,
            "heartbeat": time.time(),
            "connected": connected,
            "accounts": self.accounts,
            "stopped": stopped,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".pending")
        pending.write_text(json.dumps(value), encoding="utf-8")
        pending.replace(self.path)


def status(root: Path, now=None):
    """过期心跳不能继续显示在线；缓存资料可在机器人离线时展示。"""
    now = time.time() if now is None else now
    try:
        value = json.loads((root / "data/runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    fresh = 0 <= now - value.get("heartbeat", 0) < 20
    connected = value.get("connected", []) if fresh and not value.get("stopped") else []
    state = (
        "stopped"
        if value.get("stopped") or not value
        else "stale"
        if not fresh
        else "connected"
        if connected
        else "waiting"
    )
    directory = read_directory(root)
    accounts = connected or value.get("accounts", []) or sorted(directory)
    qq = next((str(item) for item in accounts if str(item).isdigit()), "")
    name = directory.get(qq, {}).get("users", {}).get(qq, {}).get("name", "")
    return {
        "state": state,
        "qq": qq,
        "nickname": name or ("昵称待同步" if qq else "QQBot"),
        "uptime": max(0, int(now - value.get("started", now)))
        if fresh and state != "stopped"
        else None,
        "connected_count": len(connected),
    }
