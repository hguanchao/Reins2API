"""停止原因映射（移植自 zen2api protocol/bridge/stop.ts）。

桥上只携带一套规范词表（"stop" / "tool_calls" / "length" / "content_filter" /
"error" / "stop_sequence"），每个协议把它投影到自己的 finish_reason /
stop_reason / status 字段。
"""

from __future__ import annotations

# 表示"这一轮以错误收尾"的规范停止原因（各协议投影时都要识别）。
ERROR_STOPS = frozenset({"error", "network_error", "server_error"})


def is_error_stop(stop: str) -> bool:
    return stop in ERROR_STOPS


def is_tool_stop(stop: str) -> bool:
    """规范停止原因是否承诺下游还有工具调用。"""
    return stop in ("tool_calls", "tool_use", "function_call")


def chat_stop(stop: str) -> str:
    if stop in ("tool_use", "tool_calls"):
        return "tool_calls"
    if stop in ("max_tokens", "length"):
        return "length"
    if stop in ("content_filter", "error"):
        return stop
    return "stop"


def anthropic_stop(stop: str) -> str:
    if stop in ("tool_use", "tool_calls"):
        return "tool_use"
    if stop in ("max_tokens", "length"):
        return "max_tokens"
    if stop in ("stop_sequence", "pause_turn", "refusal"):
        return stop
    return "end_turn"


def canonical_chat_stop(stop: str) -> str:
    if stop in ("tool_calls", "function_call"):
        return "tool_calls"
    if stop == "length":
        return "length"
    if stop == "content_filter":
        return "content_filter"
    if is_error_stop(stop):
        return "error"
    return "stop"


def canonical_anthropic_stop(stop: str) -> str:
    if stop == "tool_use":
        return "tool_calls"
    if stop == "max_tokens":
        return "length"
    if stop in ("stop_sequence", "pause_turn", "refusal"):
        return stop
    if is_error_stop(stop):
        return "error"
    return "stop"


def canonical_responses_incomplete(reason: str) -> str:
    return "content_filter" if reason == "content_filter" else "length"


def responses_incomplete_reason(stop: str) -> str:
    return "content_filter" if stop == "content_filter" else "max_output_tokens"
