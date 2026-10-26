"""仅当前所有者明确提出的自更新请求可获得一次更新授权。"""

import asyncio
import os
import re
import signal
import subprocess

from migration import ROOT

from .identity import permission_level
from .prompt_store import tool_definition


class Maintenance:
    def __init__(self, config, key, text):
        self.config, self.key = config, key
        # 完整匹配当前消息；引用、历史或工具参数都不能提供操作授权。
        self.explicit = bool(re.fullmatch(
            r"(?:请|帮我)?\s*(?:(?:用|使用)\s*[Gg][Ii][Tt]\s*)?"
            r"(?:更新|升级)(?:一下)?(?:机器人|你自己|自己|代码)?"
            r"(?:\s*(?:并|然后|后|再|并且)\s*重启(?:机器人|你自己|自己)?)?[。！!\s]*",
            text.strip(),
        ))
        self.requested = False

    def allowed(self):
        return self.explicit and permission_level(self.config, self.key[2]) == 2

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
