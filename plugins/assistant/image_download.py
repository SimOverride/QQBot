"""限制图片下载大小，校验公网地址并固定连接目标。"""

import asyncio
import http.client
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .http import ServiceError


def public_dns(host):
    # 使用固定公网 HTTPS 解析入口，避免代理的虚拟地址影响目标校验。
    try:
        with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
            response = client.get(
                "https://1.1.1.1/dns-query",
                params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            response.raise_for_status()
            data = response.json()
        if data.get("Status") != 0:
            raise ValueError
        return [item["data"] for item in data.get("Answer", []) if item.get("type") == 1]
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise ServiceError("图片公网 DNS 解析失败，请换一个候选或稍后重试。") from None


def public_target(url):
    if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() for c in url):
        raise ServiceError("图片地址无效。")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        ips = [entry[4][0] for entry in addresses]
        # 仅域名的纯 Fake-IP 响应允许重新解析；显式 IP 和混合私网响应仍拒绝。
        try:
            ipaddress.ip_address(parsed.hostname)
            literal = True
        except ValueError:
            literal = False
        if (
            not literal
            and ips
            and all(ipaddress.ip_address(ip) in ipaddress.ip_network("198.18.0.0/15") for ip in ips)
        ):
            ips = public_dns(parsed.hostname)
        if not ips or not all(
            ipaddress.ip_address(ip).is_global and not ipaddress.ip_address(ip).is_multicast
            for ip in ips
        ):
            raise ValueError
        return parsed, port, ips[0]
    except (ValueError, OSError):
        raise ServiceError("图片地址不是可访问的公网地址。") from None


def _download(url, max_bytes):
    deadline = time.monotonic() + 20
    for _ in range(4):
        parsed, port, ip = public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("图片下载超时。")
        cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = cls(parsed.hostname, port, timeout=min(8, remaining))
        # 连接已校验地址，同时保留原域名的 TLS SNI 和 Host。
        connection._create_connection = lambda address, timeout, *a: socket.create_connection(
            (ip, port), timeout
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET",
                path,
                headers={"User-Agent": "QQBot-Emote/1.0", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ServiceError("图片跳转无效。")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ServiceError("图片下载失败。")
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ServiceError("图片超过大小上限。")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ServiceError("图片下载超时。")
                chunk = response.read(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ServiceError("图片超过大小上限。")
        except (OSError, ValueError, http.client.HTTPException):
            raise ServiceError("图片下载失败。") from None
        finally:
            connection.close()
    raise ServiceError("图片跳转次数过多。")


async def download_image(url, max_bytes):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_download, url, max_bytes), 25)
    except TimeoutError:
        raise ServiceError("图片下载超时。") from None
