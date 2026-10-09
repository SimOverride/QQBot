"""为现有个人和群生成待确认建议，不修改已保存认知，不输出聊天原文。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from admin_server import create_app  # noqa: E402
from console_store import ConsoleStore  # noqa: E402


async def main():
    store = ConsoleStore(ROOT)
    inventory = store.inventory()
    resources = [r["resource"] for r in inventory["people"] + inventory["groups"]]
    app = create_app(ROOT)
    success = skipped = failed = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8090",
        headers={"X-Console-Token": app.state.token},
        timeout=240,
    ) as client:
        for index, resource in enumerate(resources, 1):
            if store.proposal(resource):
                skipped += 1
                continue
            current = store.get(resource)
            try:
                response = await client.post(
                    "/api/suggest",
                    json={
                        "resource": resource,
                        "revision": current["revision"],
                        "chat": [
                            {
                                "role": "user",
                                "content": "根据聊天重新整理认知，合并重复资料，"
                                "不以认知生成显示昵称，"
                                "总体认知及每会话印象各用一段自然文字，不分类，"
                                "保留固定修正，说明消息依据、冲突与资料不足之处。"
                                "输出JSON先写explanation键，再写value键，保持所有空字段。",
                            }
                        ],
                    },
                )
                if response.status_code == 200:
                    success += 1
                    print(f"{index}/{len(resources)} 已生成待确认建议", flush=True)
                else:
                    failed += 1
                    print(
                        f"{index}/{len(resources)} 未生成，状态码 {response.status_code}",
                        flush=True,
                    )
            except Exception as error:
                failed += 1
                print(f"{index}/{len(resources)} 请求失败：{type(error).__name__}", flush=True)
    print(f"生成 {success}，复用 {skipped}，失败 {failed}；已保存认知未修改。", flush=True)
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
