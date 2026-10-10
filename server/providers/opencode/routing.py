"""模型索引、SDK→协议映射与路由决策（移植自 zen2api routing/ 四文件）。

- model-index：有哪些模型、各说什么协议、凭证实际能到哪些——纯数据，不发请求。
- protocol-map：SDK → 上游协议的协议知识（上游加新 SDK 时改这里）。
- router：路由决策。本项目无凭证轮换，恒走匿名档；可用性以凭证可见清单为准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gateway.base import ModelNotFound, Route
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES

CAPABILITIES_URL = "https://models.opencode.ai/api.json"
_PROVIDER_NAME = "opencode"


# ── model-index：纯数据 ─────────────────────────────────────────


def empty_metadata() -> dict[str, Any]:
    return {
        "contextWindow": 0,
        "maxInput": 0,
        "maxOutput": 0,
        "reasoning": False,
        "reasoningOptions": [],
        "toolCall": False,
        "structuredOutput": False,
        "inputModalities": [],
        "outputModalities": [],
    }


@dataclass
class ModelRecord:
    protocol: str
    metadata: dict[str, Any] = field(default_factory=empty_metadata)
    free: bool = False


class ModelIndex:
    def __init__(self) -> None:
        self._records: dict[str, ModelRecord] = {}
        # 凭证可见清单（{upstream}/v1/models）。首次拉取成功前是"未知"而非"空"。
        self._available: set[str] = set()
        self._listed = False

    def replace(self, records: dict[str, ModelRecord]) -> None:
        self._records = records

    def set_available(self, ids: list[str]) -> None:
        self._available = set(ids)
        self._listed = True

    def record(self, model: str) -> ModelRecord | None:
        return self._records.get(model)

    def protocol(self, model: str) -> str | None:
        record = self._records.get(model)
        return record.protocol if record else None

    def free(self, model: str) -> bool | None:
        record = self._records.get(model)
        return record.free if record else None

    def knows_protocol(self, model: str) -> bool:
        return model in self._records

    def servable(self, model: str) -> bool:
        """上游凭证能否触达：协议已知，且（清单拉到后）在清单上。"""
        if model not in self._records:
            return False
        if not self._listed:
            return True
        return model in self._available

    def servable_models(self) -> list[str]:
        """凭证清单 ∩ 能力目录协议。首次清单拉取前用目录自身的模型兜底，
        发现失败不至于让网关下线。"""
        if not self._listed:
            return sorted(self._records)
        return sorted(m for m in self._available if m in self._records)

    def counts(self) -> dict[str, int]:
        return {"protocols": len(self._records), "available": len(self._available)}


# ── protocol-map：SDK → 上游协议 ────────────────────────────────


def protocol_for_sdk(npm: str) -> str | None:
    """SDK 即上游的协议声明：
      @ai-sdk/anthropic          -> /v1/messages
      @ai-sdk/openai             -> /v1/responses
      @ai-sdk/openai-compatible  -> /v1/chat/completions
    没有消息形态等价物的 SDK（如 @ai-sdk/google）直接排除，不猜。"""
    value = npm.strip().lower()
    if "anthropic" in value:
        return PROTOCOL_ANTHROPIC
    if value == "@ai-sdk/openai" or value.endswith("/openai"):
        return PROTOCOL_RESPONSES
    if "openai-compatible" in value:
        return PROTOCOL_CHAT
    return None


def is_zen_provider(provider_id: str, api: str) -> bool:
    """能力目录里哪些 provider 是本网关服务的 Zen 上游；
    Go 凭证通道是另一条本网关不服务的线，显式排除。"""
    value = f"{provider_id} {api}".lower()
    if "opencode-go" in value or "/go/" in value:
        return False
    return "opencode" in value or "/zen/" in value


# ── router：路由决策（无凭证轮换，恒匿名档） ────────────────────


class Router:
    def __init__(self, index: ModelIndex):
        self._index = index

    def supported(self, model: str) -> bool:
        """能否服务 = 能否建出路由；换个口径都会出现"列表有、路由拒"的自相矛盾。"""
        try:
            self.route(model)
            return True
        except ModelNotFound:
            return False

    def route(self, model: str) -> Route:
        """model 为裸上游 id。匿名档是唯一通道：只放免费模型——上游对非免费
        模型一律回 "Missing API key"（实测），先拦下来给出清晰的 400。"""
        if not self._index.knows_protocol(model):
            raise ModelNotFound(f'model "{model}" is not available in the configured Zen pool')
        if not self._index.servable(model):
            raise ModelNotFound(f'model "{model}" is not available in the configured Zen pool')
        record = self._index.record(model)
        if not self._anonymous_eligible(model):
            raise ModelNotFound(
                f'model "{model}" is not free: the public credential only serves free models'
            )
        return Route(
            provider=_PROVIDER_NAME,
            model=f"{_PROVIDER_NAME}/{model}",
            upstream_model=model,
            protocol=self._protocol_for(model),
            free=bool(record.free) if record else False,
        )

    def _anonymous_eligible(self, model: str) -> bool:
        """目录标价为准；未标价时回退 -free 命名惯例（与 zen2api 一致）。"""
        free = self._index.free(model)
        if free is not None:
            return free
        return bool(re.search("free", model, re.IGNORECASE))

    def list(self) -> list[dict[str, Any]]:
        """只列能建出路由的模型——客户端从 /v1/models 挑走的绝不落到路由拒绝。"""
        entries: list[dict[str, Any]] = []
        for model_id in self._index.servable_models():
            try:
                route = self.route(model_id)
            except ModelNotFound:
                continue
            record = self._index.record(model_id)
            entries.append({
                "id": route.model,
                "upstream_model": route.upstream_model,
                "protocol": route.protocol,
                "free": route.free,
                "metadata": dict(record.metadata) if record else empty_metadata(),
            })
        entries.sort(key=lambda e: e["id"])
        return entries

    def _protocol_for(self, model: str) -> str:
        # Chat 是目录还没描述该 ID 时的协议中立兜底。
        return self._index.protocol(model) or PROTOCOL_CHAT
