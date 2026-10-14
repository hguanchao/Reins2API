"""provider 共享助手：跨 provider 复用、不隶属任何单一上游的极小工具。

放在 providers/ 顶层（而非某个 provider 包里）：插件轴要求 provider 之间互不
import，凡是两家以上共用的逻辑都必须上提到共享层。这里收的都是「逐字相同」的
碎片——此前在 codex / workbuddy / opencode 三处各抄一份，改一处漏两处是常态：

- `BEIJING_TZ` / `stamp`：状态展示用的北京时间字符串（全项目时间口径一致）。
- `refresh_result`：手动刷新回报形状（三家同形状，api 层按同一组键拼响应）。
- `RawBytes`：已整包读出的上游错误体视图，满足 UpstreamResponse.raw 契约。
- `apply_retry_after`：从响应头解析上游明示等待时长（两家分类器共用）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

BEIJING_TZ = timezone(timedelta(hours=8))

_RETRY_AFTER_HEADERS = ("retry-after", "retry-after-ms", "x-ratelimit-reset")
_RETRY_AFTER_SANITY_S = 2 * 3600  # 超过视为上游异常值丢弃（与软冷却封顶同量级）


class RetryAfterCarrier(Protocol):
    """apply_retry_after 的入参契约：任何带 retry_after_s 字段的分类结果。"""

    retry_after_s: float


def stamp(ms: int) -> str | None:
    """epoch ms → 北京时间字符串（status 展示用）。"""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def refresh_result(
    *, added: int = 0, removed: int = 0, failed: int = 0, error: str = ""
) -> dict[str, Any]:
    """手动刷新的回报形状（三家同形状，api 层按同一组键拼响应与界面提示）。"""
    return {"added": added, "removed": removed, "failed": failed, "error": error}


class RawBytes:
    """已整包读出的上游错误体视图（满足 UpstreamResponse 的 raw 契约）。"""

    def __init__(self, data: bytes):
        self._data = data

    @property
    def content(self) -> bytes:
        return self._data

    def iter_content(self, chunk_size: int = 8192):
        if self._data:
            yield self._data

    def close(self) -> None:
        return None


def apply_retry_after(result: RetryAfterCarrier, headers: dict[str, str] | None) -> Any:
    """从响应头解析上游明示等待时长（retry-after → retry-after-ms → x-ratelimit-reset）。

    纯数字才解析（HTTP-Date 不支持，宁缺毋滥）；非正/超上限丢弃，回落本地退避计算。
    只写 `result.retry_after_s`——两家分类器的 Classified 结构不同，但都有这个字段。
    """
    if not headers:
        return result
    lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    now_s = time.time()
    for name in _RETRY_AFTER_HEADERS:
        raw = lowered.get(name) or ""
        if not raw or not raw.isdigit() or len(raw) > 16:
            continue
        n = int(raw)
        if name == "x-ratelimit-reset":
            # epoch 时刻而非时长：秒口径（10 位）与毫秒口径（13 位）折算剩余量
            if len(raw) < 8:
                continue
            epoch_s = n / 1000.0 if len(raw) >= 13 else float(n)
            n = int(epoch_s - now_s)
        if n <= 0 or n > _RETRY_AFTER_SANITY_S:
            continue
        result.retry_after_s = float(n)
        break
    return result
