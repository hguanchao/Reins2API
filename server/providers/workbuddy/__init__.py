"""WorkBuddy 上游接入（CodeBuddy/workbuddy.ai 号池 + 反向代理网关）。

组装 store（accounts 表）/ classify / shape / dsml / upstream / provider，
实现 gateway.base.Provider 契约。设计与取舍见 docs/workbuddy-provider-design.md。
"""

from __future__ import annotations

from providers.workbuddy.provider import WorkBuddyProvider

__all__ = ["WorkBuddyProvider"]
