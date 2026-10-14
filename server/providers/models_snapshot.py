"""上游模型目录快照：`server/data/models.json`。

三个 provider 共用同一套**统一字段名**，只保留排查/对照要看的核心信息——上游
原样条目又大又杂（codex manifest 单条可达 40+ KB，光 base_instructions 就 17 KB），
整条落盘没意义：

    {"opencode": [ {"id": …, "name": …, …}, … ],
     "workbuddy": {"cn": [ … ], "glb": [ … ]},
     "codex": [ … ],
     "meta": {"<provider>": {"updated_at_ms": …, …}}}

条目由各 provider 用 core_entry() 构造（字段含义见其 docstring）；上游没声明的
字段省略——**不落 False/0 冒充「确定没有」**。

这份快照同时是**冷启动的唯一数据源**（关掉自动刷新后，重启靠它重建内存目录，
而内存目录是路由依据），所以条目里带上了重建所需的路由/展示字段（protocol、
free、extras）。`meta.<provider>` 是与条目形状无关的旁挂信息：`updated_at_ms`
是**目录内容最后变更时间**（内容没变就不动它），opencode 另存凭证可见清单。

写入策略：只替换本次刷新的那个 provider 的键，其余原样保留；内容与现快照一致
时不写盘（不抖 mtime、不产生无意义写入），确实要变时先把现文件轮转成
`models.json.bak` 再原子替换。写入失败只告警，绝不阻塞刷新（与遥测同姿态）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

PATH = DATA_DIR / "models.json"
_lock = threading.Lock()


def backup_path() -> Path:
    """轮转目标（models.json.bak）。

    从 PATH 派生而不是另立常量：测试会把 PATH 重绑到临时目录，两个常量会漂移，
    于是合成内容被轮转写进真实 data/（踩过这个坑）。绑定关系由这里保证。
    """
    return PATH.with_name(PATH.name + ".bak")

# 统一条目字段词表：opencode / workbuddy / codex 三家同名同义。
CORE_FIELDS: tuple[str, ...] = (
    "id",                  # 上游模型 id（slug / 裸名）
    "name",                # 显示名
    "description",         # 描述（workbuddy 优先中文）
    "context_window",      # 上下文窗口（输入 tokens 上限）
    "max_output_tokens",   # 最大输出 tokens
    "input_modalities",    # 输入模态，如 ["text", "image", "pdf"]（上游声明原文）
    "output_modalities",   # 输出模态，如 ["text"]
    "supports_images",     # 图片输入（workbuddy 上游只有这个布尔位，没有模态列表）
    "supports_reasoning",  # 思考/推理
    "supports_tool_call",  # 工具调用
    "reasoning_efforts",   # 支持的思考档位，如 ["low", "medium", "high"]
    "default_effort",      # 默认思考档位
    "protocol",            # 上游协议（chat / responses / anthropic）——重建路由要用
    "free",                # 免费标记（成本为 0 的判定结果，False 是确定值不是"未知"）
    "extras",              # provider 自身的展示字段（各家自解释，统一层不校验内容）
)


def core_entry(**fields: Any) -> dict[str, Any]:
    """构造统一快照条目：值为 None 的字段省略，词表外的字段名直接报错（防手滑）。

    `extras` 是留给各 provider 的逃生口：放「重建内存目录要用、但只有这家有」的
    展示字段（如 workbuddy 的倍率/活动、opencode 的 max_input），内容不做校验。
    """
    unknown = set(fields) - set(CORE_FIELDS)
    if unknown:
        raise ValueError(f"models.json 快照字段不在统一词表内: {sorted(unknown)}")
    return {key: value for key, value in fields.items() if value is not None}


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 排序条目：上游字典序不稳定，未排序会让「内容未变」被 write 判成变了，
    每次刷新都白轮转 .bak 并重写文件（write 的 no-op 判定是整体相等）。"""
    return sorted(entries, key=lambda entry: str(entry.get("id") or ""))


def _default() -> dict[str, Any]:
    return {"opencode": [], "workbuddy": {"cn": [], "glb": []}, "codex": []}


def read() -> dict[str, Any]:
    """当前快照（缺失的顶层键用默认形状补齐）。"""
    with _lock:
        return _read_locked()


def ensure() -> None:
    """文件不存在时按默认形状创建；已存在则补齐缺失的顶层键。"""
    try:
        with _lock:
            _write_locked(_read_locked())
    except Exception as exc:  # 快照是辅助产物，建不出来也不该拦住启动
        logger.warning(f"[快照] 初始化失败 path={PATH}: {type(exc).__name__}: {exc}")


def write(provider: str, payload: Any, meta: dict[str, Any] | None = None) -> bool:
    """替换该 provider 的快照并落盘（原子写，内容要变时先轮转 .bak）。

    返回本次是否真的写了盘：条目与 `meta` 都与现快照一致时跳过——刷新一次就写
    一次会让 mtime 每次都抖，也白写盘。`meta` 是增量合并；只有条目内容变了才
    更新 `updated_at_ms`（它记的是**目录内容**的最后变更时间，不是每次拉取时间）。
    失败只告警，返回 False（调用方不应因快照失败报错）。
    """
    try:
        with _lock:
            data = _read_locked()
            old_meta = _meta_of(data, provider)
            new_meta = {**old_meta, **(meta or {})}
            changed = data.get(provider) != payload
            if changed:
                new_meta["updated_at_ms"] = int(time.time() * 1000)
            if not changed and new_meta == old_meta:
                return False
            if changed:
                _rotate_locked()
            data[provider] = payload
            stored = data.get("meta")
            if not isinstance(stored, dict):
                stored = {}
                data["meta"] = stored
            stored[provider] = new_meta
            _write_locked(data)
            return True
    except Exception as exc:
        logger.warning(f"[快照] provider={provider} 写入失败(不阻塞刷新): {type(exc).__name__}: {exc}")
        return False


def read_meta(provider: str) -> dict[str, Any]:
    """provider 的旁挂信息（内容变更时间、凭证可见清单等）；没有则空 dict。"""
    with _lock:
        return _meta_of(_read_locked(), provider)


def _meta_of(data: dict[str, Any], provider: str) -> dict[str, Any]:
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return {}
    entry = meta.get(provider)
    return dict(entry) if isinstance(entry, dict) else {}


def _read_locked() -> dict[str, Any]:
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default()
    except Exception as exc:
        logger.warning(f"[快照] 读取失败, 用默认形状 path={PATH}: {exc}")
        return _default()
    if not isinstance(raw, dict):
        return _default()
    merged = _default()
    for key, value in raw.items():
        merged[key] = value
    return merged


def _write_locked(data: dict[str, Any]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, PATH)


def _rotate_locked() -> None:
    """写入前把现文件轮转成 .bak：上游给出坏/空目录时可以人工回滚。"""
    backup = backup_path()
    try:
        if PATH.exists():
            shutil.copyfile(PATH, backup)
    except Exception as exc:
        logger.warning(f"[快照] 备份失败(继续写入): {type(exc).__name__}: {exc}")
