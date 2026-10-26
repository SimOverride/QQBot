"""向所有者提供自更新工具，由模型理解当前话语并决定调用。"""

import asyncio
import os
import re
import signal
import subprocess

from migration import ROOT

from .identity import permission_level
from .prompt_store import tool_definition


class Maintenance:
    def __init__(self, config, key):
        self.config, self.key = config, key
        self.requested = False

    def allowed(self):
        return permission_level(self.config, self.key[2]) == 2

    def functions(self):
        return [tool_definition("update_bot")] if self.allowed() else []

    async def execute(self, args):
        if args != {}:
            return {"status": "invalid_arguments"}
        if not self.allowed():
            return {"status": "access_denied"}
        if not re.fullmatch(r"[0-9a-f]{48}", os.environ.get("QQBOT_UPDATE_TOKEN", "")):
            return {"status": "unavailable", "hint": "请先通过 start.bat 启动更新监护进程。"}
        from updater import check

        try:
            await asyncio.to_thread(check, ROOT)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return {"status": "blocked", "hint": "仓库预检查失败，未停机。请检查本地修改、"
                    "当前分支、远端上游和 Git 安装。"}
        self.requested = True
        return {"status": "scheduled", "hint": "本轮回复成功后停机更新，尚未更新成功。"}

    async def finish(self, send, is_current=lambda: True):
        if not self.requested or not self.allowed() or not is_current():
            return
        await send("即将停止机器人，检查当前分支上游并快进更新，同步依赖后重启后台和机器人。"
                   "有本地修改、分叉或执行失败会停止；NapCat 保持运行。")
        if not is_current():
            return
        token = os.environ["QQBOT_UPDATE_TOKEN"]
        path = ROOT / "data" / f"update-{token}.request"
        path.write_text(token, encoding="ascii")
        self.requested = False
        # 只向当前进程发送信号，交给 Uvicorn 正常关闭连接及运行锁。
        signal.raise_signal(signal.SIGINT)
