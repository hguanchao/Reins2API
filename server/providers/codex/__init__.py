"""Codex 上游接入（ChatGPT 后端 Codex 服务：OAuth PKCE 号池 + Responses 协议转发）。

组装 store（accounts 表）/ oauth / shape / upstream / classify / pool / login，
实现 gateway.base.Provider 契约。
"""

from __future__ import annotations

from providers.codex.provider import CodexProvider

__all__ = ["CodexProvider"]
