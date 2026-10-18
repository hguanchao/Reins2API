"""Provider 插件接口与注册表。

分层边界：gateway 核心只认识这里的契约，永不 import providers/*；
providers/* 实现本接口并向注册表注册自己（依赖方向：api → inference →
registry → provider → gateway 类型）。加一个新上游 = 加一个包 + 一行注册。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gateway.jsonutil import close_quietly
from gateway.types import protocol_path


class ModelNotFound(ValueError):
    """模型无法路由（未知 / 未上架 / 无可用协议）。"""


class UpstreamUnavailable(RuntimeError):
    """provider 无可用上游资源（如号池全冷却/全占满）——推理层映射 429。"""


class ModelUnavailable(RuntimeError):
    """该模型在当前账号池里被模型级冷却挡住（号都在，只是都不服务这个模型）。

    与 UpstreamUnavailable 的区别在调用方该怎么做：池子不可用该退避重试，模型不可用
    换模型才有用。所以推理层映射 400 + `model_unavailable`——避免客户端把"这个模型
    不可用"当成服务端过载反复重试（历史形态：503 → 客户端自动重试 5 次）。
    """

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def random_id(prefix: str, size: int) -> str:
    """prefix_ + size 字节随机 hex。"""
    return f"{prefix}_{secrets.token_hex(size)}"


@dataclass(frozen=True)
class RequestIDs:
    session: str
    request: str
    project: str
    parent_session: str = ""
    # 客户端稳定键（ip+UA 哈希，跨会话不变）：prompt_cache_key 用它而不是会话键，
    # 同一客户端的新会话第一发才能吃到上段静态前缀缓存。
    client: str = ""


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
    # 展示名与能力块由 **provider** 算好（厂商字段名只活在 provider 里）：
    # capabilities 是标准化后的平铺字段，直接进 /v1/models；gateway 不做解释。
    display_name: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)


class _FirstChunkStamp:
    """首个非空块到达时回调一次（一次性），其余一切透传给原 raw。

    iter_content 是流式与非流式两条路径的共同终点——read_body() 在 curl 的
    stream 模式下也回退到它——所以只包这一处就够，不必在两个调用点各埋一次。
    """

    def __init__(self, raw: Any, stamp: Callable[[], None]):
        self._raw = raw
        self._stamp = stamp
        self._fired = False

    def iter_content(self, chunk_size: int = 8192):
        for chunk in self._raw.iter_content(chunk_size=chunk_size):
            if chunk and not self._fired:
                self._fired = True
                self._stamp()
            yield chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class UpstreamResponse:
    """provider 拥有的上游 HTTP 响应的统一视图（内部持有 curl_cffi Response）。"""

    def __init__(self, status: int, headers: dict[str, str], raw: Any, *, account: str = ""):
        self.status = status
        self.headers = headers
        self._raw = raw
        self.account = account  # 服务本响应的账号 uid（provider 填写；遥测分账用）

    def stamp_first_chunk(self, stamp: Callable[[], None]) -> None:
        """给 raw 包一层：首个非空块到达时回调一次 stamp()（用于首字耗时打点）。

        放在这里而不是调用方直接改写 raw——raw 是本类的私有实现细节，
        外部拿到的是 read_body()/iter_chunks() 这层视图。
        """
        self._raw = _FirstChunkStamp(self._raw, stamp)

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
        close_quietly(self._raw)


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

    def derive_ids(
        self, headers: dict[str, str], body: dict[str, Any], client_ip: str = ""
    ) -> RequestIDs:
        """从请求头/请求体/客户端地址派生会话、请求与客户端稳定标识。"""

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
ALIAS_PREFIXES = {
    "oc": "opencode", "cn": "workbuddy", "glb": "workbuddy", "cx": "codex",
}


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

    def owner(self, model: str) -> str:
        """模型名归属的 provider 名（展示用 /v1/models 的 owned_by）。

        与 route_target 同源：别名前缀 → 该 provider；provider/xxx → 该 provider；
        裸名 → 默认 provider。未注册的前缀落到默认 provider（目录里的 id 都是
        已注册 provider 产出的，这里只做展示，不承担拒绝职责）。
        """
        provider, unknown = self.route_target(model)
        return provider.name if provider is not None and not unknown else self.default.name

    def route_target(self, model: str) -> tuple[Provider | None, str | None]:
        """只读路由预判:返回 (provider, 未知前缀)。

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
    "ModelUnavailable",
    "Provider",
    "Registry",
    "RequestIDs",
    "Route",
    "UpstreamResponse",
    "UpstreamUnavailable",
    "protocol_path",
    "random_id",
    "registry",
]
