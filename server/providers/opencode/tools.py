"""官方 OpenCode 工具真定义（从官方 CLI 抓取，@opencode/cli 2.x）。

免费档从 2026-10-14 起不再只核对工具名：它把**每个它认识的名字**的描述与参数
schema 跟官方定义逐字段比对，只要有一处不同就 403 FreeTierError。实测确认过的
四种失败：最小桩定义、真描述配桩 schema、沿用 2.x 前的旧名 bash、干脆不带 tools
（缺 shell/read 一类核心工具也算非 agent 会话形态）。所以这里落的是原样抓取的
定义，注入缺失核心工具和替换客户端同名工具都只用它。

opencode_tools.json 保持与官方抓取结果字节一致：上游是逐字段比对，不欢迎任何
"顺手美化"（重排、补默认值、改类型都会让它不再相等）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT

# 上游要求 agent 会话形态必备的工具名。shell 是 OpenCode 2.x 的名字；2.x 前的
# bash 不再注入——上游认识 bash 且会校验其定义，而网关手上没有 bash 的官方定义。
CORE_TOOLS = ("shell", "edit", "glob", "grep", "read")

_TOOLS_FILE = Path(__file__).with_name("opencode_tools.json")


@lru_cache(maxsize=1)
def known_tools() -> dict[str, dict[str, Any]]:
    """工具名 -> {description, parameters}；文件缺失或损坏时退化为空表。"""
    try:
        payload = json.loads(_TOOLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(name): value for name, value in payload.items() if isinstance(value, dict)}


def known(name: str) -> bool:
    """上游是否认识这个名字（认识的就必须带官方定义）。"""
    return name in known_tools()


def render_tool(protocol: str, name: str) -> dict[str, Any]:
    """按上游协议渲染一个工具；名字没有官方定义时退回最小形状。"""
    definition = known_tools().get(name) or {}
    description = str(definition.get("description") or f"Agent tool {name}")
    parameters = definition.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    if protocol == PROTOCOL_CHAT:
        return {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }
    if protocol == PROTOCOL_ANTHROPIC:
        return {"name": name, "description": description, "input_schema": parameters}
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


def render_toolset(protocol: str, names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [render_tool(protocol, name) for name in names]
