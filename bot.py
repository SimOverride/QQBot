"""Start the local OneBot v11 service."""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.log import logger

from migration import ROOT, project_lock


def configure_logging():
    import sys

    def safe_record(record):
        name = record["name"] or ""
        # Framework message/adapter logs may include complete events or API payloads.
        if name == "nonebot" and record["function"] == "handle_event":
            return False
        return not name.startswith(("nonebot.message", "nonebot.adapters"))

    logger.remove()
    logger.add(sys.stderr, level="INFO", filter=safe_record, diagnose=False, backtrace=False)
    logger.add(
        "logs/qqbot.log",
        level="INFO",
        filter=safe_record,
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
        diagnose=False,
        backtrace=False,
    )


def main():
    """初始化并运行机器人，运行锁由入口持有。"""
    nonebot.init()
    configure_logging()
    nonebot.get_driver().register_adapter(Adapter)
    plugin = nonebot.load_plugin("plugins.assistant")
    if plugin is None:
        raise RuntimeError("无法加载机器人插件")
    plugin.module.setup()
    nonebot.run()


if __name__ == "__main__":
    # 与离线导入互斥；未完成恢复时不允许启动。
    with project_lock(ROOT, runtime=True):
        main()
