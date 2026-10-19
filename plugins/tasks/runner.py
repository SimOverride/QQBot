"""本机 dsh 子进程及有界事件协议，不拼接 shell 命令。"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

from .desktop import installation, launch
from .process import ProcessJob


def environment():
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
    return {k: v for k, v in os.environ.items() if k.upper() in allowed}


async def stop_process(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def process_options():
    return (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )


class Runner:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.processes = {}

    def package(self):
        path = installation(self.config.dsh_package_dir)
        if not self.config.deepseek_api_key.get_secret_value():
            raise ValueError("未配置 DEEPSEEK_API_KEY")
        return path.resolve()

    def workspace(self, task):
        return self.root / "data/task-workspaces" / task["id"] / "work"

    async def run(self, task, prompt, emit, supplements):
        package = self.package()
        work = self.workspace(task)
        work.mkdir(parents=True, exist_ok=True)
        home = work.parent / "home"
        home.mkdir(exist_ok=True)
        patch = work.parent / "bridge.patch.json"
        disabled = [
            "headless-runner",
            "headless-startup",
            "session-title-llm",
            "tool-subagent",
            "tool-subagent-fork",
            "tool-workflow",
            "tool-ralph",
        ]
        entries = [{"id": name, "disabled": True} for name in disabled]
        entries.extend(
            [
                {
                    "id": "agent-default-model",
                    "config": {"provider": "deepseek-official", "model": self.config.dsh_model},
                },
                {"id": "approval", "config": {"policy": "never"}},
                {
                    "id": "permission",
                    "config": {
                        "defaultPreset": "workspace-write",
                        "presets": {
                            "workspace-write": {"sandbox": "workspace-write", "approval": "never"}
                        },
                    },
                },
                {
                    "insert": [
                        {
                            "id": "qqbot-bridge",
                            "name": Path(__file__).with_name("bridge.mjs").resolve().as_uri(),
                        }
                    ]
                },
            ]
        )
        patch.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        env = environment()
        env.update(
            DSH_HOME=str(home),
            DSH_TELEMETRY_DISABLED="1",
            DSH_PERMISSION_MODE="workspace-write",
            DEEPSEEK_API_KEY=self.config.deepseek_api_key.get_secret_value(),
            QQBOT_DSH_PACKAGE=str(package),
            QQBOT_MAX_STEPS=str(self.config.task_max_steps),
        )
        process = await asyncio.create_subprocess_exec(
            *launch(package, env),
            "--profile",
            "headless",
            "--patch",
            str(patch),
            cwd=work,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=262144,
            **process_options(),
        )
        self.processes[task["id"]] = process
        try:
            job = ProcessJob(process.pid)
        except BaseException:
            await stop_process(process)
            self.processes.pop(task["id"], None)
            raise
        sent, accepted = set(), set()
        final_reason = None

        async def diagnostics():
            tail = b""
            while chunk := await process.stderr.read(4096):
                tail = (tail + chunk)[-12000:]
            return tail.decode("utf-8", errors="replace")

        diagnostic = asyncio.create_task(diagnostics())

        async def send(item):
            process.stdin.write((json.dumps(item, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()

        async def pump():
            while True:
                for item in supplements():
                    if item["id"] not in sent:
                        await send({"type": "supplement", "id": item["id"], "text": item["text"]})
                        sent.add(item["id"])
                await asyncio.sleep(0.2)

        worker = None
        try:
            async with asyncio.timeout(self.config.task_timeout_seconds):
                await send({"type": "prompt", "id": "initial", "text": prompt})
                worker = asyncio.create_task(pump())
                while line := await process.stdout.readline():
                    if not line.startswith(b"QQBOT_EVENT "):
                        continue
                    event = json.loads(line[len(b"QQBOT_EVENT ") :])
                    kind, data = event["type"], event["data"]
                    emit(kind, data)
                    if kind == "accepted":
                        accepted.add(data["id"])
                    if kind == "error":
                        raise RuntimeError(data["message"])
                    if kind == "idle" and "initial" in accepted:
                        pending = {s["id"] for s in supplements()}
                        if not pending.issubset(accepted):
                            continue
                        final_reason = data.get("reason", {}).get("kind")
                        await send({"type": "close"})
                        break
                code = await process.wait()
                if code != 0 or final_reason != "completed":
                    emit("diagnostic", {"stderr": await diagnostic})
                    raise RuntimeError("dsh 未正常完成任务，详见任务事件")
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await stop_process(process)
            job.close()
            await diagnostic
            self.processes.pop(task["id"], None)
