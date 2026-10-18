"""workbuddy 每日签到：活动状态查询 + 领取（billing 域，幂等）。

接口契约逆向自官方桌面端成长中心：

    POST {billing_base}/v2/billing/meter/checkin-activity-status   查状态
    POST {billing_base}/v2/billing/meter/daily-checkin             领取

响应契约（HTTP 状态与业务码双判，业务负载可能被 data/result 包裹）：

    成功      2xx 且负载含 credit，如 {"credit": 100}
    今日已签  null 体，或 400 + {"code":10001,"msg":"今天已签到，请明天再来"}
    企业号    400 + {"code":10001,"msg":"企业账号不支持该操作"}
    认证拒绝  401 / 403

幂等是整套设计的地基：重复领取只会返回"已签"，所以重试与每日多次轮询都安全。
provider 层的本地日期台账（accounts.last_checkin_day）只是把重复调用压成零成本的
短路，不承担正确性——台账与上游不一致时以调用上游为准。

CN 专属：global 域没有该活动，企业号上游直接拒绝，两者都在发请求前短路
（见 skip_reason）。每日对注定失败的端点空撞既是日志噪音，也是风控特征。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.settings import BEIJING_TZ as _BEIJING_TZ

# crequests 指向出站会话复用层（每线程一条 keep-alive 连接，逐请求清 Cookie）：
# 调用写法与 curl_cffi 顶层 API 同形，只是不再每次新建 Session。类型与异常
# (RequestsError/Response) 在该模块内原样再导出。
from net import outbound as crequests
from net.proxy import proxies_for
from providers.workbuddy.store import Account
from providers.workbuddy.upstream import (
    billing_base,
    billing_headers,
    short_rpc_timeout,
)

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

CODE_OK = 0
CODE_ALREADY = 10001  # 今日已签到 / 企业账号不支持（按 msg 文案区分）

# 单账号签到结果状态
STATE_CLAIMED = "claimed"    # 本轮领到积分
STATE_ALREADY = "already"    # 今日已签（幂等命中）
STATE_INACTIVE = "inactive"  # 活动未开启
STATE_SKIPPED = "skipped"    # 账号类型不适用（global/企业号），未发请求
STATE_ERROR = "error"        # 请求失败（provider 层用，checkin 内部以异常表达）


# 网络类失败的短退避（秒）。签到接口幂等，重试不会重复领取；定时任务最常撞上的
# 是"刚开机/刚唤醒，网络还要几十秒才就绪"，单次 5 秒重试跨不过这个窗口。
_RETRY_DELAYS = (5.0, 15.0)


class CheckinError(RuntimeError):
    """签到请求失败（网络 / HTTP / 业务码异常）。"""


@dataclass
class CheckinStatus:
    """checkin-activity-status 的观测值；缺失字段一律保留 None（不臆测）。"""

    active: bool = True
    today_checked_in: bool = False
    today_credit: float | None = None
    streak_days: int | None = None
    total_credits: float | None = None
    is_streak_day: bool = False
    activity_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def describe(self, prefix: str = "今日已签") -> str:
        bits: list[str] = []
        if self.today_credit is not None:
            bits.append(f"今日 +{fmt_credit(self.today_credit)}")
        if self.streak_days is not None:
            bits.append(f"连续 {self.streak_days} 天")
        if self.total_credits is not None:
            bits.append(f"累计 {fmt_credit(self.total_credits)} 积分")
        return f"{prefix}（{'，'.join(bits)}）" if bits else prefix


@dataclass
class CheckinOutcome:
    """一次签到尝试的结论（含汇报文案）。"""

    state: str
    message: str = ""
    credit: float | None = None
    streak_days: int | None = None
    total_credits: float | None = None
    is_streak_day: bool = False
    http_status: int = 0

    @property
    def ok(self) -> bool:
        """是否需要人工关注：只有 ERROR 算失败。"""
        return self.state != STATE_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "credit": self.credit,
            "streak_days": self.streak_days,
            "total_credits": self.total_credits,
            "is_streak_day": self.is_streak_day,
            "http_status": self.http_status,
        }


def beijing_today() -> str:
    """北京日期 YYYY-MM-DD：签到按 UTC+8 结算一天，本机时区不参与。"""
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")


def fmt_credit(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def skip_reason(account: Account) -> str:
    """不适用的账号在发请求前短路，返回空串表示可以签到。"""
    if account.realm == "global":
        return "global 域无签到体系"
    if account.enterprise_id:
        return "企业账号不支持签到"
    if not account.access_token:
        return "缺少 access_token"
    return ""


# ── 响应解析 ──


def _dig(obj: Any, key: str) -> Any:
    """在可能被 data/result/resp/response 包裹的负载里找字段（对齐官方桌面端）。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for wrapper in ("data", "result", "resp", "response"):
            inner = obj.get(wrapper)
            if isinstance(inner, dict):
                found = _dig(inner, key)
                if found is not None:
                    return found
    return None


def _business_code(body: Any) -> int | None:
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, bool):
            return None
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.strip().lstrip("-").isdigit():
            return int(code.strip())
    return None


def _message(body: Any) -> str:
    for key in ("msg", "message", "error"):
        value = _dig(body, key)
        if isinstance(value, str) and value:
            return value
    return ""


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _first_float(body: Any, *keys: str) -> float | None:
    for key in keys:
        number = _as_float(_dig(body, key))
        if number is not None:
            return number
    return None


