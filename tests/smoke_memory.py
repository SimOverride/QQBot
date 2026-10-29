"""使用独立虚构库验证真实 Basic Memory MCP，不读取项目聊天或密钥。"""

import asyncio
import tempfile
from pathlib import Path

from plugins.assistant.memory_index import Index
from plugins.assistant.obsidian import Workbench
from plugins.assistant.vault import Vault


async def main():
    executable = Path(__file__).resolve().parents[1] / "data/memory-engine/Scripts/basic-memory.exe"
    with tempfile.TemporaryDirectory(prefix="qqbot-memory-smoke-") as folder:
        root = Path(folder)
        vault = Vault(root, 999)
        note = vault.set_field(123, "测试偏好", "DeploymentTest purple paper airplane", manual=True)
        workbench = Workbench(root, 999)
        workbench.prepare()
        workbench.draft(note["id"])
        (vault.path / "工作台" / (note["id"] + ".md")).write_text(
            "OnlyDraftGhost123", encoding="utf-8"
        )
        index = Index(root, 999, executable)
        try:
            await index.start()
            assert index.ready, "真实记忆引擎未启动"
            found = []
            for _ in range(15):
                found = await index.search("DeploymentTest")
                if found:
                    break
                await asyncio.sleep(1)
            assert any(n["id"] == note["id"] for n in found), "真实索引未检索到测试笔记"
            assert vault.get(note["id"])["revision"] == note["revision"], "索引意外改写了笔记"
            assert not await index.search("OnlyDraftGhost123"), "待审稿不应进入正式检索"
            print("真实 Basic Memory MCP 启动、检索、原文保护与 Obsidian 草稿排除验证通过")
        finally:
            await index.close()


if __name__ == "__main__":
    asyncio.run(main())
