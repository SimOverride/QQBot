"""Basic Memory 独立本机 MCP 索引；检索结果必须回读正式笔记后才能使用。"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from plugins.tasks.process import ProcessJob
from plugins.tasks.runner import process_options, stop_process

from .vault import Vault, atomic


class Index:
    def __init__(self, root, bot, executable=None):
        self.root, self.bot = Path(root), bot
        self.vault = Vault(root, bot)
        self.state = self.root / "data/memory-index" / str(bot)
        self.process = self.job = None
        self.serial = 0
        self.lock = asyncio.Lock()
        self.ready = False
        self.executable = executable

    def status(self, state, error=""):
        atomic(
            self.state / "status.json",
            json.dumps({"state": state, "error": error, "updated": time.time(), "mode": "text"}),
        )

    async def start(self):
        executable = self.root / "data/memory-engine/Scripts/basic-memory.exe"
        if os.name != "nt":
            executable = self.root / "data/memory-engine/bin/basic-memory"
        executable = self.executable or executable
        if not executable.exists():
            self.status("unavailable", "请运行 scripts/setup_memory.ps1 安装记忆引擎")
            return
        self.state.mkdir(parents=True, exist_ok=True)
        self.vault.path.mkdir(parents=True, exist_ok=True)
        config = {
            "projects": {"main": {"path": str(self.vault.path.resolve()), "mode": "local"}},
            "default_project": "main",
            "auto_update": False,
            "index_changes": True,
            "ensure_frontmatter_on_sync": False,
            "default_search_type": "text",
            "logfire_enabled": False,
            "logfire_send_to_logfire": False,
            "log_level": "ERROR",
        }
        atomic(self.state / "config.json", json.dumps(config))
        env = {
            **os.environ,
            "BASIC_MEMORY_CONFIG_DIR": str(self.state.resolve()),
            "BASIC_MEMORY_HOME": str(self.vault.path.resolve()),
            "BASIC_MEMORY_AUTO_UPDATE": "false",
            "BASIC_MEMORY_LOG_LEVEL": "ERROR",
            "PYTHONUTF8": "1",
        }
        self.status("starting")
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(executable),
                "mcp",
                "--project",
                "main",
                "--transport",
                "stdio",
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024 * 1024,
                **process_options(),
            )
            self.job = ProcessJob(self.process.pid)
            async with asyncio.timeout(45):
                await self.rpc(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "qqbot", "version": "1"},
                    },
                )
                self.process.stdin.write(
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
                    + b"\n"
                )
                await self.process.stdin.drain()
            self.ready = True
            self.status("ready")
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            self.status("failed", type(error).__name__)

    async def rpc(self, method, params):
        self.serial += 1
        ident = self.serial
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
            + b"\n"
        )
        await self.process.stdin.drain()
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise RuntimeError("记忆引擎已退出")
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if result.get("id") != ident:
                continue
            if "error" in result:
                raise RuntimeError("记忆引擎请求失败")
            return result.get("result", {})

    async def search(self, query):
        if not self.ready:
            return []
        try:
            async with asyncio.timeout(1.5), self.lock:
                result = await self.rpc(
                    "tools/call",
                    {
                        "name": "search_notes",
                        "arguments": {
                            "query": query[:400],
                            "project": "main",
                            "search_type": "text",
                            "output_format": "json",
                            "page_size": 12,
                        },
                    },
                )
            if result.get("isError"):
                raise RuntimeError("记忆检索失败")
            # 索引中的摘要不直接注入；即使索引落后也必须通过当前来源与固定值校验。
            text = json.dumps(result, ensure_ascii=False)
            ids = set(re.findall(r"(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])", text))
            notes = [n for n in self.vault.list() if n["id"] in ids]
            self.status("ready")
            return notes[:6]
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.process is None or self.process.returncode is not None:
                self.ready = False
            self.status("degraded", type(error).__name__)
            return []

    async def close(self):
        self.ready = False
        if self.process:
            # 先以协议 EOF 请求正常退出，等所有继承管道关闭后再释放作业对象。
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.communicate(), 5)
            except TimeoutError:
                if self.job:
                    self.job.close()
                    self.job = None
                await stop_process(self.process)
                try:
                    await asyncio.wait_for(self.process.communicate(), 3)
                except TimeoutError:
                    pass
            self.process = None
        if self.job:
            self.job.close()
            self.job = None


class Indexes:
    def __init__(self, root):
        self.root = root
        self.items, self.tasks, self.started = {}, {}, {}

    async def search(self, bot, query):
        if bot not in self.items:
            item = self.items[bot] = Index(self.root, bot)
            self.tasks[bot] = asyncio.create_task(item.start())
            self.started[bot] = time.monotonic()
            return []
        if (
            not self.items[bot].ready
            and self.tasks[bot].done()
            and time.monotonic() - self.started[bot] >= 60
        ):

            async def restart():
                await self.items[bot].close()
                await self.items[bot].start()

            self.started[bot] = time.monotonic()
            self.tasks[bot] = asyncio.create_task(restart())
            return []
        return await self.items[bot].search(query)

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await asyncio.gather(*(item.close() for item in self.items.values()))
