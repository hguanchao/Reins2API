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


class UpstreamUnavailable(RuntimeError):
    """provider 无可用上游资源（如号池全冷却/全占满）——推理层映射 429。"""


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
    model: str           # 对外规范名(oc:big-pickle / cn:deepseek-v4.1-flash)
    upstream_model: str  # 实际打到上游的模型名(big-pickle / deepseek-v4.1-flash)
    protocol: str        # 上游协议:chat | responses | anthropic
    free: bool
    realm: str = ""      # provider 内部选号域(cn/global);opencode 等无域 provider 恒空


@dataclass
class ModelInfo:
    id: str               # 规范名 opencode/xxx
    upstream_model: str
    protocol: str
    free: bool
    metadata: dict[str, Any] = field(default_factory=dict)


class UpstreamResponse:
    """provider 拥有的上游 HTTP 响应的统一视图（内部持有 curl_cffi Response）。"""

    def __init__(self, status: int, headers: dict[str, str], raw: Any, *, account: str = ""):
        self.status = status
        self.headers = headers
        self._raw = raw
        self.account = account  # 服务本响应的账号 uid（provider 填写；遥测分账用）

    def read_body(self) -> bytes:
        """整包读取响应体。

        stream=True 时 curl_cffi 的 .content / .text 恒为空（body 只在
        iter_content 里），而推理请求一律走 stream，所以错误路径必须回退到
        iter_content，否则上游的错误原文会被丢掉、只剩 HTTP 状态文案。
        """
        try:
            content = bytes(self._raw.content)
            if content:
                return content
            try:
                return b"".join(chunk for chunk in self._raw.iter_content(chunk_size=8192) if chunk)
            except AssertionError:
                return content  # 非流式响应不支持 iter_content，此时 content 已是全部
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


# 模型别名前缀（":" 分隔）→ provider 名。别名是客户端规范名，provider 发上游前
# 自行剥前缀取裸名；cn:/glb: 同属 workbuddy，realm 由 provider 从前缀解析。
ALIAS_PREFIXES = {"oc": "opencode", "cn": "workbuddy", "glb": "workbuddy"}


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
        """模型别名路由:oc:/cn:/glb: 按前缀表选 provider(provider 发上游前
        自行剥前缀取裸名);旧式 provider/xxx 兼容;裸名交给默认 provider。"""
        with self._lock:
            providers = dict(self._providers)
        target = self._alias_target(model, providers)
        if target is not None:
            return target.route(model)
        if "/" in model:
            target = providers.get(model.split("/", 1)[0])
            if target is not None:
                return target.route(model)
        return self.default.route(model)

    def route_target(self, model: str) -> tuple[Provider | None, str | None]:
        """与 resolve 同源的只读路由预判:返回 (provider, 未知前缀)。

        裸名/无前缀 → (default, None);别名与 provider/xxx 命中 → (provider, None);
        形如 xxx:yyy 或 xxx/yyy 但前缀未注册(含别名指向的 provider 未注册)→
        (None, xxx)——调用方据此回 400,而不是把它当裸名丢给默认 provider。"""
        with self._lock:
            providers = dict(self._providers)
        if ":" in model:
            prefix = model.split(":", 1)[0]
            name = self._alias_name(prefix)
            if name is None:
                return None, prefix
            target = providers.get(name)
            return (target, None) if target is not None else (None, prefix)
        if "/" in model:
            prefix = model.split("/", 1)[0]
            target = providers.get(prefix)
            if target is not None:
                return target, None
            return None, prefix
        return self.default, None

    @staticmethod
    def _alias_name(prefix: str) -> str | None:
        return ALIAS_PREFIXES.get(prefix)

    def _alias_target(self, model: str, providers: dict[str, Provider]) -> Provider | None:
        if ":" not in model:
            return None
        name = self._alias_name(model.split(":", 1)[0])
        return providers.get(name) if name else None

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
    "UpstreamUnavailable",
    "protocol_path",
    "random_id",
    "registry",
    "stable_id",
]
