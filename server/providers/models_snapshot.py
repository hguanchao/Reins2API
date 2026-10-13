"""上游模型目录快照：`server/data/models.json`。

三个 provider 共用同一套**统一字段名**，只保留排查/对照要看的核心信息——上游
原样条目又大又杂（codex manifest 单条可达 40+ KB，光 base_instructions 就 17 KB），
整条落盘没意义：

    {"opencode": [ {"id": …, "name": …, …}, … ],
     "workbuddy": {"cn": [ … ], "glb": [ … ]},
     "codex": [ … ]}

条目由各 provider 用 core_entry() 构造（字段含义见其 docstring）；上游没声明的
字段省略——**不落 False/0 冒充「确定没有」**。写入策略：只替换本次刷新的那个
provider 的键，其余原样保留；某一路没拉到（账号缺失/请求失败）时保留上一次的
好数据，不写空覆盖。写入失败只告警，绝不阻塞刷新（与遥测同姿态）。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

PATH = DATA_DIR / "models.json"
_lock = threading.Lock()

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
)


def core_entry(**fields: Any) -> dict[str, Any]:
    """构造统一快照条目：值为 None 的字段省略，词表外的字段名直接报错（防手滑）。"""
    unknown = set(fields) - set(CORE_FIELDS)
    if unknown:
        raise ValueError(f"models.json 快照字段不在统一词表内: {sorted(unknown)}")
    return {key: value for key, value in fields.items() if value is not None}


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


def write(provider: str, payload: Any) -> None:
    """替换该 provider 的快照并落盘（原子写）。失败只告警。"""
    try:
        with _lock:
            data = _read_locked()
            data[provider] = payload
            _write_locked(data)
    except Exception as exc:
        logger.warning(f"[快照] provider={provider} 写入失败(不阻塞刷新): {type(exc).__name__}: {exc}")


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
