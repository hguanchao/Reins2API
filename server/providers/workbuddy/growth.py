"""workbuddy 成长中心（growth 域）：连登兑换/抽奖、猫猫旅行、对话活跃上报。

契约逆向自官方 Web 成长中心（与 wb2api-panel internal/upstream/{streak,travel,report}.go
同款，2026-09 活动期实测）：

    growth 域走 chat_base + billing_headers（CN = copilot.tencent.com，路径不带 /v2）：
      GET  /activity/growth/streak                        连登档位 + 补签卡 + 兑换状态
      POST /activity/growth/redeem                        {"tier","client_token"} 兑换档位
      GET  /activity/growth/lottery/summary               抽奖次数
      POST /activity/growth/lottery/draw                  {"client_token"} 抽一次
      GET  /activity/growth/buddy/info                    猫档案（data.buddy=null 即无猫）
      POST /activity/growth/buddy/agreement               {"agree":true} 同意协议（幂等）
      POST /activity/growth/buddy/first                   {} 领养第一只猫（+300 分）
      GET  /activity/growth/buddy/travel/status            旅行状态机
      POST /activity/growth/buddy/travel/depart           {"location_id"} 派出
      POST /activity/growth/buddy/travel/claim            {"record_id"} 到站领奖

    billing 域走 billing_base + billing_headers（CN = codebuddy.cn）：
      POST /v2/report                            对话活跃上报（chat_request_send 事件）

响应信封统一 {code, msg, data}：HTTP 非 2xx 或 code != 0 都算失败，抛 GrowthError
（带 status/code/msg，供调用方区分「档位未解锁 403」「领养门槛未达 400」这类预期结论）。

**不做传输重试**：growth 全是维护类动作，其中兑换/抽奖/派出/领奖都是写路径——网络
超时重试可能在「上游已处理」之后再来一次（抽奖会白扣一次次数）。低频任务一天跑一到
两趟，失败留给下一趟更安全（与 checkin 的重试取舍相反，那里是幂等读+幂等写）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as crequests

from net.proxy import proxies_for
from providers.workbuddy.store import Account
from providers.workbuddy.upstream import (
    billing_base,
    chat_base,
    growth_headers,
    short_rpc_timeout,
)

# ── 端点路径 ──

STREAK_PATH = "/activity/growth/streak"
REDEEM_PATH = "/activity/growth/redeem"
LOTTERY_SUMMARY_PATH = "/activity/growth/lottery/summary"
LOTTERY_DRAW_PATH = "/activity/growth/lottery/draw"
BUDDY_INFO_PATH = "/activity/growth/buddy/info"
BUDDY_AGREEMENT_PATH = "/activity/growth/buddy/agreement"
BUDDY_FIRST_PATH = "/activity/growth/buddy/first"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
REPORT_PATH = "/v2/report"

# 探出四个地点收益/时长区间相同，固定选 4（古镇客栈）——选哪个都行，固定值才是幂等的
TRAVEL_LOCATION_ID = 4

# 旅行状态机取值
TRAVEL_IDLE = "idle"
TRAVEL_TRAVELING = "traveling"
TRAVEL_ARRIVED = "arrived"

# 档位状态：locked = 连续登录天数不足（兑换必 403）；claimed = 已兑过
TIER_LOCKED = "locked"
TIER_CLAIMED = "claimed"

# 领养门槛未达的业务错误关键词（HTTP 400 + 该文案）
_TASK_INCOMPLETE_MARKER = "first_buddy task not completed yet"


class GrowthError(RuntimeError):
    """growth / report 请求失败（网络 / HTTP / 业务码）。"""

    def __init__(self, message: str, *, status: int = 0, code: int | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = code


@dataclass
class StreakTier:
    """连登档位（7d/14d/28d）当前状态与兑换产出。"""

    tier: str = ""
    days: int = 0
    credit: int = 0
    energy: int = 0
    cards: int = 0
    chances: int = 0
    status: str = ""

    @property
    def redeemable(self) -> bool:
        """未解锁（locked，兑换必 403）与已兑换（claimed）之外都可兑换。"""
        return bool(self.tier) and self.status not in (TIER_LOCKED, TIER_CLAIMED)

    def reward_text(self) -> str:
        bits = []
        if self.credit:
            bits.append(f"+{self.credit} 积分")
        if self.energy:
            bits.append(f"+{self.energy} 能量")
        if self.cards:
            bits.append(f"补签卡×{self.cards}")
        if self.chances:
            bits.append(f"抽奖×{self.chances}")
        return " ".join(bits)


@dataclass
class StreakFull:
    """连登完整状态（GET /activity/growth/streak 的 data）。"""

    days: int = 0
    tier_statuses: dict[str, str] = field(default_factory=dict)
    tiers: list[StreakTier] = field(default_factory=list)
    makeup_cards: int = 0

    def redeemable_tiers(self) -> list[StreakTier]:
        return [tier for tier in self.tiers if tier.redeemable]


@dataclass
class TravelState:
    """猫猫旅行状态（state 缺失按未知处理，调用方只记日志不猜）。"""

    state: str = ""
    daily_limit_reached: bool = False
    record_id: int = 0
    reward_credit: float = 0.0


@dataclass
class Buddy:
    id: int = 0
    name: str = ""


# ── 请求与信封解析 ──


def client_token() -> str:
    """幂等令牌（前端 randomUUID 同款形态）：兑换/抽奖重放时上游据此去重。"""
    return str(uuid.uuid4())


def _snippet(text: str, limit: int = 160) -> str:
    return " ".join(str(text or "").split())[:limit]


def _envelope(response: crequests.Response) -> Any:
    """解 {code,msg,data} 信封：非 2xx / code != 0 抛 GrowthError。"""
    status = int(response.status_code)
    raw = response.text or ""
    if status >= 400:
        raise GrowthError(
            f"HTTP {status}: {_snippet(raw)}", status=status
        )
    try:
        body = response.json()
    except Exception:
        raise GrowthError(f"响应不是 JSON: {_snippet(raw)}", status=status) from None
    if not isinstance(body, dict):
        raise GrowthError(f"响应不是对象: {_snippet(raw)}", status=status)
    code = body.get("code")
    if code not in (0, None):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        raise GrowthError(
            f"code={code} msg={_snippet(str(body.get('msg') or ''), 120)}",
            status=status, code=code,
        )
    return body.get("data")


def call(account: Account, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """发 growth 域请求并解信封；body=None 时不带请求体（GET 走它）。"""
    url = f"{chat_base(account).rstrip('/')}{path}"
    headers = growth_headers(account)
    try:
        if method == "GET":
            response = crequests.get(
                url, headers=headers, timeout=short_rpc_timeout(),
                proxies=proxies_for(url), impersonate="chrome",
            )
        else:
            response = crequests.post(
                url, headers=headers, json=body if body is not None else {},
                timeout=short_rpc_timeout(), proxies=proxies_for(url), impersonate="chrome",
            )
    except crequests.RequestsError as exc:
        raise GrowthError(f"网络失败: {type(exc).__name__}: {exc}") from exc
    return _envelope(response)


# ── 连登 + 抽奖 ──


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


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


def streak_full(account: Account) -> StreakFull:
    """拉连登完整状态。字段缺失一律保留零值，不臆测。"""
    data = call(account, "GET", STREAK_PATH)
    payload = data if isinstance(data, dict) else {}
    streak = payload.get("streak") if isinstance(payload.get("streak"), dict) else {}
    redemption = (
        payload.get("redemption_status")
        if isinstance(payload.get("redemption_status"), dict) else {}
    )
    cards = payload.get("makeup_cards") if isinstance(payload.get("makeup_cards"), dict) else {}

    out = StreakFull(
        days=_as_int(streak.get("days")) or 0,
        makeup_cards=_as_int(cards.get("balance")) or 0,
        tier_statuses={
            tier: str(redemption.get(f"tier_{tier}_status") or "")
            for tier in ("7d", "14d", "28d")
        },
    )
    raw_tiers = redemption.get("tiers")
    for entry in raw_tiers if isinstance(raw_tiers, list) else []:
        if not isinstance(entry, dict):
            continue
        tier = str(entry.get("tier") or "")
        out.tiers.append(StreakTier(
            tier=tier,
            days=_as_int(entry.get("days")) or 0,
            credit=_as_int(entry.get("credit")) or 0,
            energy=_as_int(entry.get("energy")) or 0,
            cards=_as_int(entry.get("cards")) or 0,
            chances=_as_int(entry.get("chances")) or 0,
            status=out.tier_statuses.get(tier, ""),
        ))
    return out


def redeem_tier(account: Account, tier: str) -> None:
    """兑换档位；未解锁时上游 403（调用方按 locked 状态提前跳过即可）。"""
    call(account, "POST", REDEEM_PATH, {"tier": tier, "client_token": client_token()})


def lottery_chances(account: Account) -> int:
    """当前抽奖次数（次数只从档位兑换获得）。"""
    data = call(account, "GET", LOTTERY_SUMMARY_PATH)
    payload = data if isinstance(data, dict) else {}
    return _as_int(payload.get("chances")) or 0


def lottery_draw(account: Account) -> Any:
    """抽一次，返回奖品负载（形状随活动期变化，原样回给调用方记日志）。"""
    return call(account, "POST", LOTTERY_DRAW_PATH, {"client_token": client_token()})


# ── 对话活跃上报 ──


def _chat_request_event(account: Account, conversation_id: str, request_id: str,
                        model_id: str, model_name: str) -> dict[str, Any]:
    """客户端 chat_request_send 事件完整形状。

    照抄客户端的全字段（不是最小 3 字段）：上游后续加严时缺字段会被判成非法事件。
    风控口径是「每号每天 1 次即可」，不做多时点高频上报。
    """
    now_ms = int(time.time() * 1000)
    return {
        "eventCode": "chat_request_send",
        "timestamp": now_ms,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conversation_id,
        "requestId": request_id or conversation_id,
        "inputLength": 12,
        "requestModelId": model_id,
        "requestModelName": model_name,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now_ms,
        "traceId": "",
        "rootRequestId": request_id or conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "agentType": "conversation",
        # userId 必填：缺失时上游 200 但静默丢弃事件（连登不点亮、first_buddy 不解锁）
        "userId": account.uid,
    }


def report_chat_activity(account: Account, conversation_id: str, request_id: str = "",
                         model_id: str = "deepseek-v4-flash",
                         model_name: str = "DeepSeek V4 Flash") -> None:
    """上报一条对话活跃事件（billing 域 /v2/report）。

    一条上报同时点亮 growth 连登天数并解锁 first_buddy 任务（领养前置）。
    """
    event = _chat_request_event(account, conversation_id, request_id,
                                model_id, model_name or model_id)
    url = f"{billing_base(account).rstrip('/')}{REPORT_PATH}"
    try:
        response = crequests.post(
            url, headers=growth_headers(account), json=[event],
            timeout=short_rpc_timeout(), proxies=proxies_for(url), impersonate="chrome",
        )
    except crequests.RequestsError as exc:
        raise GrowthError(f"网络失败: {type(exc).__name__}: {exc}") from exc
    _envelope(response)


def streak_days(account: Account) -> int:
    """连登天数（只读 oracle）：上报后回读用它自检「200 但被静默丢弃」。"""
    return streak_full(account).days


# ── 猫猫旅行 ──


def buddy_info(account: Account) -> Buddy | None:
    """当前猫档案；None 表示无猫（data.buddy 为 null / 空对象）。"""
    data = call(account, "GET", BUDDY_INFO_PATH)
    payload = data if isinstance(data, dict) else {}
    raw = payload.get("buddy")
    if not isinstance(raw, dict) or not raw:
        return None
    return Buddy(id=_as_int(raw.get("id")) or 0, name=str(raw.get("name") or ""))


def buddy_agreement(account: Account) -> None:
    """同意领养协议（幂等，重复调用无副作用）。"""
    call(account, "POST", BUDDY_AGREEMENT_PATH, {"agree": True})


def buddy_first(account: Account) -> None:
    """领养第一只猫；门槛未达时上游 400（is_task_incomplete 判定）。"""
    call(account, "POST", BUDDY_FIRST_PATH, {})


def travel_status(account: Account) -> TravelState:
    data = call(account, "GET", TRAVEL_STATUS_PATH)
    payload = data if isinstance(data, dict) else {}
    return TravelState(
        state=str(payload.get("state") or ""),
        daily_limit_reached=payload.get("daily_limit_reached") is True,
        record_id=_as_int(payload.get("record_id")) or 0,
        reward_credit=_as_float(payload.get("reward_credit")) or 0.0,
    )


def travel_depart(account: Account, location_id: int = TRAVEL_LOCATION_ID) -> None:
    call(account, "POST", TRAVEL_DEPART_PATH, {"location_id": int(location_id)})


def travel_claim(account: Account, record_id: int) -> float:
    """领取到站奖励，返回 reward_credit（字段缺失按 0，不视为失败）。"""
    data = call(account, "POST", TRAVEL_CLAIM_PATH, {"record_id": int(record_id)})
    payload = data if isinstance(data, dict) else {}
    return _as_float(payload.get("reward_credit")) or 0.0


# ── 预期结论判定（调用方据此静默跳过，不当失败处理）──


def is_tier_locked(err: Exception) -> bool:
    """档位未解锁：HTTP 403「连续登录天数不足」。"""
    return isinstance(err, GrowthError) and err.status == 403


def is_task_incomplete(err: Exception) -> bool:
    """领养门槛未达：HTTP 400 + first_buddy 文案。

    门槛真实来源是「当日无活跃上报」（上报点亮 first_buddy 任务），不是账号问题；
    当日不应重试，避免对上游重试轰炸。
    """
    if not isinstance(err, GrowthError) or err.status != 400:
        return False
    return _TASK_INCOMPLETE_MARKER in str(err).lower()
