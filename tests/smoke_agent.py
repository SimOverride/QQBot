"""手动验证当前客户端原生持续会话；使用真实 API，不发送 QQ 消息。"""

import asyncio
import base64
import json
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from dotenv import dotenv_values

from plugins.assistant.config import Config
from plugins.assistant.dsh import Turn


async def main():
    env = dotenv_values(".env")
    cfg = Config(deepseek_api_key=env.get("DEEPSEEK_API_KEY", ""), model_trace_enabled=False)
    with tempfile.TemporaryDirectory(prefix="qqbot-agent-test-") as directory:
        root = Path(directory)
        for index, question in enumerate(
            ("请记住本会话测试口令是蓝色纸鹤，仅回复收到。", "上一回合测试口令是什么？仅回答口令。")
        ):

            def chunk(kind, data):
                return (
                    struct.pack(">I", len(data))
                    + kind
                    + data
                    + struct.pack(">I", zlib.crc32(kind + data))
                )

            pixels = bytearray()
            for y in range(300):
                pixels.append(0)
                for x in range(400):
                    inside = (
                        60 < x < 300 and 60 < y < 240
                        if index == 0
                        else (x - 200) ** 2 + (y - 150) ** 2 < 110**2
                    )
                    pixels.extend(
                        (255, 0, 0)
                        if inside and index == 0
                        else (0, 0, 255)
                        if inside
                        else (255, 255, 255)
                    )
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 400, 300, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(pixels))
                + chunk(b"IEND", b"")
            )
            question += " 同时说明这次图片的颜色和形状。"
            parts = [
                {"type": "text", "text": question},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
                },
            ]
            turn = Turn(cfg, f"smoke-{index}")
            turn.runtime = root / "runtime"
            turn.view = {
                "id": "qqbot-123-group-10",
                "title": "测试群（10）",
                "home": str(root / "view"),
                "cwd": str(root),
                "user": question,
            }
            try:
                async with asyncio.timeout(120):
                    text, calls, _ = await turn.step(
                        [
                            {"role": "system", "content": "这是隔离测试，仅按用户要求回答。"},
                            {"role": "user", "content": parts},
                        ],
                        False,
                        [],
                    )
                assert not calls
                assert "红" in text if index == 0 else "蓝" in text and "圆" in text, (
                    "实际图片识别失败"
                )
                if index == 1:
                    assert "蓝色纸鹤" in text, "恢复会话未保留上轮上下文"
            finally:
                await turn.close()
        from plugins.tasks.desktop import launch
        from plugins.tasks.runner import Runner, environment

        package = Runner(root, cfg).package()
        native_env = environment()
        executable = launch(package, native_env)[0]
        native_env["QQBOT_DSH_PACKAGE"] = str(package)
        checked = await asyncio.to_thread(
            subprocess.run,
            [
                executable,
                "--expose-internals",
                str(Path(__file__).with_name("dsh_view_check.mjs").resolve()),
                "read",
                str(root / "view"),
                "qqbot-123-group-10",
            ],
            env=native_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert checked.returncode == 0, "浏览镜像读取失败"
        counts = json.loads(checked.stdout)
        assert counts["users"] == 2 and counts["assistants"] == 2, counts
        turn = Turn(cfg, "smoke-tools")
        try:
            functions = [
                {
                    "name": name,
                    "description": "独立读取测试标记",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
                for name in ("read_alpha", "read_beta")
            ]
            messages = [
                {"role": "system", "content": "先调用所有要求的工具，再根据结果回答。"},
                {
                    "role": "user",
                    "content": ("请调用 read_alpha 和 read_beta 两个工具，"
                                "读取结果后说明随工具结果附上的图片颜色和形状。"),
                },
            ]
            seen = set()
            async with asyncio.timeout(120):
                for _ in range(5):
                    text, calls, output = await turn.step(messages, True, functions)
                    if not calls:
                        break
                    messages.extend(output)
                    for call in calls:
                        seen.add(call["name"])
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["call_id"],
                                "content": "测试读取成功",
                            }
                        )
                    if any(call["name"] == "read_alpha" for call in calls):
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "工具返回的原图，不是新请求"},
                                    parts[-1],
                                ],
                            }
                        )
            assert seen == {"read_alpha", "read_beta"}, seen
            assert "蓝" in text and "圆" in text, "工具返回的原图未被识别"
        finally:
            await turn.close()
        print("客户端原生两轮图片识别、上下文恢复、工具执行和浏览镜像通过。")


if __name__ == "__main__":
    asyncio.run(main())
