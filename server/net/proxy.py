"""出站代理解析。

direct / system（环境变量 → Windows WinINET 注册表）/ manual 三档，
统一旁路列表匹配。三档的取值来自运行时配置（server/config.json，管理面可改），
改配置调 invalidate() 即可立即生效，不用重启。

system 档 30s TTL：每次请求重读会开注册表查询，从不重读则先启动代理后开网关
必须重启。manual 档按 (mode, url) 变更检测重读，避免每请求重复解析。

注册表用 Python 标准库 winreg 直读，不必绕道外部命令（exec reg.exe 那条路
是给没有注册表 API 的运行时用的）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from urllib.parse import urlparse

from core import config as app_config
from core.logger import logger

# "跟随系统"的可信时长
SYSTEM_TTL = 30.0

_lock = threading.Lock()
_source: dict | None = None      # {"url": str, "bypass": list[str]}
_source_read_at = 0.0
_loaded_key: tuple[str, str] | None = None   # 上次刷新用的 (mode, url)
_description = "直连（不走代理）"
_logged_nosys = False


def proxies_for(url: str) -> dict[str, str] | None:
    """curl_cffi 的 proxies 形状；直连或命中旁路返回 None。"""
    mode, manual_url = _settings()
    if mode == "direct":
        return None
    source = _current_source(mode, manual_url)
    if source is None:
        return None
    if _is_bypassed(url, source["bypass"]):
        return None
    return {"http": source["url"], "https": source["url"]}


def describe() -> str:
    """运维面可读的代理实况（尚未解析过时先解析一次）。"""
    with _lock:
        if _loaded_key is None:
            mode, manual_url = _settings()
            _refresh_locked(mode=mode, manual_url=manual_url)
        return _description


def invalidate() -> None:
    """配置变更后调用：立即按新配置重解析，describe()/proxies_for 马上是新实况。"""
    with _lock:
        mode, manual_url = _settings()
        _refresh_locked(mode=mode, manual_url=manual_url)


def _settings() -> tuple[str, str]:
    proxy_cfg = app_config.get().get("proxy") or {}
    mode = str(proxy_cfg.get("mode") or "direct")
    if mode not in app_config.PROXY_MODES:
        mode = "direct"
    return mode, str(proxy_cfg.get("url") or "")


def _current_source(mode: str, manual_url: str) -> dict | None:
    with _lock:
        key = (mode, manual_url if mode == "manual" else "")
        stale = _loaded_key != key or (
            mode == "system" and time.time() - _source_read_at > SYSTEM_TTL
        )
        if stale:
            _refresh_locked(mode=mode, manual_url=manual_url)
        return _source


def _refresh_locked(*, mode: str, manual_url: str) -> None:
    global _source, _source_read_at, _description, _loaded_key, _logged_nosys
    _source_read_at = time.time()
    _loaded_key = (mode, manual_url if mode == "manual" else "")
    if mode == "direct":
        _source = None
        _description = "直连（不走代理）"
        return
    if mode == "manual":
        url = _normalize_proxy_url(manual_url)
        if not url:
            logger.warning("[代理] mode=manual 未配置 URL, 按直连处理")
            _source = None
            _description = "手动 · 未配置 URL，按直连处理"
            return
        _source = {"url": url, "bypass": _env_bypass()}
        _description = f"手动 · {url}"
        logger.info(f"[代理] 出站代理生效 url={url}")
        return

    from_env = _env_proxy()
    if from_env is not None:
        _source = from_env
        _description = f"跟随系统 · 环境变量 · {from_env['url']}"
        return
    from_windows = _read_windows_system_proxy()
    if from_windows is not None:
        _source = from_windows
        _description = f"跟随系统 · Windows 设置 · {from_windows['url']}"
        return
    _source = None
    _description = "跟随系统 · 系统未配置代理，按直连处理"
    if not _logged_nosys:
        _logged_nosys = True
        logger.warning("[代理] mode=system 未发现系统代理, 按直连处理")


def _env_proxy() -> dict | None:
    url = _first_env([
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    ])
    if url == "":
        return None
    return {"url": _normalize_proxy_url(url), "bypass": _env_bypass()}


def _env_bypass() -> list[str]:
    return _split_list(_first_env(["NO_PROXY", "no_proxy"]))


def _read_windows_system_proxy() -> dict | None:
    """读桌面代理工具实际写入的 WinINET 设置；关闭/读不到 → None（回落直连）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                return None
            if enabled != 1:
                return None
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except OSError:
                return None
            try:
                override, _ = winreg.QueryValueEx(key, "ProxyOverride")
            except OSError:
                override = ""
    except OSError:
        return None
    if not isinstance(server, str) or server.strip() == "":
        return None
    url = _pick_proxy_from_server_list(server)
    if url == "":
        return None
    # "<local>"（纯主机名旁路）与 TS 一致：从旁路表剔除，不做本地特判。
    bypass = [e for e in _split_list(override if isinstance(override, str) else "") if e != "<local>"]
    return {"url": url, "bypass": bypass}


def _pick_proxy_from_server_list(value: str) -> str:
    """Windows 两种形态："host:port" 裸值，或 "http=...;https=...;socks=..." 列表。"""
    by_scheme: dict[str, str] = {}
    bare = ""
    for part in [p.strip() for p in value.split(";") if p.strip()]:
        index = part.find("=")
        if index < 0:
            if bare == "":
                bare = part
            continue
        by_scheme[part[:index].strip().lower()] = part[index + 1:].strip()
    chosen = by_scheme.get("https") or by_scheme.get("http") or by_scheme.get("socks") or bare
    return _normalize_proxy_url(chosen) if chosen else ""


def _normalize_proxy_url(value: str) -> str:
    """接受 "127.0.0.1:7890" 与完整 URL（Windows 与环境变量的常见写法）。"""
    trimmed = value.strip()
    if trimmed == "":
        return ""
    if "://" in trimmed[:16] and trimmed.split("://", 1)[0].isascii():
        scheme = trimmed.split("://", 1)[0]
        if scheme.isalpha() and all(c.isalnum() or c in "+-." for c in scheme):
            return trimmed
    return f"http://{trimmed}"


def _first_env(names: list[str]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return ""


def _split_list(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _is_bypassed(target: str, bypass: list[str]) -> bool:
    """"*" 全放行；"host" 或 ".host" 匹配域及其子域；带端口必须两端都匹配。"""
    if not bypass:
        return False
    try:
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()
        port = str(parsed.port or "")
    except ValueError:
        return False
    if not host:
        return False
    for entry in bypass:
        if entry == "*":
            return True
        parts = entry.lower().split(":", 1)
        raw_host = parts[0]
        raw_port = parts[1] if len(parts) > 1 else ""
        suffix = raw_host.removeprefix(".")
        if suffix == "":
            continue
        if raw_port != "" and raw_port != port:
            continue
        if host == suffix or host.endswith("." + suffix):
            return True
    return False
