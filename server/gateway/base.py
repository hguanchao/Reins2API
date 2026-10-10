"""Provider 插件接口与注册表。

分层边界：gateway 核心只认识这里的契约，永不 import providers/*；
providers/* 实现本接口并向注册表注册自己（依赖方向：api → inference →
registry → provider → gateway 类型）。加一个新上游 = 加一个包 + 一行注册。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gateway.types import protocol_path


class ModelNotFound(ValueError):
    """模型无法路由（未知 / 未上架 / 无可用协议）。"""


def random_id(prefix: str, size: int) -> str:
    """prefix_ + size 字节随机 hex（移植自 zen2api util/identity.ts randomID）。"""
    return f"{prefix}_{secrets.token_hex(size)}"


def stable_id(prefix: str, value: str) -> str:
    """prefix_ + sha256(prefix\\0value) 前 24 位 hex——同一输入永远同一 ID。"""
    digest = hashlib.sha256(f"{prefix}\0{value}".encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


@dataclass(frozen=True)
class RequestIDs:
    session: str
    request: str
    project: str
    parent_session: str = ""


@dataclass(frozen=True)
class Route:
    provider: str
    model: str           # 对外规范名（opencode/big-pickle）
    upstream_model: str  # 实际打到上游的模型名（big-pickle）
    protocol: str        # 上游协议：chat | responses | anthropic
    free: bool


@dataclass
class ModelInfo:
    id: str               # 规范名 opencode/xxx
    upstream_model: str
    protocol: str
    free: bool
    metadata: dict[str, Any] = field(default_factory=dict)


class UpstreamResponse:
    """provider 拥有的上游 HTTP 响应的统一视图（内部持有 curl_cffi Response）。"""

    def __init__(self, status: int, headers: dict[str, str], raw: Any):
        self.status = status
        self.headers = headers
        self._raw = raw

    def read_body(self) -> bytes:
        try:
            return bytes(self._raw.content)
        finally:
            self.close()

    def iter_chunks(self, chunk_size: int = 4096) -> Iterator[bytes]:
        for chunk in self._raw.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass


@runtime_checkable
class Provider(Protocol):
    name: str
    upstream_url: str

    def refresh(self) -> None:
        """拉取并合并模型目录；失败保留旧目录。"""

    def status(self) -> dict[str, Any]:
        """运维状态（upstream / refreshed_at / models 计数）。"""

    def list_models(self) -> list[ModelInfo]: ...

    def supported(self, model: str) -> bool: ...

    def route(self, model: str) -> Route: ...

    def derive_ids(self, headers: dict[str, str], body: dict[str, Any]) -> RequestIDs:
        """从请求头与请求体派生稳定的会话/请求标识。"""

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        """上游侧请求塑形（免费档强制流式 + 核心工具集），返回 (塑形后, 是否改动)。"""

    def send(
        self,
        route: Route,
        protocol: str,
        body: bytes,
        ids: RequestIDs,
        *,
        stream: bool,
    ) -> UpstreamResponse: ...


class Registry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._lock = threading.Lock()

    def register(self, provider: Provider) -> None:
        with self._lock:
            self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        with self._lock:
            return self._providers[name]

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._providers)

    @property
    def default(self) -> Provider:
        """第一个注册的 provider，负责解析裸模型名。"""
        with self._lock:
            if not self._providers:
                raise RuntimeError("no provider registered")
            return next(iter(self._providers.values()))

    def resolve(self, model: str) -> Route:
        """规范名 provider/xxx 路由到对应 provider；裸名交给默认 provider。"""
        with self._lock:
            providers = dict(self._providers)
        if "/" in model:
            prefix = model.split("/", 1)[0]
            target = providers.get(prefix)
            if target is not None:
                return target.route(model)
        return self.default.route(model)

    def list_models(self) -> list[ModelInfo]:
        with self._lock:
            providers = list(self._providers.values())
        out: list[ModelInfo] = []
        for provider in providers:
            out.extend(provider.list_models())
        return out


registry = Registry()

__all__ = [
    "ModelInfo",
    "ModelNotFound",
    "Provider",
    "Registry",
    "RequestIDs",
    "Route",
    "UpstreamResponse",
    "protocol_path",
    "random_id",
    "registry",
    "stable_id",
]
