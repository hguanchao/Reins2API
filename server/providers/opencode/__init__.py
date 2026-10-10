"""OpenCode Zen 上游接入（唯一启用的 provider）。

组装路由/发现/上游/身份各模块，实现 gateway.base.Provider 契约。
凭证体系已按项目决策砍掉：单一 public 凭证、单次尝试、恒匿名档。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import logger
from gateway.base import ModelInfo, RequestIDs, Route, UpstreamResponse
from providers.opencode.discovery import discover
from providers.opencode.identity import derive_request_ids
from providers.opencode.routing import ModelIndex, Router
from providers.opencode.upstream import UPSTREAM, send_upstream, shape_free_request_body

_BEIJING_TZ = timezone(timedelta(hours=8))


class OpenCodeProvider:
    name = "opencode"
    upstream_url = UPSTREAM

    def __init__(self) -> None:
        self._index = ModelIndex()
        self._router = Router(self._index)
        self._lock = threading.Lock()
        self._refreshed_at: str | None = None

    # ── 目录 ──

    def refresh(self) -> None:
        """拉两源并合并；失败保留旧目录。"""
        from providers.opencode.upstream import PUBLIC_KEY

        records, available, errors = discover(UPSTREAM, PUBLIC_KEY)
        with self._lock:
            if records:
                self._index.replace(records)
            if available is not None:
                self._index.set_available(available)
            self._refreshed_at = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        counts = self._index.counts()
        logger.info(
            f"[目录] 刷新完成 协议记录={counts['protocols']} 凭证可见={counts['available']}"
            + (f" 错误={len(errors)}" if errors else "")
        )

    def status(self) -> dict[str, Any]:
        return {
            "upstream": UPSTREAM,
            "refreshed_at": self._refreshed_at,
            "models": len(self._router.list()),
        }

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=entry["id"],
                upstream_model=entry["upstream_model"],
                protocol=entry["protocol"],
                free=entry["free"],
                metadata=entry["metadata"],
            )
            for entry in self._router.list()
        ]

    # ── 路由 ──

    def supported(self, model: str) -> bool:
        return self._router.supported(self._bare(model))

    def route(self, model: str) -> Route:
        return self._router.route(self._bare(model))

    def _bare(self, model: str) -> str:
        prefix = f"{self.name}/"
        return model.removeprefix(prefix)

    # ── 请求 ──

    def derive_ids(self, headers: dict[str, str], body: dict[str, Any]) -> RequestIDs:
        return derive_request_ids(headers, body)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        return shape_free_request_body(body, protocol)

    def send(
        self,
        route: Route,
        protocol: str,
        body: bytes,
        ids: RequestIDs,
        *,
        stream: bool,
    ) -> UpstreamResponse:
        return send_upstream(protocol, body, ids, stream=stream)
