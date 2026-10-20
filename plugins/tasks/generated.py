"""生成插件的测试、不可变版本发布及独立进程调用。"""

import asyncio
import hashlib
import json
import shutil
import sys

from .process import ProcessJob
from .runner import environment, process_options, stop_process

SPEC = """
本任务需要交付一个 Python 3.12 群插件。只使用标准库。
在当前目录创建 plugin.py、manifest.json、test_plugin.py。
manifest.json 结构为 {"title":"中文短名称","description":"工具适用场景"}。
plugin.py 定义 run(request)，接收 {"text":用户请求,"data":当前群的插件数据字典}，
返回 {"text":中文结果,"data":更新后的数据字典}，只通过返回数据持久化。
不得自行连接 QQ、指定群号、访问环境密钥、修改工作目录外文件或启动后台进程。
通过标准输入接收一行 JSON，调用 run，标准输出仅打印一行 JSON（不打印调试文字）。
test_plugin.py 使用 unittest，按实际功能与风险选择有效测试，自行运行并修复；不得空测试凑数。
这是按需调用插件；不具备自主定时或消息订阅能力，如需求依赖这些能力则明确说明，不伪装完成。
不要修改已有部署；后端在独立快照上重新测试，成功后自动发布，群范围由后端绑定。
"""


async def invoke(args, cwd, payload=None, timeout=60):
    args = [args[0], "-X", "utf8", *args[1:]]
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=environment(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_options(),
    )
    try:
        job = ProcessJob(process.pid)
    except BaseException:
        await stop_process(process)
        raise

    async def read(stream):
        chunks, size = [], 0
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > 128000:
                raise ValueError("插件输出超过限制")
            chunks.append(chunk)
        return b"".join(chunks)

    readers = [asyncio.create_task(read(stream)) for stream in (process.stdout, process.stderr)]
    try:
        async with asyncio.timeout(timeout):
            if payload is not None:
                process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
            process.stdin.close()
            output, error = await asyncio.gather(*readers)
            code = await process.wait()
            if code:
                raise ValueError("插件进程失败：" + error.decode("utf-8", errors="replace")[-2000:])
            return output.decode("utf-8")
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await stop_process(process)
        job.close()


def result(text):
    value = json.loads(text)
    if (
        not isinstance(value, dict)
        or set(value) != {"text", "data"}
        or not isinstance(value["text"], str)
        or len(value["text"]) > 6000
        or not isinstance(value["data"], dict)
        or len(json.dumps(value)) > 64000
    ):
        raise ValueError("插件返回格式无效")
    return value


def digest(folder):
    return hashlib.sha256(
        b"".join(
            (folder / n).read_bytes() for n in ("plugin.py", "manifest.json", "test_plugin.py")
        )
    ).hexdigest()


async def publish(manager, task):
    work = manager.runner.workspace(task)
    names = ("plugin.py", "manifest.json", "test_plugin.py")
    for name in names:
        file = work / name
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 200000:
            raise ValueError("插件交付不完整或文件大小超限")
        if not file.resolve().is_relative_to(work.resolve()):
            raise ValueError("插件产物路径越界")
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8-sig"))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"title", "description"}
        or any(
            not isinstance(manifest[k], str) or not 1 <= len(manifest[k]) <= limit
            for k, limit in (("title", 40), ("description", 400))
        )
    ):
        raise ValueError("插件清单格式无效")
    identity = "gen_" + task["id"][2:]
    version = digest(work)
    folder = manager.root / "data/generated" / identity / version
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(work / name, folder / name)
    # 不信任 dsh 的“测试通过”文字，后端重新执行测试并核对测试后的文件哈希。
    test = (
        "import sys,unittest;sys.path.insert(0,'.');"
        "s=unittest.defaultTestLoader.discover('.',pattern='test_plugin.py');"
        "n=s.countTestCases();r=unittest.TextTestRunner().run(s);"
        "sys.exit(0 if n>0 and r.wasSuccessful() else 1)"
    )
    await invoke([sys.executable, "-I", "-B", "-c", test], folder)
    result(
        await invoke([sys.executable, "-I", "-B", "plugin.py"], folder, {"text": "", "data": {}})
    )
    if digest(folder) != version:
        raise ValueError("测试修改了插件产物，拒绝发布")
    current = manager.store.get(task["id"])
    if current["status"] != "testing":
        raise ValueError("任务状态已变化，取消发布")
    extensions = manager.extensions
    if extensions is None:
        raise ValueError("插件系统不可用")
    value = extensions.store.read()
    entries = value["bots"].setdefault(str(task["bot"]), {})
    old = entries.get(identity)
    entry = old or {
        "enabled": True,
        "scope": [str(task["group"])],
        "groups": {str(task["group"]): True},
        "data": {},
    }
    if entry["scope"] != [str(task["group"])]:
        raise ValueError("插件发布群范围不匹配")
    entries[identity] = entry
    metadata = {
        "version": version,
        "title": manifest["title"],
        "description": manifest["description"],
    }
    data = entry["data"].setdefault(str(task["group"]), {})
    data["_runtime"] = metadata
    data.setdefault("state", {})
    extensions.store.write(value)
    extensions.plugins[identity] = Generated(identity, manager.root, metadata)
    manager.record(
        task["id"], "published", {"plugin": identity, "version": version, "group": task["group"]}
    )
    return identity


class Generated:
    def __init__(self, identity, root, metadata=None):
        self.id, self.root = identity, root
        metadata = metadata or {}
        self.title, self.command_name = metadata.get("title", identity), "/" + identity
        self.lock = asyncio.Lock()
        self.functions = [
            {
                "name": "use_" + identity,
                "description": metadata.get("description", "调用本群生成插件"),
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]

    async def execute(self, session, args):
        if (
            not isinstance(args, dict)
            or set(args) != {"text"}
            or not isinstance(args["text"], str)
            or len(args["text"]) > 8000
        ):
            return {"status": "invalid_arguments"}
        async with self.lock:
            manager, (bot, group, _) = session.manager, session.key
            value = manager.store.read()
            entry = manager.entry(bot, self.id, value)
            data = entry["data"][str(group)]
            version = data["_runtime"]["version"]
            if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
                return {"status": "invalid_version"}
            folder = self.root / "data/generated" / self.id / version
            try:
                if digest(folder) != version:
                    return {"status": "artifact_changed"}
                output = result(
                    await invoke(
                        [sys.executable, "-I", "-B", "plugin.py"],
                        folder,
                        {"text": args["text"], "data": data.get("state", {})},
                    )
                )
                value = manager.store.read()
                current = manager.entry(bot, self.id, value)
                if (
                    not manager.enabled(bot, group, self.id)
                    or current["data"][str(group)]["_runtime"]["version"] != version
                ):
                    return {"status": "plugin_changed", "hint": "执行期间插件已变化，未保存结果"}
                current["data"][str(group)]["state"] = output["data"]
                manager.store.write(value)
                return {"status": "completed", "text": output["text"]}
            except (OSError, ValueError, TimeoutError):
                return {"status": "plugin_failed"}

    async def command(self, session, args):
        return json.dumps(await self.execute(session, {"text": " ".join(args)}), ensure_ascii=False)

    async def tick(self, manager, bots, now):
        return


def load(extensions, root):
    try:
        value = extensions.store.read()
    except (ValueError, OSError):
        return
    for entries in value["bots"].values():
        for name, entry in entries.items():
            if name.startswith("gen_") and any("_runtime" in d for d in entry["data"].values()):
                metadata = next(d["_runtime"] for d in entry["data"].values() if "_runtime" in d)
                extensions.plugins[name] = Generated(name, root, metadata)
