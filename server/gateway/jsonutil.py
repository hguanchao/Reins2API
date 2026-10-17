"""JSON 取值辅助（移植自 zen2api util/json.ts）。

桥接的是形状任意的上游 payload，这些容忍型辅助函数在缺失或类型不符的路径上
返回零值而不是抛错。与 Go 网关那套 nil-returning 辅助一一对应。
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any


def is_object(value: Any) -> bool:
    return isinstance(value, dict)


def is_nil(value: Any) -> bool:
    return value is None


def first_non_zero(*values: float) -> float:
    for value in values:
        if value != 0:
            return value
    return 0


def parse_json_or_string(value: str) -> Any:
    """空串 → {}，合法 JSON → 解析值，坏 JSON → 原字符串。"""
    if value == "":
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return value


def put(obj: dict[str, Any], key: str, value: Any) -> None:
    """只赋非 nil 值，缺省的可选字段在线上保持缺省而不是被序列化成 null。"""
    if not is_nil(value):
        obj[key] = value


def first_any(*values: Any) -> Any:
    for value in values:
        if not is_nil(value):
            return value
    return None


def clone_map(obj: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(obj)


def as_slice(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if is_nil(value):
        return []
    return [value]


def any_at(obj: Any, *path: str) -> Any:
    current = obj
    for key in path:
        if not is_object(current):
            return None
        current = current.get(key)
    return current


def slice_at(obj: Any, *path: str) -> list[Any]:
    value = any_at(obj, *path)
    return value if isinstance(value, list) else []


def map_at(obj: Any, *path: str) -> dict[str, Any]:
    # 返回 {} 而非 None，调用方可以像 Go 里 len(map) > 0 一样用 len(...) > 0
    value = any_at(obj, *path)
    return value if isinstance(value, dict) else {}


def string_at(obj: Any, *path: str) -> str:
    value = any_at(obj, *path)
    return value if isinstance(value, str) else ""


def bool_at(obj: Any, *path: str) -> bool:
    value = any_at(obj, *path)
    return value if isinstance(value, bool) else False


def int_at(obj: Any, *path: str) -> int:
    value = any_at(obj, *path)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if not math.isnan(value) and not math.isinf(value) else 0
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def first_string(*values: str) -> str:
    for value in values:
        if (value or "").strip() != "":
            return value.strip()
    return ""


def first_non_empty(*values: str | None) -> str:
    """首个非空字符串（None 与 "" 都跳过）——原样返回，不做 strip。

    与 first_string 的区别：后者按"去空白后非空"判定并返回去空白值，本函数只判空。
    协议层拼 finish_reason / error type 时用这个（空白串是合法值，不该被吞）。
    """
    for value in values:
        if value:
            return value
    return ""


# ── 出站序列化 ──────────────────────────────────────────────────


def well_formed(text: str) -> str:
    """孤立代理项 → U+FFFD（与 JS String.prototype.toWellFormed 同义）。

    客户端能用 `\\ud800` 这类 JSON 转义把孤立代理项送进来：json.loads 照单全收，
    但 `json.dumps(..., ensure_ascii=False).encode("utf-8")` 会直接抛
    UnicodeEncodeError——出站序列化的唯一崩溃点。官方 OpenCode 客户端在编译请求时
    统一做 toWellFormed，这里对齐同一语义：成对代理项合并成真实字符，孤立项换
    U+FFFD。正常字符串走 encode 快路径原样返回。
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sanitize_surrogates(value: Any) -> Any:
    """递归清理 dict / list / str 里的孤立代理项（键也清），返回新结构。"""
    if isinstance(value, str):
        return well_formed(value)
    if isinstance(value, list):
        return [sanitize_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {
            well_formed(key) if isinstance(key, str) else key: sanitize_surrogates(item)
            for key, item in value.items()
        }
    return value


def close_quietly(target: Any) -> None:
    """关掉上游连接/迭代器，失败静默。

    释放失败绝不能盖住调用栈上真正的错误（客户端断连、上游截断），也不该让
    "提前收工"这条路抛异常——所以这里吞掉一切。
    """
    close = getattr(target, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


def encode_json_body(value: Any) -> bytes:
    """出站请求体序列化：UTF-8、非 ASCII 原样。

    先按正常路径序列化（绝大多数请求一次成功，不做全量递归清理）；只有编码器真的
    撞上孤立代理项时才做一遍 toWellFormed 再重试。
    """
    try:
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(sanitize_surrogates(value), ensure_ascii=False).encode("utf-8")
