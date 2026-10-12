"""上游模型目录快照：`server/data/models.json`。

把各上游**原样返回**的模型条目落一份 JSON，供排查与对照用——网关内部用的是
归一化后的目录（`ModelInfo`），这里保存的是上游真实的数据结构：

    {"opencode": [ …api.json 的模型对象… ],
     "workbuddy": {"cn": [ …/v3/config 的 data.models 条目… ],
                   "glb": [ …同上（global 域）… ]},
     "codex": [ …/backend-api/codex/models 的 manifest 条目… ]}

写入策略：只替换本次刷新的那个 provider 的键，其余原样保留；某一路没拉到
（账号缺失/请求失败）时保留上一次的好数据，不写空覆盖。写入失败只告警，
绝不阻塞刷新（与遥测同姿态）。
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
        logger.warning(f"[目录快照] {PATH} 初始化失败: {type(exc).__name__}: {exc}")


def write(provider: str, payload: Any) -> None:
    """替换该 provider 的快照并落盘（原子写）。失败只告警。"""
    try:
        with _lock:
            data = _read_locked()
            data[provider] = payload
            _write_locked(data)
    except Exception as exc:
        logger.warning(f"[目录快照] {provider} 写入失败（不阻塞刷新）: {type(exc).__name__}: {exc}")


def _read_locked() -> dict[str, Any]:
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default()
    except Exception as exc:
        logger.warning(f"[目录快照] {PATH} 读取失败，用默认形状: {exc}")
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
