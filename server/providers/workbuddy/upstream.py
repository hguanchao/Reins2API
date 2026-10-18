"""workbuddy 上游客户端：端点、头族、refresh、模型目录、余额。

HTTP 统一 curl_cffi
`impersonate="chrome"` + `net.proxy.proxies_for()`（与全项目出口决策同一条路）；
chat 恒 stream=True（上游拒绝非流式），超时语义 = 连接 15s + 空闲掐流
（curl_cffi stream 模式 LOW_SPEED 监控），短 RPC 120s 总上限。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.settings import (
    BEIJING_TZ,
    WB_BILLING_BASE,
    WB_CLI_VERSION,
    WB_CLIENT_VERSION,
    WB_DEVICE_TOKEN,
    WB_GLOBAL_BASE,
    WB_ORIGIN,
    WB_TIMEOUT_CONNECT,
    WB_TIMEOUT_IDLE,
    WB_TIMEOUT_TTFB,
    WB_UPSTREAM_BASE,
    WB_USER_AGENT,
)

# crequests 指向出站会话复用层（每线程一条 keep-alive 连接，逐请求清 Cookie）：
# 调用写法与 curl_cffi 顶层 API 同形，只是不再每次新建 Session。类型与异常
# (RequestsError/Response) 在该模块内原样再导出。
from net import outbound as crequests
from net.proxy import proxies_for
from providers.workbuddy.store import Account

CHAT_COMPLETIONS_PATH = "/v2/chat/completions"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
V3_CONFIG_PATH = "/v3/config"
USER_RESOURCE_PATH = "/billing/meter/get-user-resource"
USER_RESOURCE_PATH_V2 = "/v2/billing/meter/get-user-resource"
# 企业版额度不属「个人资源包」体系：get-user-resource* 对企业号恒返回空 Accounts
# （实测 code 0 且 Accounts null），企业额度走这个端点。
ENTERPRISE_USAGE_PATH = "/v2/billing/meter/get-enterprise-user-usage"

# 「不限量」在企业额度里的等价表示（limitNum < 0）：余额给一个不可能触顶的大数、
# 总量给 -1 标记不限。这样路由权重（credits 项）与积分保底都不构成限制。
ENTERPRISE_UNLIMITED_REMAIN = 1 << 40
ENTERPRISE_UNLIMITED_TOTAL = -1.0

# /v3/config 对 UA 敏感（UA 常量紧随 default_user_agent 定义）。
V3_CONFIG_PRODUCT = "SaaS"


def message_id() -> str:
    """消息级 32 位 hex（X-Conversation-Message-ID / X-Request-ID，每条独立）。"""
    return uuid.uuid4().hex


def derive_account_stable_id(uid: str, purpose: str) -> str:
    """按 uid + 用途盐派生 36 hex 稳定标识（跨重启恒定、账号间互异）。

    purpose="machine" → X-Machine-ID（设备级）；"session" → X-Session-ID
    （账号固定会话）。防多号被上游按设备指纹缺失/漂移关联风控。
    """
    digest = hashlib.sha256(f"wb2a:{purpose}:{uid}".encode()).hexdigest()
    return digest[:36]


def default_user_agent(realm: str = "cn") -> str:
    """三段式官方桌面端 UA：`WorkBuddy/<v> <platform>/<v> CLI/<v>`。

    global 平台段必须是 `WorkBuddy AI`（送错可能触发上游 11140 风控）。
    """
    platform = "WorkBuddy AI" if realm == "global" else "WorkBuddy"
    return f"WorkBuddy/{WB_CLIENT_VERSION} {platform}/{WB_CLIENT_VERSION} CLI/{WB_CLI_VERSION}"


# /v3/config 对 UA 敏感：必须能解析出 CodeBuddy 版本号，否则 400 code=12403；
# 且不同 UA 下发不同规模的模型集合——global 必须用完整桌面端 UA 才拿得到含
# gpt-6-* 在内的完整目录，裸 `CLI/<v>` 与 IDE UA 都会被降级。故 global 复用
# default_user_agent("global")，CN 沿用 IDE UA。（实测：完整桌面端 UA 下发 28 条，
# 裸 CLI 降级成 22 条，IDE UA 只剩别名/路由条目）
V3_CONFIG_UA_CN = "CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0"
V3_CONFIG_UA_GLOBAL = default_user_agent("global")


# ── 按账号域（realm）分流 ────────────────────────────────────────
# CN 与 global 是两套互不相通的上游部署，
# 账号凭证、模型目录、定价各自独立——chat/refresh/models/billing 全部按 realm 选 base。


def chat_base(account: Account) -> str:
    return WB_GLOBAL_BASE if account.realm == "global" else WB_UPSTREAM_BASE


def billing_base(account: Account) -> str:
    return WB_GLOBAL_BASE if account.realm == "global" else WB_BILLING_BASE


def origin_for(account: Account) -> str:
    return WB_GLOBAL_BASE if account.realm == "global" else WB_ORIGIN


def _host(account: Account) -> str:
    return (
        chat_base(account)
        .removeprefix("https://")
        .removeprefix("http://")
        .rstrip("/")
    )


# ── 头族构造（common / chat / billing / refresh 四组）──


def common_headers(account: Account) -> dict[str, str]:
    origin = origin_for(account)
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "origin": origin,
        "referer": f"{origin}/",
        "user-agent": WB_USER_AGENT or default_user_agent(account.realm),
        "x-codebuddy-request": "1",
        "accept-language": "en-US" if account.realm == "global" else "zh-CN",
        "x-machine-id": derive_account_stable_id(account.uid, "machine"),
        "x-session-id": derive_account_stable_id(account.uid, "session"),
    }


def chat_headers(account: Account, conversation_id: str = "",
                 conversation_request_id: str = "") -> dict[str, str]:
    """chat 出站头：common + 账号头 + 用量归属 + 会话头族。

    安全红线：绝不在 chat 请求里携带 X-Refresh-Token。
    """
    headers = common_headers(account)
    headers["accept"] = "application/json, text/event-stream"
    if account.access_token:
        headers["authorization"] = f"Bearer {account.access_token}"
    else:
        headers["x-no-authorization"] = "1"
    if account.uid:
        headers["x-user-id"] = account.uid
    # 域头按 realm 分发：CN 缺省 X-No-* 约定（与官方 CLI 一致）；
    # global 显式声明无企业 + 国际版域（国际版出站头形态，防风控按缺省判定）。
    if account.realm == "global":
        headers["x-no-enterprise-id"] = "1"
        headers["x-domain"] = "www.workbuddy.ai"
    else:
        if account.enterprise_id:
            headers["x-enterprise-id"] = account.enterprise_id
        else:
            headers["x-no-enterprise-id"] = "1"
        headers["x-no-department-info"] = "1"
    # 用量归属：对齐官方桌面端指纹（上游归因不再出现「网关特征」空值）。
    headers["x-agent-purpose"] = "conversation"
    headers["x-ide-name"] = "WorkBuddy"
    headers["x-ide-type"] = "WorkBuddy"
    headers["x-ide-version"] = WB_CLIENT_VERSION
    headers["x-product"] = "WorkBuddy"
    # 设备风控头：账号级 > 全局兜底；空则不注入（优雅降级）。
    device_token = account.device_token or WB_DEVICE_TOKEN
    if device_token:
        headers["x-device-token"] = device_token
    # 会话头族：X-Conversation-Request-ID 是后台聚合主键（一次 user send 内
    # 所有 tool call/重试/换号复用同值），由 provider 在轮转循环外生成。
    conv_req = conversation_request_id or message_id()
    msg_id = message_id()
    if conversation_id:
        headers["x-conversation-id"] = conversation_id
    headers["x-conversation-request-id"] = conv_req
    headers["x-conversation-message-id"] = msg_id
    headers["x-request-id"] = msg_id
    headers["x-root-request-id"] = conv_req
    headers["x-trace-id"] = conv_req
    headers["x-b3-traceid"] = conv_req
    headers["x-b3-spanid"] = msg_id[:16]
    headers["x-b3-sampled"] = "1"
    return headers


def billing_headers(account: Account) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {account.access_token}",
        "accept": "application/json",
        "content-type": "application/json",
        "x-codebuddy-request": "1",
        "accept-language": "en-US" if account.realm == "global" else "zh-CN",
        "user-agent": f"WorkBuddy/{WB_CLIENT_VERSION}",
    }
    if account.uid:
        headers["x-user-id"] = account.uid
    if account.enterprise_id:
        headers["x-enterprise-id"] = account.enterprise_id
        headers["x-tenant-id"] = account.enterprise_id
    device_token = account.device_token or WB_DEVICE_TOKEN
    if device_token:
        headers["x-device-token"] = device_token
    return headers


def growth_headers(account: Account) -> dict[str, str]:
    """growth 域（连登/旅行/活跃上报）请求头：billing 头 + 显式 X-Domain。

    X-Domain 是上游做域路由的显式声明（值 = 该 realm 的 chat host，写 v3/config 时
    也是同一个值）；billing_headers 自身不带，签到/余额用不上它，但成长中心接口
    在跨域（cn/global 同一套路径）时靠它认域。
    """
    headers = billing_headers(account)
    headers["x-domain"] = _host(account)
    return headers


def refresh_headers(account: Account) -> dict[str, str]:
    """refresh 端点专属头（X-Refresh-Token 只允许出现在这里）。"""
    headers = common_headers(account)
    headers["x-refresh-token"] = account.refresh_token
    if account.enterprise_id:
        headers["x-enterprise-id"] = account.enterprise_id
    headers["x-auth-refresh-source"] = "plugin"
    return headers


# ── 请求发起 ──


def short_rpc_timeout() -> tuple[float, float]:
    """短 RPC（refresh/models/credits）：curl_cffi tuple → TIMEOUT_MS = connect+read。"""
    return (float(WB_TIMEOUT_CONNECT), float(WB_TIMEOUT_TTFB - WB_TIMEOUT_CONNECT))


def chat_request(account: Account, body: bytes, *, conversation_id: str = "",
                 conversation_request_id: str = "") -> crequests.Response:
    """POST /v2/chat/completions，恒 stream=True（上游拒绝非流式）。

    超时 = 连接 WB_TIMEOUT_CONNECT + 空闲掐流（LOW_SPEED：连续无数据
    connect+idle 秒即断）。网络异常抛 curl_cffi RequestsError。
    """
    url = f"{chat_base(account).rstrip('/')}{CHAT_COMPLETIONS_PATH}"
    return crequests.post(
        url,
        headers=chat_headers(account, conversation_id, conversation_request_id),
        data=body,
        stream=True,
        timeout=(float(WB_TIMEOUT_CONNECT), float(WB_TIMEOUT_IDLE)),
        proxies=proxies_for(url),
        impersonate="chrome",
    )


def refresh_request(account: Account) -> dict[str, Any]:
    """POST token/refresh，返回 data 字段 {accessToken, refreshToken, expiresIn, domain}。

    上游统一信封 {code, msg, data}（逐层拆包取 data）：code != 0 或缺
    accessToken 视为失败。失败抛 RequestsError（网络）或 ValueError（需重新登录）。
    """
    url = f"{chat_base(account).rstrip('/')}{REFRESH_PATH}"
    response = crequests.post(
        url,
        headers=refresh_headers(account),
        timeout=short_rpc_timeout(),
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise ValueError(f"refresh status {response.status_code}: {response.text[:120]}")
    envelope = response.json()
    if not isinstance(envelope, dict):
        raise ValueError("refresh_failed: unexpected response shape")
    if envelope.get("code") != 0:
        raise ValueError(
            f"refresh_failed: code={envelope.get('code')} msg={envelope.get('msg')} — re-login required"
        )
    data = envelope.get("data")
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise ValueError("refresh_failed: no accessToken in response — re-login required")
    return data


def fetch_models(account: Account) -> list[dict[str, Any]]:
    """GET /v3/config → 过滤后的模型目录条目列表（非对话模型/已下架剔除）。

    模型目录与定价按 realm 各自独立——global 账号打 workbuddy.ai 域。
    """
    url = f"{chat_base(account).rstrip('/')}{V3_CONFIG_PATH}"
    response = crequests.get(
        url,
        headers={
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "authorization": f"Bearer {account.access_token}",
            "x-user-id": account.uid,
            "x-domain": _host(account),
            "x-product": V3_CONFIG_PRODUCT,
            "user-agent": V3_CONFIG_UA_GLOBAL if account.realm == "global" else V3_CONFIG_UA_CN,
            "x-codebuddy-request": "1",
        },
        timeout=short_rpc_timeout(),
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise ValueError(f"v3/config status {response.status_code}: {response.text[:120]}")
    envelope = response.json()
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        raise ValueError(f"v3/config code={envelope.get('code') if isinstance(envelope, dict) else '?'}")
    data = envelope.get("data") or {}
    entries = [e for e in (data.get("models") or []) if isinstance(e, dict) and str(e.get("id") or "").strip()]
    # 试用横幅模型只出现在 productFeaturesConfig.ModelTrialBanner（不在 data.models）。
    features = data.get("productFeaturesConfig") or {}
    banners = ((features.get("ModelTrialBanner") or {}).get("banners")) or []
    by_id = {str(e["id"]).strip(): e for e in entries}
    for banner in banners:
        if not isinstance(banner, dict):
            continue
        trial_id = str(banner.get("modelId") or "").strip()
        if not trial_id or trial_id in by_id:
            continue
        base = by_id.get(str(banner.get("targetModelId") or "").strip())
        entry = dict(base) if isinstance(base, dict) else {"id": trial_id}
        entry["id"] = trial_id
        entry["credits"] = ""
        entry["tags"] = []
        by_id[trial_id] = entry
    _apply_model_promotions(by_id, data.get("modelPromotions") or [])
    return [e for e in by_id.values() if not is_non_chat_model(e)]


# ── 限时优惠（modelPromotions 的解析与生效判定）──
# /v3/config data.modelPromotions：deepseek 系错峰五折、限时免费等。Credits 字段是
# 牌价（转正后基准倍率），生效价 = 牌价 × factor；「错峰使用」类只有时段文案无
# 机器可读 factor，仅透出标签与说明。

_PROMO_TZ = BEIJING_TZ  # 上游优惠时区恒 Asia/Shanghai


def _promo_clock(hhmm: str) -> int | None:
    """解析 "HH:MM" 为当日分钟数；坏值返回 None。"""
    parts = str(hhmm).split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if hour < 0 or hour > 24 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _promo_active(promo: dict[str, Any], now: datetime) -> bool:
    """评估优惠在 now 是否生效：enabled + validFrom/validUntil（RFC3339，解析
    失败视为无该限制）+ 落在任一 daily 窗口（支持跨午夜 23:00→7:50）。"""
    if not promo.get("enabled"):
        return False
    schedule = promo.get("schedule")
    if isinstance(schedule, dict):
        for key, compare in (("validFrom", lambda at: now < at), ("validUntil", lambda at: now >= at)):
            raw = str(schedule.get(key) or "")
            if raw:
                try:
                    at = datetime.fromisoformat(raw)  # 3.11+ 原生解析 RFC3339 的 Z 后缀
                except ValueError:
                    continue  # 坏时间戳不臆测，按无限制处理
                if compare(at):
                    return False
        daily = schedule.get("daily") or []
        if isinstance(daily, list) and daily:
            current = now.hour * 60 + now.minute
            in_window = False
            for window in daily:
                if not isinstance(window, dict):
                    continue
                start = _promo_clock(str(window.get("start") or ""))
                end = _promo_clock(str(window.get("end") or ""))
                if start is None or end is None:
                    continue
                if start <= end:
                    if start <= current < end:
                        in_window = True
                        break
                elif current >= start or current < end:  # 跨午夜
                    in_window = True
                    break
            if not in_window:
                return False
    return True


def _apply_model_promotions(by_id: dict[str, dict[str, Any]], promos: Any) -> None:
    """把当前生效的优惠挂到目录条目（in-place）：同模型多条命中取 priority 最高
    （glm-5.2 白天 badge-only 与夜间五折靠 priority+daily 双轨切换）。"""
    if not isinstance(promos, list) or not by_id:
        return
    now = datetime.now(_PROMO_TZ)
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for promo in promos:
        if not isinstance(promo, dict) or not _promo_active(promo, now):
            continue
        try:
            priority = int(promo.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        for model_id in promo.get("modelIds") or []:
            if model_id in by_id:
                current = best.get(model_id)
                if current is None or priority > current[0]:
                    best[model_id] = (priority, promo)
    for model_id, (_priority, promo) in best.items():
        entry = by_id[model_id]
        badge = promo.get("badge")
        hover = promo.get("hover")
        discount = promo.get("discount")
        if isinstance(badge, dict) and badge.get("label"):
            entry["promo_label"] = str(badge["label"])
        if isinstance(hover, dict) and hover.get("textZh"):
            entry["promo_note"] = str(hover["textZh"])
        if isinstance(discount, dict):
            factor = discount.get("factor")
            try:
                entry["promo_factor"] = float(factor) if factor is not None else None
            except (TypeError, ValueError):
                entry["promo_factor"] = None
            entry["promo_credits"] = str(discount.get("discountedCredits") or "")


_GEN_TAGS = ("text-to-image", "text-to-video", "image-to-video", "image-to-image")


def is_non_chat_model(entry: dict[str, Any]) -> bool:
    """非对话模型过滤：嵌入/补全/代码专用前缀、
    maxOutputTokens ≤ 256、图片/视频生成类标签——选上去只会报 11102。"""
    model_id = str(entry.get("id") or "").lower()
    if any(model_id.startswith(p) for p in ("nes-", "completion-", "codewise-")):
        return True
    if entry.get("disabled"):
        return True
    try:
        if int(entry.get("maxOutputTokens") or 0) <= 256:
            return True
    except (TypeError, ValueError):
        pass
    tags = entry.get("tags") or []
    for tag in tags:
        if isinstance(tag, str) and tag.lower() in _GEN_TAGS:
            return True
    return False


@dataclass
class CreditsDetail:
    """一次余额查询的完整观测（聚合值 + 包级到期信息）。

    expiring / earliest_* 是「优先消耗快过期积分」的依据：
      - expiring：到期时刻 ≤ now + 窗口 的那部分余额（它是 remain 的子集）
      - earliest_at_ms / earliest_remaining：最近一批到期包的到期时刻与余额
        （无到期包时为 0 / 0）
    """

    remain: float = 0.0
    total: float = 0.0
    expiring: float = 0.0
    earliest_at_ms: int = 0
    earliest_remaining: float = 0.0


def _package_end_ms(raw: Any) -> int:
    """套餐 `CycleEndTime`（"YYYY-MM-DD HH:MM:SS"，UTC+8）→ 绝对到期毫秒。

    解析不出返回 0（= 该包不参与快过期判定）：上游偶发回空串/脏格式，不臆测。
    """
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        at = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    # 上游时间戳恒按 UTC+8 解释（与签到/余额查询的账期口径一致），本机时区不参与
    return int(at.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)


def fetch_credits_detail(account: Account, *, expiring_within_ms: int = 0) -> CreditsDetail:
    """POST get-user-resource（企业号走企业额度端点）→ 聚合余额 + 快过期分桶。

    个人资源包聚合（ProductCode p_tcaca）；remain 负值钳 0；上游缺 size 的
    包按 remain 兜底。billing 路径按 realm 分流：
    global 首选无 /v2 前缀（404 回落 /v2），CN 固定 /v2。

    `expiring_within_ms` > 0 时，对「到期时刻 ≤ now+窗口」的包单独累加余额；
    0 = 不分桶（expiring 恒 0，与只有聚合值的调用方行为一致）。
    """
    if account.enterprise_id:
        return _fetch_enterprise_detail(account, expiring_within_ms=expiring_within_ms)
    tz = BEIJING_TZ
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "PackageEndTimeRangeEnd": (datetime.now(tz) + timedelta(days=365 * 101)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }
    paths = (
        [USER_RESOURCE_PATH, USER_RESOURCE_PATH_V2]
        if account.realm == "global"
        else [USER_RESOURCE_PATH_V2]
    )
    response = None
    for path in paths:
        url = f"{billing_base(account).rstrip('/')}{path}"
        response = crequests.post(
            url,
            headers=billing_headers(account),
            json=body,
            timeout=short_rpc_timeout(),
            proxies=proxies_for(url),
            impersonate="chrome",
        )
        if response.status_code == 404:
            continue  # global 新旧路径分叉：404 换下一候选
        break
    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else 0
        text = response.text[:120] if response is not None else ""
        raise ValueError(f"user-resource status {status}: {text}")
    envelope = response.json()
    # 上游统一信封 {code, msg, data}（逐层拆包）：真正的
    # {Response:{Data:{Accounts}}} 负载在 data 字段里。
    if not isinstance(envelope, dict):
        raise ValueError("user-resource parse: unexpected envelope")
    if envelope.get("code") != 0:
        raise ValueError(f"user-resource code={envelope.get('code')} msg={envelope.get('msg')}")
    payload = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    accounts = ((((payload or {}).get("Response") or {}).get("Data") or {}).get("Accounts")) or []
    now_ms = int(time.time() * 1000)
    detail = CreditsDetail()
    for pkg in accounts:
        if not isinstance(pkg, dict):
            continue
        cycle_remain = _num(pkg.get("CycleCapacityRemain"))
        cycle_size = _num(pkg.get("CycleCapacitySize"))
        if cycle_remain is not None:
            r = max(cycle_remain, 0.0)
            size = cycle_size if (cycle_size is not None and cycle_size >= r) else r
        else:
            plain_remain = _num(pkg.get("CapacityRemain"))
            plain_size = _num(pkg.get("CapacitySize"))
            if plain_remain is None:
                continue
            r = max(plain_remain, 0.0)
            size = plain_size if (plain_size is not None and plain_size >= r) else r
        detail.remain += r
        detail.total += size
        # 到期分桶：只在包里还有余额时才有意义（空包到期不影响选号）
        if r <= 0:
            continue
        end_ms = _package_end_ms(pkg.get("CycleEndTime"))
        if end_ms <= now_ms:
            continue
        if detail.earliest_at_ms == 0 or end_ms < detail.earliest_at_ms:
            detail.earliest_at_ms = end_ms
            detail.earliest_remaining = r
        elif end_ms == detail.earliest_at_ms:
            detail.earliest_remaining += r
        if expiring_within_ms > 0 and end_ms <= now_ms + expiring_within_ms:
            detail.expiring += r
    # 快过期是 remain 的子集（上游对账延迟下可能算超，钳住）
    detail.expiring = min(detail.expiring, detail.remain)
    detail.earliest_remaining = min(detail.earliest_remaining, detail.remain)
    return detail


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fetch_enterprise_detail(account: Account, *, expiring_within_ms: int = 0) -> CreditsDetail:
    """企业额度：POST get-enterprise-user-usage（空体）→ remain/total（+周期到期分桶）。

    字段口径（兼容上游两套命名）：
      - `limitNum` / `limit_num`：分配给该账号的周期额度；**负值 = 不限量**
      - `credit`（已用积分，浮点）优先，缺省回落 `used_num`
      - `cycleEndTime` 优先、缺省回落 `cycleResetTime`：周期到期时刻（快过期分桶同个人口径）

    已用量四舍五入到整数（向下取整会低报剩余额度，与"还剩多少"的展示意图相悖）。
    """
    url = f"{billing_base(account).rstrip('/')}{ENTERPRISE_USAGE_PATH}"
    try:
        response = crequests.post(
            url, headers=billing_headers(account), json={},
            timeout=short_rpc_timeout(), proxies=proxies_for(url), impersonate="chrome",
        )
    except crequests.RequestsError as exc:
        raise ValueError(f"enterprise-usage network: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise ValueError(f"enterprise-usage status {response.status_code}: {response.text[:120]}")
    envelope = response.json()
    if not isinstance(envelope, dict):
        raise ValueError("enterprise-usage parse: unexpected envelope")
    if envelope.get("code") != 0:
        raise ValueError(
            f"enterprise-usage code={envelope.get('code')} msg={envelope.get('msg')}"
        )
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    limit = _num(data.get("limitNum"))
    if limit is None:
        limit = _num(data.get("limit_num"))
    used = _num(data.get("credit"))
    if used is None or used == 0:
        used = _num(data.get("used_num"))
    if limit is None:
        raise ValueError("enterprise-usage parse: no limit field")
    if limit < 0:
        return CreditsDetail(
            remain=float(ENTERPRISE_UNLIMITED_REMAIN), total=ENTERPRISE_UNLIMITED_TOTAL
        )
    detail = CreditsDetail(
        remain=max(limit - round(used or 0.0), 0.0),
        total=limit,
    )
    now_ms = int(time.time() * 1000)
    end_ms = _package_end_ms(data.get("cycleEndTime")) or _package_end_ms(data.get("cycleResetTime"))
    if end_ms > now_ms and detail.remain > 0:
        detail.earliest_at_ms = end_ms
        detail.earliest_remaining = detail.remain
        if expiring_within_ms > 0 and end_ms <= now_ms + expiring_within_ms:
            detail.expiring = detail.remain
    return detail


def refresh_expires_ms(expires_in: Any) -> int:
    """refresh 响应 expiresIn（秒）→ 绝对过期毫秒。

    实测恒 5184000（60d）；缺省/脏值（>10y，防「永不刷新」漂移）返回 0 = 保留旧值。
    """
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        return 0
    if expires_in >= 10 * 365 * 86400:
        return 0
    return int(time.time() * 1000 + expires_in * 1000)