def _parse(response: crequests.Response) -> Any:
    """JSON 体原样返回；解析失败包成 {"raw": ...} 与真实的 null 体区分开。"""
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:200]}


def _call(account: Account, path: str, *, retries: int) -> tuple[int, Any]:
    """POST {billing_base}{path}，网络类失败按退避重试。

    只重试传输层异常：4xx/5xx 是服务端已经给出的答案，重试改变不了结论，
    只会把上游调用量放大成风控特征。
    """
    url = f"{billing_base(account).rstrip('/')}{path}"
    headers = billing_headers(account)
    timeout = short_rpc_timeout()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = crequests.post(
                url,
                headers=headers,
                json={},
                timeout=timeout,
                proxies=proxies_for(url),
                impersonate="chrome",
            )
            return int(response.status_code), _parse(response)
        except crequests.RequestsError as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
    raise CheckinError(f"网络失败: {type(last_error).__name__}: {last_error}")


# ── 对外动作 ──


def checkin_status(account: Account) -> CheckinStatus:
    """查今日签到状态（只读，可重试）。"""
    http_status, body = _call(account, STATUS_PATH, retries=1)
    if not 200 <= http_status < 300:
        raise CheckinError(
            f"状态查询 HTTP {http_status}: {_message(body) or ''}".rstrip(": ")
        )
    code = _business_code(body)
    if code not in (None, CODE_OK):
        raise CheckinError(f"状态查询 code={code} msg={_message(body)}")
    # 无法解析的负载（截断的 HTML/空体）按失败处理：放行会被当成"未签"去领取，
    # 把一次网关故障变成一次多余的写请求。
    if not isinstance(body, dict) or set(body) == {"raw"}:
        raise CheckinError("状态查询返回无法解析的负载")

    today_credit = _first_float(body, "today_credit", "daily_credit")
    streak_obj = _dig(body, "streak")
    streak_days = _as_int(_dig(body, "streak_days"))
    if streak_days is None and isinstance(streak_obj, dict):
        streak_days = _as_int(streak_obj.get("days"))

    return CheckinStatus(
        # active 缺失按开启处理（旧接口不下发该字段，缺省不能把活动判成关闭）
        active=_dig(body, "active") is not False,
        today_checked_in=_dig(body, "today_checked_in") in (True, 1),
        today_credit=today_credit,
        streak_days=streak_days,
        total_credits=_first_float(body, "total_credits"),
        is_streak_day=bool(_dig(body, "is_streak_day")),
        activity_name=str(_dig(body, "activity_name") or ""),
        raw=body,
    )


def claim_checkin(account: Account) -> CheckinOutcome:
    """领取今日积分（写路径，幂等）。"""
    http_status, body = _call(account, CLAIM_PATH, retries=1)
    if http_status in (401, 403):
        raise CheckinError(
            f"签到被拒 HTTP {http_status}（凭证可能已失效，请重新登录）"
        )
    if body is None:
        # 契约：null 体 = 服务端判定今日已领
        return CheckinOutcome(STATE_ALREADY, "今日已签过（服务端判定已领取）")
    if _business_code(body) == CODE_ALREADY or "已签" in _message(body):
        return CheckinOutcome(STATE_ALREADY, _message(body) or "今日已签过")

    credit = _as_float(_dig(body, "credit"))
    if 200 <= http_status < 300 and credit is not None:
        return CheckinOutcome(
            STATE_CLAIMED,
            f"签到成功 +{fmt_credit(credit)} 积分",
            credit=credit,
            http_status=http_status,
        )
    raise CheckinError(
        f"领取失败 HTTP {http_status} code={_business_code(body)} "
        f"msg={_message(body) or '-'}"
    )


def run_checkin(account: Account) -> CheckinOutcome:
    """一轮签到：短路不适用账号 → 查状态 → 未签才领 → 回读连签/累计。

    硬失败抛 CheckinError（由 provider 转成 STATE_ERROR 并记日志）；今天是否
    已经签过这类"正常结论"一律用返回值表达，调用方无需区分异常与业务态。
    """
    reason = skip_reason(account)
    if reason:
        return CheckinOutcome(STATE_SKIPPED, reason)

    status = checkin_status(account)
    if not status.active:
        name = f"（{status.activity_name}）" if status.activity_name else ""
        return CheckinOutcome(STATE_INACTIVE, f"签到活动未开启{name}")
    if status.today_checked_in:
        return CheckinOutcome(
            STATE_ALREADY, status.describe(),
            credit=status.today_credit, streak_days=status.streak_days,
            total_credits=status.total_credits, is_streak_day=status.is_streak_day,
        )

    outcome = claim_checkin(account)
    if outcome.state != STATE_CLAIMED:
        return outcome

    # 回读连签/累计：领取已经成功，回读失败不能把结论改写成失败。
    try:
        fresh = checkin_status(account)
    except CheckinError:
        return outcome
    outcome.streak_days = fresh.streak_days
    outcome.total_credits = fresh.total_credits
    outcome.is_streak_day = fresh.is_streak_day
    if fresh.streak_days is not None or fresh.total_credits is not None:
        outcome.message = fresh.describe(outcome.message)
    return outcome
