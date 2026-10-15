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
# 缓存时长。这里**只是"多久复查一次"**，不是"多久之后算没数据"——cached_ip 有过期值
# 也要照给（见下）。出口 IP 只在代理换节点时变，周期性重探纯属浪费 echo 配额，所以取
# 半小时：换个节点最迟半小时内会重新探到，而中间每次请求都还有旧的正确值可用。
CACHE_TTL = 1800.0

_lock = threading.Lock()
# 按 reference_url 分键：不同上游可能落在不同的代理决策上（NO_PROXY 例外），出口未必同号
_cache: dict[str, tuple[dict, float]] = {}
# 正在后台探的 URL（单飞，别让每条请求都甩一个线程去打 echo）
_inflight: set[str] = set()
# 上次**记过日志**的探测结论（按 URL）。探测本身要按期复查，但结论没变时不该再写一行
# ——否则每条间隔超过 TTL 的请求都会甩一次后台探测并留一行 INFO，日志被"出口 IP 探测
# 成功"刷满。结论变了（换出口 / 代理改变 / 从失败恢复）才写，那正是需要留痕的时刻。
_logged: dict[str, tuple] = {}
# 代理配置每变一次就 +1：探测开始和写回之间若被 invalidate（换代理），这次结果作废，
# 否则一个"改代理前发出、改代理后才回来"的探测会把旧出口 IP 塞进新缓存，错到 TTL 到期。
_generation = 0


def invalidate() -> None:
    """代理配置变更后调用：丢掉缓存，下次探测按新配置重新走。"""
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()
        # 结论指纹一并清掉：换了代理就该重新留痕，哪怕探到的 IP 恰好相同
        _logged.clear()


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

    _log_if_changed(reference_url, result)
    with _lock:
        # 探测期间代理配置变过就作废：这个结果走的是旧配置，写进新缓存会错到 TTL 到期
        if generation == _generation:
            _cache[reference_url] = (result, now)
    return result


def _log_if_changed(reference_url: str, result: dict) -> None:
    """结论没变就不写日志：探测按期复查（半小时一次），留痕只在**变化**时。

    指纹覆盖出口 IP、代理形态（proxied / via）与成败：换出口、改代理、由失败恢复都会
    落一行；只有"还是那个出口"被静音。失败每次都写——故障持续期间需要知道它一直在失败。
    """
    fingerprint = (
        result.get("ok"),
        result.get("ip") or "",
        result.get("proxied"),
        result.get("via") or "",
    )
    with _lock:
        if _logged.get(reference_url) == fingerprint and result.get("ok"):
            return
        _logged[reference_url] = fingerprint
    if result["ok"]:
        logger.info(
            f"[出口] IP 探测成功 ip={result['ip']} proxied={result['proxied']} via={result['via']}"
        )
    else:
        logger.warning(f"[出口] IP 探测失败: {result['error']}")


def cached_ip(reference_url: str) -> str:
    """请求落库时取出口 IP：只读缓存，不联网、不阻塞转发。

    返回**上一次探到的值，不管它过去了多久**：出口 IP 只随代理配置变，而代理一
    变就 invalidate 清缓存，所以陈旧值仍是对的。反过来"过 TTL 就返回空串、等后台
    重探"会让每条间隔超过 TTL 的请求都记成空——请求列表里出口 IP 常年显示「—」
    就是这个原因。过期的缓存顺手甩个后台探测刷新（单飞、结论不变不记日志），
    但不影响本次返回值。
    """
    now = time.time()
    with _lock:
        hit = _cache.get(reference_url)
    if hit is not None:
        if now - hit[1] >= CACHE_TTL:
            warm(reference_url)
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
