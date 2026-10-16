"""运行时配置：JSON 文件持久化（server/config.json），线程安全读写。

与 core.settings 的分工：settings 是写死的结构常量，本模块存会变的运行
配置（出站代理、定时任务开关/时点）。settings 里的 PROXY_MODE/PROXY_URL 与
WB_SCHEDULE_DEFAULTS 作为默认值来源；文件里存的值覆盖默认值，管理面
（/api/config、/api/workbuddy/schedule）修改后立即落盘、重启保留。

schedule 段的形状（四类任务各一个独立开关 + 一条 5 字段 cron，时区北京时间）：

    "schedule": {
      "checkin":   {"enabled": true, "cron": "7 9,21 * * *"},
      "activity":  {"enabled": true, "cron": "7 10 * * *"},
      "travel":    {"enabled": true, "cron": "7 9,21 * * *"},
      "keepalive": {"enabled": true, "cron": "7 22 * * *"}
    }

老配置里的 `hours: [9, 21]` 仍可读：按 WB_SCHEDULE_MINUTE 迁成 cron 后写回，
迁移只发生一次（落盘即换形态）。

写入用临时文件 + os.replace 原子替换，避免半写文件。
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from core.cron import CronError
from core.cron import parse as parse_cron
from core.logger import logger
from core.settings import BASE_DIR, PROXY_MODE, PROXY_URL, WB_SCHEDULE_DEFAULTS, WB_SCHEDULE_MINUTE

CONFIG_PATH = BASE_DIR / "config.json"

PROXY_MODES = ("direct", "system", "manual")

# 定时任务名（schedule 段的固定键集；顺序即管理台展示顺序）
SCHEDULE_TASKS = ("checkin", "activity", "travel", "keepalive")


def _default_schedule() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "enabled": bool(spec.get("enabled", True)),
            "cron": str(spec.get("cron") or ""),
        }
        for name, spec in WB_SCHEDULE_DEFAULTS.items()
        if name in SCHEDULE_TASKS
    }


def hours_to_cron(hours: list[int]) -> str:
    """旧 `hours: [9, 21]` → cron（分钟取 WB_SCHEDULE_MINUTE，即迁移前的语义）。"""
    valid = sorted({int(h) for h in hours if isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23})
    if not valid:
        return ""
    return f"{max(0, min(59, WB_SCHEDULE_MINUTE))} {','.join(str(h) for h in valid)} * * *"


DEFAULTS: dict[str, Any] = {
    "proxy": {"mode": PROXY_MODE, "url": PROXY_URL or ""},
    "schedule": _default_schedule(),
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


def schedule() -> dict[str, dict[str, Any]]:
    """schedule 段快照（排程器每轮读它）。"""
    return get()["schedule"]


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
        schedule_patch = patch.get("schedule")
        if isinstance(schedule_patch, dict):
            merged["schedule"] = _clean_schedule(schedule_patch, merged["schedule"])
        _save(merged)
        _cache = merged
        return deepcopy(merged)


def update_schedule(schedule_patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """只改 schedule 段（管理台开关/时点），返回清洗后的新值。"""
    return update({"schedule": schedule_patch})["schedule"]


def _clean_schedule(raw: dict[str, Any], base: dict[str, dict[str, Any]]
                    ) -> dict[str, dict[str, Any]]:
    """清洗 schedule 段：只认 SCHEDULE_TASKS 里的键，逐项校验。

    - enabled：非 bool 忽略（保留原值）
    - cron：必须能被解析器接受；**非法/缺失一律保留原值**（要停任务请关开关，
      而不是留一个开着却永不触发的表达式——那种状态只会让人以为任务坏了）
    - hours：老字段，仅作迁移输入（按 WB_SCHEDULE_MINUTE 转成 cron）
    """
    out = deepcopy(base)
    for name in SCHEDULE_TASKS:
        item = raw.get(name)
        if not isinstance(item, dict):
            continue
        entry = out.setdefault(name, {"enabled": True, "cron": ""})
        enabled = item.get("enabled")
        if isinstance(enabled, bool):
            entry["enabled"] = enabled
        cron_text = item.get("cron")
        if isinstance(cron_text, str) and cron_text.strip():
            try:
                entry["cron"] = parse_cron(cron_text).expr
            except CronError as exc:
                logger.warning(f"[配置] {name} 的 cron 非法，保留原值: {exc}")
        elif isinstance(item.get("hours"), list):
            migrated = hours_to_cron(item["hours"])
            if migrated:
                entry["cron"] = migrated
        entry.pop("hours", None)  # 旧字段不留在规范形态里
    return out


def _load() -> dict[str, Any]:
    merged = deepcopy(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except Exception as exc:
        logger.warning(f"[配置] 配置文件读取失败, 用默认值 path={CONFIG_PATH}: {exc}")
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
    schedule_raw = raw.get("schedule")
    if isinstance(schedule_raw, dict):
        merged["schedule"] = _clean_schedule(schedule_raw, merged["schedule"])
    return merged


def _save(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, CONFIG_PATH)
