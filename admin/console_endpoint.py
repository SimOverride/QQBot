"""本机后台的固定监听地址。"""

import re
from pathlib import Path
from urllib.parse import urlsplit


def external_origin(value: str) -> str:
    """只允许一个完整的 HTTPS 来源，统一浏览器会省略的默认端口。"""
    if not value:
        return ""
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?/?", value):
        raise ValueError("后台外部地址必须是 HTTPS 域名，可带端口，不可含路径或凭据")
    url = urlsplit(value)
    if url.port == 0:
        raise ValueError("后台外部端口无效")
    port = f":{url.port}" if url.port not in (443, None) else ""
    return f"https://{url.hostname}{port}"


ROOT = Path(__file__).resolve().parents[1]


def console_origin(root, manual):
    """自动配置只保存已校验的公网入口，两进程共享同一个文件。"""
    try:
        value = (root / "data/sakura-console.url").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = manual
    return external_origin(value)


HOST = "127.0.0.1"
PORT = 8090
URL = f"http://{HOST}:{PORT}"
