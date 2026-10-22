"""opencode 免费通道错误分类测试（对照 opencode2api abuse.go 的实战词表）。

三种拒绝该做的事完全不同，分类错了的代价是真实发生过的：IP 封禁被当线路抖动
重发 4 次（每次顶着拉黑 IP 撞上游），客户端只看到一串英文原文。这里锁定：

  * 403 IP 封禁三种措辞 → free_tier_ip_blocked（不可重试）；
  * 403 形态门 FreeTierError → deterministic_rejection（不可重试）——注意它
    **不是** IP 封禁：那是请求形态问题；
  * 429 FreeUsageLimitError → free_usage_limit（不可重试）；其余 429 保持瞬态；
  * 400/410/422 带 invalid_request / ModelDeprecated 标记 → deterministic；
  * 5xx / 其余 → transient（可重试）。

运行：cd server && uv run python tests/test_opencode_rejects.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.opencode.rejects import (
    DETERMINISTIC,
    FREE_LIMIT,
    IP_BLOCKED,
    TRANSIENT,
    classify_free_rejection,
)

FREETIER_403 = json.dumps({
    "type": "error",
    "error": {"type": "FreeTierError",
              "message": "OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep building."},
}).encode()

ABUSE_403_A = b"we've detected abusive traffic from this IP address"
ABUSE_403_B = b"Forbidden, OpenCode Free Model Gateway Detect Abuse Traffic"
ABUSE_403_C = b"Abuse of traffic"

FREE_LIMIT_429 = json.dumps({
    "type": "error",
    "error": {"type": "FreeUsageLimitError",
              "message": "Free usage limit reached. Subscribe to OpenCode Go to keep going: https://opencode.ai/go"},
}).encode()

DEPRECATED_410 = json.dumps({
    "type": "error",
    "error": {"type": "ModelDeprecated", "message": "Model x has been deprecated."},
}).encode()

CONTEXT_400 = json.dumps({
    "error": {"type": "invalid_request_error", "message": "maximum context length exceeded"},
}).encode()

PLAIN_500 = b"internal server error"


def test_ip_block_wordings():
    for body in (ABUSE_403_A, ABUSE_403_B, ABUSE_403_C):
        kind = classify_free_rejection(403, body)
        assert kind is IP_BLOCKED, f"{body[:40]} -> {kind.kind}"
    # 长文案形态：两段上下文同时出现才算
    long_form = b"free models aren't available from your current network because of abuse"
    assert classify_free_rejection(403, long_form) is IP_BLOCKED
    # 只出现一半上下文不算
    assert classify_free_rejection(403, b"free models aren't available") is not IP_BLOCKED


def test_freetier_shape_gate_is_deterministic_not_ip_block():
    kind = classify_free_rejection(403, FREETIER_403)
    assert kind is DETERMINISTIC
    assert kind.kind == "deterministic_rejection" and not kind.retryable


def test_free_usage_limit():
    assert classify_free_rejection(429, FREE_LIMIT_429) is FREE_LIMIT
    # 文案兜底（类型名丢了也能认）
    assert classify_free_rejection(429, b"Free usage limit reached, come back later") is FREE_LIMIT
    # 其余 429（真限流）：无归因，但单出口上同样不重发。
    kind = classify_free_rejection(429, b"rate limit exceeded")
    assert kind is not TRANSIENT and not kind.retryable


def test_deterministic_request_rejections():
    assert classify_free_rejection(410, DEPRECATED_410) is DETERMINISTIC
    assert classify_free_rejection(400, CONTEXT_400) is DETERMINISTIC
    # 403 的 invalid_request 不在确定性标记里走 403 分支（无归因 4xx，不重发）
    kind = classify_free_rejection(403, b"invalid_request_error something")
    assert not kind.retryable


def test_transient_errors_stay_5xx_only():
    # 只有 5xx 可重试；4xx 在单凭证单出口下重发必然是同一个答案。
    assert classify_free_rejection(500, PLAIN_500) is TRANSIENT
    assert classify_free_rejection(502, b"bad gateway") is TRANSIENT
    for status in (400, 403, 404, 429):
        kind = classify_free_rejection(status, b"some body")
        assert not kind.retryable, f"{status} 不该可重试"


def test_string_body_accepted():
    # 调用方可能拿的是已解码字符串
    assert classify_free_rejection(429, "Free usage limit reached") is FREE_LIMIT


# ── 内建运行入口（无 pytest 环境直接跑）──

_ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    failed = 0
    for test in _ALL:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_ALL) - failed}/{len(_ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
