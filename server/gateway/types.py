"""Bridge 中间结构。

每个外部协议先解码成这些协议无关的形状，再编码进上游协议——一条转换路径服务
全部九个方向。JSON payload 在 Python 里始终是松散 dict/list（绝不建严格模型），
未知字段才能原样保留；这些 dataclass 只承载桥内部的中间表示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_CHAT = "chat"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_ANTHROPIC = "anthropic"


def protocol_path(protocol: str) -> str:
    if protocol == PROTOCOL_RESPONSES:
        return "/v1/responses"
    if protocol == PROTOCOL_ANTHROPIC:
        return "/v1/messages"
    return "/v1/chat/completions"


@dataclass
class BridgeBlock:
    kind: str = ""
    text: str = ""
    signature: str = ""
    encrypted: str = ""
    url: str = ""
    media_type: str = ""
    data: str = ""
    file_id: str = ""
    filename: str = ""
    id: str = ""
    name: str = ""
    args: Any = None
    args_json: str = ""
    call_id: str = ""
    result: Any = None
    is_error: bool = False


def new_block(kind: str, patch: dict[str, Any] | None = None) -> BridgeBlock:
    block = BridgeBlock(kind=kind)
    if patch:
        for key, value in patch.items():
            setattr(block, key, value)
    return block


@dataclass
class BridgeMessage:
    role: str = ""
    blocks: list[BridgeBlock] = field(default_factory=list)


@dataclass
class BridgeTool:
    name: str = ""
    description: str = ""
    schema: Any = None
    strict: bool = False


@dataclass
class BridgeToolChoice:
    mode: str = ""
    name: str = ""


@dataclass
class BridgeRequest:
    model: str = ""
    system: list[BridgeBlock] = field(default_factory=list)
    developer: list[BridgeBlock] = field(default_factory=list)
    messages: list[BridgeMessage] = field(default_factory=list)
    tools: list[BridgeTool] = field(default_factory=list)
    tool_choice: BridgeToolChoice = field(default_factory=BridgeToolChoice)
    stream: bool = False
    temperature: Any = None
    top_p: Any = None
    max_tokens: Any = None
    stop: Any = None
    reasoning: Any = None
    metadata: Any = None
    # 采样与响应形状旋钮以不透明值过桥：各协议接受的形状不同，没有原生等价物
    # 的值直接省略而不是报错。
    response_format: Any = None
    parallel_tool_calls: Any = None
    frequency_penalty: Any = None
    presence_penalty: Any = None
    seed: Any = None
    # prompt_cache_key 是最重要的一个：原生 OpenCode 把会话切片 ID 放这里骑
    # prompt-cache 亲和，丢掉它会让每个代理轮次对全部历史重新计算注意力。
    prompt_cache_key: Any = None
    safety_identifier: Any = None
    service_tier: Any = None
    store: Any = None


@dataclass
class Usage:
    # 含缓存读写的完整 prompt 输入，对齐 OpenAI 的 prompt_tokens 语义。
    # 每个输出编码器把缓存字段拆一次。
    input: int = 0
    output: int = 0
    total: int = 0
    cached: int = 0
    cache_creation: int = 0
    reasoning: int = 0
    # 上游计费量（积分；workbuddy 把本次真实扣费放在 usage.credit）。None = 未观测、
    # 0 = 实测免费（限免期/夜间免费）——两者必须分开，把"没给"当"免费"会让消耗统计失真。
    credit: float | None = None


def new_usage() -> Usage:
    return Usage()


@dataclass
class BridgeResponse:
    id: str = ""
    model: str = ""
    text: str = ""
    reasoning: list[BridgeBlock] = field(default_factory=list)
    tools: list[BridgeBlock] = field(default_factory=list)
    stop: str = ""
    error: str = ""
    usage: Usage = field(default_factory=Usage)
    created: int = 0


@dataclass
class BridgeStreamEvent:
    kind: str = ""
    response_id: str | None = None
    model: str | None = None
    text: str | None = None
    signature: str | None = None
    tool_key: str | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    stop: str | None = None
    error: str | None = None
    error_type: str | None = None
    encrypted: str | None = None
    usage: Usage | None = None


def merge_bridge_usage(destination: Usage, source: Usage) -> None:
    if source.input != 0:
        destination.input = source.input
    if source.output != 0:
        destination.output = source.output
    if source.total != 0:
        destination.total = max(destination.total, source.total)
    if source.cached != 0:
        destination.cached = source.cached
    if source.cache_creation != 0:
        destination.cache_creation = source.cache_creation
    if source.reasoning != 0:
        destination.reasoning = source.reasoning
    # credit 用「最新非 None 覆盖」而不是 !=0 那套：0 是有效观测（实测免费），
    # 与"上游没给这个字段"是两件事，只有 None 才该被跳过。
    if source.credit is not None:
        destination.credit = source.credit
    destination.total = max(destination.total, destination.input + destination.output)
