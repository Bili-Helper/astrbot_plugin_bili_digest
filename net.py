"""Bounded HTTP reads and redirect validation for untrusted media links."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .extract import allowed_url
from .process import DigestError


async def public_url(url, domains=None):
    p = urlsplit(url)
    if (
        p.scheme not in ("http", "https")
        or not p.hostname
        or p.username
        or p.password
        or p.port not in (None, 80, 443)
    ):
        raise DigestError("链接格式或端口不受支持。")
    if domains is not None and not allowed_url(url, domains):
        raise DigestError("跳转链接不在视频域名白名单内。")
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo, p.hostname, p.port or (443 if p.scheme == "https" else 80)
        )
    except OSError:
        raise DigestError("视频域名解析失败。") from None
    if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
        raise DigestError("不允许访问本机或内网地址。")


async def fetch(url, *, domains=None, limit=8 * 1024 * 1024, headers=None, params=None, timeout=30):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(6):
            await public_url(url, domains)
            async with client.stream("GET", url, headers=headers, params=params) as response:
                if response.is_redirect:
                    next_url = urljoin(str(response.url), response.headers["location"])
                    if headers and urlsplit(next_url).netloc != urlsplit(url).netloc:
                        raise DigestError("拒绝将鉴权信息发送到重定向的其他站点。")
                    url = next_url
                    params = None
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise DigestError("远端内容超过大小限制。")
                return bytes(data), str(response.url)
    raise DigestError("短链接跳转次数过多。")


async def expand(url, domains):
    # Short links need their Location only; avoid downloading the landing page.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(6):
            await public_url(url, domains)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(str(response.url), response.headers["location"])
                    continue
                response.raise_for_status()
                return str(response.url)
    raise DigestError("短链接跳转次数过多。")
