"""运行时配置：JSON 文件持久化（server/config.json），线程安全读写。

与 core.settings 的分工：settings 是写死的结构常量，本模块存会变的运行
配置（出站代理等）。settings 里的 PROXY_MODE/PROXY_URL 作为默认值来源；
文件里存的值覆盖默认值，管理面（/api/config）修改后立即落盘、重启保留。

写入用临时文件 + os.replace 原子替换，避免半写文件。
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from core.logger import logger
from core.settings import BASE_DIR, PROXY_MODE, PROXY_URL

CONFIG_PATH = BASE_DIR / "config.json"

PROXY_MODES = ("direct", "system", "manual")

DEFAULTS: dict[str, Any] = {
    "proxy": {"mode": PROXY_MODE, "url": PROXY_URL or ""},
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def get() -> dict[str, Any]:
    """当前配置（深拷贝，调用方可随意改）。首次调用读文件。"""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return deepcopy(_cache)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入并落盘，返回新配置。仅接受已知键，值做类型清洗。"""
    global _cache
    with _lock:
        current = _cache if _cache is not None else _load()
        merged = deepcopy(current)
        proxy_patch = patch.get("proxy")
        if isinstance(proxy_patch, dict):
            mode = str(proxy_patch.get("mode") or "")
            if mode in PROXY_MODES:
                merged["proxy"]["mode"] = mode
            url = proxy_patch.get("url")
            if isinstance(url, str):
                merged["proxy"]["url"] = url.strip()[:512]
        _save(merged)
        _cache = merged
        return deepcopy(merged)


def _load() -> dict[str, Any]:
    merged = deepcopy(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except Exception as exc:
        logger.warning(f"[配置] {CONFIG_PATH} 读取失败，用默认值: {exc}")
        return merged
    if not isinstance(raw, dict):
        return merged
    proxy_raw = raw.get("proxy")
    if isinstance(proxy_raw, dict):
        mode = str(proxy_raw.get("mode") or "")
        if mode in PROXY_MODES:
            merged["proxy"]["mode"] = mode
        url = proxy_raw.get("url")
        if isinstance(url, str):
            merged["proxy"]["url"] = url.strip()[:512]
    return merged


def _save(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, CONFIG_PATH)
