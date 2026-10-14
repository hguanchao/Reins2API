"""出口 IP 探测：回报"上游看到的我们"是哪个地址。

关键点：走和推理请求**同一套代理决策**（`proxies_for`），并且用**上游 URL**做决策。
不能用 echo 服务自己的 URL 去问 `proxies_for`——旁路列表（NO_PROXY 等）可能把 echo
判成直连，那样显示出来的 IP 就不是真正打上游时走的那条路。

两个消费方：管理面（`probe`，可阻塞、可强制刷新）与请求落库（`cached_ip`，只读缓存、
绝不联网——转发路径上不能为了一个展示字段去等 echo 服务）。
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
# 按 reference_url 分键：不同上游可能落在不同的代理决策上（NO_PROXY 例外），出口未必同号
_cache: dict[str, tuple[dict, float]] = {}
# 正在后台探的 URL（单飞，别让每条请求都甩一个线程去打 echo）
_inflight: set[str] = set()
# 代理配置每变一次就 +1：探测开始和写回之间若被 invalidate（换代理），这次结果作废，
# 否则一个"改代理前发出、改代理后才回来"的探测会把旧出口 IP 塞进新缓存，错到 TTL 到期。
_generation = 0


def invalidate() -> None:
    """代理配置变更后调用：丢掉缓存，下次探测按新配置重新走。"""
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()


def probe(reference_url: str, *, force: bool = False) -> dict:
    """探测出口 IP。reference_url 传上游地址，保证代理决策与真实推理一致。"""
    now = time.time()
    with _lock:
        generation = _generation
        hit = _cache.get(reference_url)
    if not force and hit is not None and now - hit[1] < CACHE_TTL:
        return {**hit[0], "cached": True}

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
        # 探测期间代理配置变过就作废：这个结果走的是旧配置，写进新缓存会错到 TTL 到期
        if generation == _generation:
            _cache[reference_url] = (result, now)
    return result


def cached_ip(reference_url: str) -> str:
    """请求落库时取出口 IP：只读缓存，不联网、不阻塞转发。

    缓存冷或过期时甩一个后台探测并返回空串——所以刚起来（或刚改完代理）的头几条
    请求出口 IP 是空的，探到之后的行才有值。
    """
    now = time.time()
    with _lock:
        hit = _cache.get(reference_url)
    if hit is not None and now - hit[1] < CACHE_TTL:
        return str(hit[0].get("ip") or "")
    warm(reference_url)
    return ""


def warm(reference_url: str) -> None:
    """后台把缓存焐热。单飞：同一 URL 同时只有一个探测在跑。"""
    with _lock:
        if reference_url in _inflight:
            return
        _inflight.add(reference_url)

    def run() -> None:
        try:
            probe(reference_url, force=True)
        except Exception as exc:  # 后台线程的异常没人接，只能自己咽
            logger.warning(f"[出口] 后台探测异常: {type(exc).__name__}: {exc}")
        finally:
            with _lock:
                _inflight.discard(reference_url)

    threading.Thread(target=run, name="egress-warm", daemon=True).start()
