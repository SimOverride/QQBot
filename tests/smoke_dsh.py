"""手动运行的本机 dsh 冒烟测试；使用真实 API，不发送 QQ 消息。"""

import asyncio
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.memory import Memory
from plugins.extensions.manager import Manager as Extensions
from plugins.tasks.manager import Manager


async def main():
    env = dotenv_values(".env")
    config = Config(
        deepseek_api_key=env.get("DEEPSEEK_API_KEY", ""),
        bot_owners={1},
        task_timeout_seconds=240,
        model_trace_enabled=False,
    )
    with tempfile.TemporaryDirectory(prefix="qqbot-dsh-smoke-") as folder:
        root = Path(folder)
        commands = Commands(config, Memory(config))
        extensions = Extensions(root, commands, [])
        manager = Manager(root, commands, extensions=extensions)
        original = manager.record

        def report(identity, kind, data):
            original(identity, kind, data)
            print(kind, flush=True)

        manager.record = report
        try:
            task = await manager.action(
                (123, 10, 1),
                None,
                {
                    "action": "request",
                    "kind": "plugin",
                    "text": (
                        "开发极简回声插件：返回输入文字，不改变data。空输入返回空文字。"
                        "按交付规范写两个单元测试。"
                    ),
                },
            )
            await manager.execute(task["id"])
            record = manager.store.get(task["id"])
            if record["status"] != "completed":
                print(manager.store.events(task["id"], 5))
                raise AssertionError(record["phase"])
            result = await extensions.session((123, 10, 1), None).execute(
                "use_" + record["plugin"], {"text": "测试通过"}
            )
            assert result.get("text") == "测试通过", result
            print("真实 dsh 开发、独立复测、原群发布、插件调用全部通过。")
        finally:
            await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
