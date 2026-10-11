"""出口 IP 探测：回报"上游看到的我们"是哪个地址。

关键点：走和推理请求**同一套代理决策**（`proxies_for`），并且用**上游 URL**做决策。
不能用 echo 服务自己的 URL 去问 `proxies_for`——旁路列表（NO_PROXY 等）可能把 echo
判成直连，那样显示出来的 IP 就不是真正打上游时走的那条路。
"""

from __future__ import annotations

import threading
import time

from curl_cffi import requests as crequests

from core.logger import logger
from net.proxy import describe, proxies_for

# 多个 echo 服务依次兜底，单个不通不影响结论
ECHO_URLS: tuple[str, ...] = (
    "https://api.ipify.org",
    "https://api.ip.sb/ip",
    "https://ifconfig.me/ip",
)
TIMEOUT = 8
# 缓存时长：管理面刷新页面不该每次都打 echo 服务
CACHE_TTL = 30.0

_lock = threading.Lock()
_cached: dict | None = None
_cached_at = 0.0


def invalidate() -> None:
    """代理配置变更后调用：丢掉缓存，下次探测按新配置重新走。"""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0


def probe(reference_url: str, *, force: bool = False) -> dict:
    """探测出口 IP。reference_url 传上游地址，保证代理决策与真实推理一致。"""
    global _cached, _cached_at
    now = time.time()
    with _lock:
        if not force and _cached is not None and now - _cached_at < CACHE_TTL:
            return {**_cached, "cached": True}

    proxies = proxies_for(reference_url)
    result: dict = {
        "ok": False,
        "ip": "",
        "proxied": proxies is not None,
        "proxy_url": (proxies or {}).get("https", ""),
        "via": describe(),
        "error": "",
        "at_ms": int(now * 1000),
        "cached": False,
    }

    for url in ECHO_URLS:
        try:
            response = crequests.get(
                url, proxies=proxies, timeout=TIMEOUT, impersonate="chrome"
            )
            text = bytes(response.content).decode("utf-8", "replace").strip()
            if response.status_code == 200 and text:
                result["ok"] = True
                result["ip"] = text[:64]
                break
            result["error"] = f"{url} HTTP {response.status_code}"
        except Exception as exc:
            result["error"] = f"{url} {type(exc).__name__}: {exc}"[:200]

    if not result["ok"]:
        logger.warning(f"[出口] IP 探测失败: {result['error']}")
    with _lock:
        _cached = result
        _cached_at = now
    return result
