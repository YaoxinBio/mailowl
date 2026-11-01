"""研究 Agent 的联网工具（OpenAI 兼容后端用；Claude CLI 后端用 Claude Code 自带的 WebSearch/WebFetch）。

研究问题可能间接受邮件内容影响（提示注入），所以 web_fetch 拒绝访问内网和本机：
IP 字面量必须是公网地址；域名不能是 localhost / .local 这类本地名字，解析结果不能落在内网段。
198.18.0.0/15 例外放行：这是 Clash/mihomo TUN 的 fake-ip 段，开着 TUN 时所有域名都解析到这里。
每一跳重定向都重新检查。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .mailparse import html_to_text

MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECTS = 5
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".home", ".corp", ".localhost", ".home.arpa")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


class WebError(Exception):
    pass


def check_url(url: str, resolve=socket.getaddrinfo) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise WebError("只允许 http / https 网址")
    host = (u.hostname or "").rstrip(".").lower()
    if not host:
        raise WebError("网址里没有主机名")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise WebError(f"拒绝访问内网或本机地址 {host}")
        return
    if host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES):
        raise WebError(f"拒绝访问本地主机名 {host}")
    try:
        infos = resolve(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise WebError(f"无法解析 {host}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global and addr not in FAKE_IP_NET:
            raise WebError(f"{host} 解析到内网或本机地址，拒绝访问")


async def web_fetch(url: str, max_chars: int = 12000, *, transport: httpx.AsyncBaseTransport | None = None,
                    resolve=socket.getaddrinfo) -> dict[str, Any]:
    async with httpx.AsyncClient(follow_redirects=False, timeout=20, transport=transport,
                                 headers={"User-Agent": UA}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url, resolve)
            async with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise WebError(f"HTTP {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_FETCH_BYTES:
                        break
                ctype = r.headers.get("content-type", "")
                charset = r.charset_encoding or "utf-8"
            text = bytes(buf[:MAX_FETCH_BYTES]).decode(charset, "replace")
            if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                text = html_to_text(text)
            elif not ctype.startswith(("text/", "application/json", "application/xml")):
                raise WebError(f"不是网页内容（{ctype or '未知类型'}）")
            return {"url": url, "content": text[:max_chars], "truncated": len(text) > max_chars}
    raise WebError("重定向次数过多")


async def web_search(query: str, api_key: str, max_results: int = 5, *,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Tavily 搜索（https://tavily.com，免费额度每月 1000 次）。"""
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        r = await client.post("https://api.tavily.com/search",
                              headers={"Authorization": f"Bearer {api_key}"},
                              json={"query": query, "max_results": max(1, min(max_results, 10))})
    if r.status_code >= 400:
        raise WebError(f"搜索接口返回 {r.status_code}: {r.text[:200]}")
    results = r.json().get("results") or []
    return {"results": [{"title": x.get("title"), "url": x.get("url"), "snippet": (x.get("content") or "")[:600]}
                        for x in results]}
