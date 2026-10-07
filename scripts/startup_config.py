"""仅读写联合启动字段，不输出其他环境配置或密钥。"""

import json
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key


def main():
    """复用项目的 dotenv 解析规则，并保留其他配置内容。"""
    root, action, *arguments = sys.argv[1:]
    path = Path(root) / ".env"
    if not path.is_file():
        raise ValueError("缺少 .env")
    if action == "read":
        values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
        # 只传递启动字段，ASCII JSON 避免 Windows 控制台编码损坏中文路径。
        print(json.dumps({
            "launcher": values.get("NAPCAT_LAUNCHER", "") or "",
            "port": values.get("NAPCAT_PORT", "6099"),
        }))
    elif action == "save" and len(arguments) == 1:
        set_key(path, "NAPCAT_LAUNCHER", arguments[0], encoding="utf-8")
    else:
        raise ValueError("无效的启动配置操作")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 不输出异常输入，避免配置中的秘密被错误信息带出。
        print("无法读写启动配置，请检查 .env 文件及权限。", file=sys.stderr)
        sys.exit(1)
