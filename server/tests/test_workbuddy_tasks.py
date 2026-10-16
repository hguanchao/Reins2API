"""workbuddy 定时任务测试：growth 域契约 + 四类任务 + 排程器 + schedule 配置。

契约逆向自 wb2api-panel（见 providers/workbuddy/growth.py 的模块注释）；这里锁的是
「请求打给谁、带什么、错误怎么判、预期结论怎么静默」——上游活动期的字段形状变了
只该改 growth.py，不该让任务层跟着变。

运行：cd server && uv run python -m pytest tests/test_workbuddy_tasks.py -q
（无 pytest 时：uv run python tests/test_workbuddy_tasks.py 直接执行内建 main）
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config as app_config
from core.settings import WB_SCHEDULE_MINUTE
from providers.workbuddy import checkin, growth
from providers.workbuddy.provider import WorkBuddyProvider
from providers.workbuddy.scheduler import (
    TASKS,
    TaskBusy,
    TaskError,
    WorkBuddyScheduler,
    next_slot,
)
from providers.workbuddy.store import Account, WorkBuddyStore

# ── HTTP 桩：替换 growth.crequests ──


class _Resp:
    def __init__(self, status: int, payload=None, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _StubHttp:
    """按「路径后缀」匹配响应的假 curl_cffi：记录每次请求的 method/url/body/headers。

    值可以是 _Resp、_Resp 列表（按序弹出，模拟多账号多次调用）或可调用对象
    （收 (url, body) 返回 _Resp）。
    """

    RequestsError = RuntimeError

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[dict[str, object]] = []

    def _pick(self, url: str, body, headers) -> _Resp:
        self.calls.append({"url": url, "body": body, "headers": headers})
        for suffix, value in self.routes.items():
            if url.endswith(suffix):
                if callable(value):
                    return value(url, body)
                if isinstance(value, list):
                    if not value:
                        raise AssertionError(f"多余的上游调用: {url}")
                    return value.pop(0)
                return value  # type: ignore[return-value]
        raise AssertionError(f"未桩的上游调用: {url}")

    def get(self, url, **kwargs):
        return self._pick(url, None, kwargs.get("headers") or {})

    def post(self, url, **kwargs):
        return self._pick(url, kwargs.get("json"), kwargs.get("headers") or {})


@contextmanager
def _stub_http(routes: dict[str, object]):
    stub = _StubHttp(routes)
    original = growth.crequests
    growth.crequests = stub  # type: ignore[assignment]
    try:
        yield stub
    finally:
        growth.crequests = original  # type: ignore[assignment]


@contextmanager
def _stub_growth(**funcs):
    """替换 growth 里的任务级函数（provider 编排测试用，不必经 HTTP）。"""
    original = {name: getattr(growth, name) for name in funcs}
    for name, value in funcs.items():
        setattr(growth, name, value)
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(growth, name, value)


@contextmanager
def _temp_config(path: Path):
    """把运行时配置指向临时文件（绝不碰仓库里的 server/config.json）。"""
    old_path, old_cache = app_config.CONFIG_PATH, app_config._cache
    app_config.CONFIG_PATH = path
    app_config._cache = None
    try:
        yield
    finally:
        app_config.CONFIG_PATH = old_path
        app_config._cache = old_cache


@contextmanager
def _fast_stagger():
    """账号间限速与领养前置等待置 0（否则每个号白等 800ms/1.05s）。"""
    import providers.workbuddy.provider as provider_module
    from core import settings

    saved = (
        settings.WB_CHECKIN_STAGGER_MS, provider_module.WB_CHECKIN_STAGGER_MS,
        provider_module.WB_ADOPT_REPORT_GAP_MS,
    )
    settings.WB_CHECKIN_STAGGER_MS = 0
    provider_module.WB_CHECKIN_STAGGER_MS = 0
    provider_module.WB_ADOPT_REPORT_GAP_MS = 0
    try:
        yield
    finally:
        (
            settings.WB_CHECKIN_STAGGER_MS, provider_module.WB_CHECKIN_STAGGER_MS,
            provider_module.WB_ADOPT_REPORT_GAP_MS,
        ) = saved


@contextmanager
def _provider(accounts: list[Account] | None = None):
    """临时库 + provider（凭证齐备的 CN 号一个，可按需追加）。

    token 刷新的上游调用也桩掉：所有任务在动手前都会 `_maybe_refresh`（token_expires_ms
    为 0 = 未知，视为需刷新），不桩就是每个用例真打一次 copilot.tencent.com。
    """
    import providers.workbuddy.upstream as wb_upstream

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        original_refresh = wb_upstream.refresh_request
        wb_upstream.refresh_request = lambda account: {
            "accessToken": "at-fresh", "refreshToken": "rt-fresh", "expiresIn": 3600
        }
        try:
            for account in accounts or []:
                store.upsert_account(account)
            provider = WorkBuddyProvider(store=store)
            provider._refresh_credits_async = lambda uid: None  # 别在测试里起线程打上游
            yield provider, store
        finally:
            wb_upstream.refresh_request = original_refresh
            store.close()


def _cn_account(uid: str = "u_1", **overrides) -> Account:
    base: dict = {"uid": uid, "realm": "cn", "access_token": "at", "refresh_token": "rt"}
    base.update(overrides)
    return Account(**base)


# ── growth.py：信封与错误映射 ──


def test_growth_envelope_and_error_mapping():
    account = _cn_account()
    # 2xx + code=0 → 返回 data
    with _stub_http({"/activity/growth/streak": _Resp(200, {"code": 0, "data": {"streak": {"days": 3}}})}):
        assert growth.streak_days(account) == 3

    # 业务码非 0（HTTP 200）也是失败，且带上 code
    with _stub_http({"/activity/growth/streak": _Resp(200, {"code": 12000, "msg": "internal"})}):
        try:
            growth.streak_days(account)
            raise AssertionError("非零业务码应抛 GrowthError")
        except growth.GrowthError as exc:
            assert exc.code == 12000 and exc.status == 200

    # HTTP 非 2xx：403 是「档位未解锁」的预期结论
    with _stub_http({"/activity/growth/redeem": _Resp(403, {"code": 1, "msg": "连续登录天数不足"})}):
        try:
            growth.redeem_tier(account, "7d")
            raise AssertionError("403 应抛 GrowthError")
        except growth.GrowthError as exc:
            assert growth.is_tier_locked(exc) and not growth.is_task_incomplete(exc)

    # 400 + first_buddy 文案 = 领养门槛未达（当日不再重试的判据）
    with _stub_http({"/activity/growth/buddy/first": _Resp(
        400, {"code": 400, "msg": "first_buddy task not completed yet"}
    )}):
        try:
            growth.buddy_first(account)
            raise AssertionError("门槛未达应抛 GrowthError")
        except growth.GrowthError as exc:
            assert growth.is_task_incomplete(exc) and not growth.is_tier_locked(exc)

    # 非 JSON 负载（截断 HTML / 网关页）算失败，不当成功
    with _stub_http({"/activity/growth/streak": _Resp(200, text="<html>bad gateway")}):
        try:
            growth.streak_days(account)
            raise AssertionError("不可解析负载应抛 GrowthError")
        except growth.GrowthError as exc:
            assert "JSON" in str(exc)

    # 缺 streak.days → 0（自检就是靠 0 发现「上报被静默丢弃」）
    with _stub_http({"/activity/growth/streak": _Resp(200, {"code": 0, "data": {}})}):
        assert growth.streak_days(account) == 0


def test_growth_streak_parses_tiers_and_statuses():
    payload = {"code": 0, "data": {
        "streak": {"days": 14},
        "makeup_cards": {"balance": 2},
        "redemption_status": {
            "tier_7d_status": "claimed",
            "tier_14d_status": "unlocked",
            "tier_28d_status": "locked",
            "tiers": [
                {"tier": "7d", "days": 7, "credit": 100, "energy": 0, "cards": 1, "chances": 1},
                {"tier": "14d", "days": 14, "credit": 300, "energy": 5, "cards": 0, "chances": 2},
                {"tier": "28d", "days": 28, "credit": 800, "chances": 3},
            ],
        },
    }}
    with _stub_http({"/activity/growth/streak": _Resp(200, payload)}):
        full = growth.streak_full(_cn_account())

    assert full.days == 14 and full.makeup_cards == 2
    # 只有 14d 可兑：claimed 与 locked 都不该再打兑换接口
    assert [tier.tier for tier in full.redeemable_tiers()] == ["14d"]
    assert "抽奖×2" in full.redeemable_tiers()[0].reward_text()


def test_growth_travel_and_buddy_parsing():
    account = _cn_account()
    with _stub_http({"/activity/growth/buddy/info": _Resp(
        200, {"code": 0, "data": {"buddy": None}}
    )}):
        assert growth.buddy_info(account) is None
    with _stub_http({"/activity/growth/buddy/info": _Resp(
        200, {"code": 0, "data": {"buddy": {"id": 7, "name": "档案喵"}}}
    )}):
        buddy = growth.buddy_info(account)
    assert buddy is not None and buddy.id == 7 and buddy.name == "档案喵"

    with _stub_http({"/activity/growth/buddy/travel/status": _Resp(
        200, {"code": 0, "data": {"state": "arrived", "record_id": 42, "reward_credit": 9}}
    )}):
        state = growth.travel_status(account)
    assert state.state == growth.TRAVEL_ARRIVED and state.record_id == 42
    assert state.reward_credit == 9.0 and state.daily_limit_reached is False

    # 派出带固定地点、领奖带 record_id（缺 record_id 上游不认，任务层会跳过）
    with _stub_http({
        "/travel/depart": _Resp(200, {"code": 0, "data": {}}),
        "/travel/claim": _Resp(200, {"code": 0, "data": {"reward_credit": 12}}),
    }) as stub:
        growth.travel_depart(account)
        assert growth.travel_claim(account, 42) == 12.0
    assert stub.calls[0]["body"] == {"location_id": growth.TRAVEL_LOCATION_ID}
    assert stub.calls[1]["body"] == {"record_id": 42}


def test_growth_report_payload_shape():
    """活跃上报：billing 域 /v2/report、数组包裹、userId 必填、全字段事件。"""
    account = _cn_account(uid="u_report")
    with _stub_http({"/v2/report": _Resp(200, {"code": 0, "msg": "ok", "data": None})}) as stub:
        growth.report_chat_activity(account, "wb2api-123", request_id="req-1")

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"] == "https://www.codebuddy.cn/v2/report"  # billing 域，不是 chat 域
    events = call["body"]
    assert isinstance(events, list) and len(events) == 1
    event = events[0]
    assert event["eventCode"] == "chat_request_send"
    assert event["userId"] == "u_report"  # 缺它上游 200 但静默丢弃
    assert event["conversationId"] == "wb2api-123" and event["requestId"] == "req-1"
    assert event["requestModelId"] == "deepseek-v4-flash"
    assert event["agentName"] == "default" and event["mode"] == "craft"
    headers = {str(k).lower(): v for k, v in (call["headers"] or {}).items()}
    assert headers.get("authorization") == "Bearer at"
    assert headers.get("x-domain") == "copilot.tencent.com"  # growth 域显式声明
    assert headers.get("x-user-id") == "u_report"


# ── 连登管家 ──


def test_streak_bonus_redeems_and_draws():
    full = growth.StreakFull(days=14, tier_statuses={"7d": "claimed", "14d": "unlocked", "28d": "locked"})
    full.tiers = [
        growth.StreakTier(tier="7d", status="claimed"),
        growth.StreakTier(tier="14d", status="unlocked", credit=300, chances=2),
        growth.StreakTier(tier="28d", status="locked"),
    ]
    redeemed: list[str] = []
    draws: list[int] = []

    with _provider([_cn_account()]) as (provider, _store), _stub_growth(
        streak_full=lambda account: full,
        redeem_tier=lambda account, tier: redeemed.append(tier),
        lottery_chances=lambda account: 2,
        lottery_draw=lambda account: draws.append(len(draws) + 1) or {"prize": "5 积分"},
    ):
        result = provider.streak_bonus("u_1")

    assert redeemed == ["14d"]  # claimed / locked 都不兑
    assert draws == [1, 2] and result["drawn"] == 2
    assert result["state"] == "claimed" and "兑换 14d 档" in result["message"]
    assert result["streak_days"] == 14
    assert result["prizes"] == ['{"prize":"5 积分"}'] * 2


def test_streak_bonus_skips_inapplicable_and_survives_draw_failure():
    calls: list[str] = []

    def _draw(account):
        calls.append("draw")
        if len(calls) > 1:
            raise growth.GrowthError("HTTP 500: boom", status=500)
        return {"prize": "1 积分"}

    full = growth.StreakFull(days=7)
    full.tiers = [growth.StreakTier(tier="7d", status="unlocked")]
    with _provider([
        _cn_account("u_cn"),
        _cn_account("u_glob", realm="global"),
        _cn_account("u_ent", enterprise_id="ent-1"),
    ]) as (provider, _store), _stub_growth(
        streak_full=lambda account: full,
        redeem_tier=lambda account, tier: None,
        lottery_chances=lambda account: 3,
        lottery_draw=_draw,
    ):
        glob = provider.streak_bonus("u_glob")
        ent = provider.streak_bonus("u_ent")
        cn = provider.streak_bonus("u_cn")

    assert glob["state"] == "skipped" and glob["message"] == "global 域无档位兑换/旅行（CN 专属）"
    assert ent["state"] == "skipped" and ent["message"] == "企业账号无成长中心"
    assert calls == ["draw", "draw"]  # 第二抽失败即停，不硬撑剩余次数
    assert cn["drawn"] == 1 and cn["state"] == "claimed"


def test_streak_bonus_all_counts_and_tolerates_errors():
    full = growth.StreakFull(days=7)
    full.tiers = [growth.StreakTier(tier="7d", status="unlocked")]

    def _chances(account):
        if account.uid == "u_bad":
            raise growth.GrowthError("HTTP 500: boom", status=500)
        return 2

    with _provider([_cn_account("u_ok"), _cn_account("u_bad")]) as (provider, _store), \
            _fast_stagger(), _stub_growth(
                streak_full=lambda account: full,
                redeem_tier=lambda account, tier: None,
                lottery_chances=_chances,
                lottery_draw=lambda account: {"prize": "x"},
            ):
        summary = provider.streak_bonus_all()

    assert summary["total"] == 2 and summary["redeemed"] == 2 and summary["drawn"] == 2
    # 抽奖次数查不到不算整号失败（兑换已成功），但要留下痕迹
    assert summary["failed"] == 0 and "抽奖" in summary["summary"]


# ── 活跃上报 ──


def test_activity_report_and_streak_self_check():
    reported: list[str] = []
    with _provider([_cn_account()]) as (provider, _store):
        with _stub_growth(
            report_chat_activity=lambda account, cid, request_id="", **kw: reported.append(cid),
            streak_days=lambda account: 5,
        ):
            ok = provider.activity_report("u_1")
        # 上报 200 但 streak.days=0 → 可疑（事件被静默丢弃）
        with _stub_growth(
            report_chat_activity=lambda account, cid, request_id="", **kw: None,
            streak_days=lambda account: 0,
        ):
            dropped = provider.activity_report("u_1")
        # 回读失败不改写成失败，也不算可疑（growth 端点在部分账号上不适用）
        def _boom(account):
            raise growth.GrowthError("HTTP 500", status=500)

        with _stub_growth(
            report_chat_activity=lambda account, cid, request_id="", **kw: None,
            streak_days=_boom,
        ):
            unreadable = provider.activity_report("u_1")

    assert reported and reported[0].startswith("wb2api-")
    assert ok["state"] == "reported" and ok["streak_days"] == 5 and ok["suspect"] is False
    assert dropped["suspect"] is True and dropped["streak_days"] == 0
    assert unreadable["state"] == "reported" and unreadable["suspect"] is False


def test_activity_report_enterprise_skipped_global_allowed():
    seen: list[str] = []
    with _provider([
        _cn_account("u_ent", enterprise_id="ent"),
        _cn_account("u_glob", realm="global"),
    ]) as (provider, _store), _stub_growth(
        report_chat_activity=lambda account, cid, request_id="", **kw: seen.append(account.uid),
        streak_days=lambda account: 3,
    ):
        ent = provider.activity_report("u_ent")
        glob = provider.activity_report("u_glob")

    assert ent["state"] == "skipped" and "企业" in ent["message"]
    # 国际版 /v2/report 实测可用：global 要报，否则它的连登永远点不亮
    assert glob["state"] == "reported" and seen == ["u_glob"]


# ── 猫猫旅行 ──


def test_travel_adopt_flow_and_daily_marker():
    reported: list[str] = []
    agreed: list[int] = []
    first: list[int] = []

    def _first(account):
        first.append(1)
        raise growth.GrowthError(
            "HTTP 400: first_buddy task not completed yet", status=400
        )

    with _provider([_cn_account()]) as (provider, _store), _fast_stagger(), _stub_growth(
        buddy_info=lambda account: None,
        report_chat_activity=lambda account, cid, request_id="", **kw: reported.append(cid),
        buddy_agreement=lambda account: agreed.append(1),
        buddy_first=_first,
    ):
        first_run = provider.travel("u_1")
        second_run = provider.travel("u_1")  # 当日已判定门槛未达：不该再打上游

    assert first_run["state"] == "adopt-blocked" and "门槛" in first_run["message"]
    assert len(reported) == 1 and reported[0].startswith("wb2api-adopt-")
    assert agreed == [1] and first == [1]
    assert second_run["state"] == "adopt-blocked" and len(first) == 1


def test_travel_adopt_success_path():
    with _provider([_cn_account()]) as (provider, _store), _stub_growth(
        buddy_info=lambda account: None,
        report_chat_activity=lambda account, cid, request_id="", **kw: None,
        buddy_agreement=lambda account: None,
        buddy_first=lambda account: None,
    ):
        result = provider.travel("u_1")
    assert result["state"] == "adopted" and result["credit"] == 300.0


def test_travel_state_machine():
    def _buddy(account):
        return growth.Buddy(id=1, name="喵")

    cases = [
        # (旅行状态, 期望结论, 是否该派出, 是否该领奖)
        (growth.TravelState(state="idle"), "departed", True, False),
        (growth.TravelState(state="idle", daily_limit_reached=True), "idle", False, False),
        (growth.TravelState(state="arrived", record_id=9), "claimed", False, True),
        (growth.TravelState(state="arrived"), "arrived", False, False),  # 缺 record_id
        (growth.TravelState(state="traveling"), "traveling", False, False),
        (growth.TravelState(state="weird"), "unknown", False, False),
    ]
    for state, expected, departed, claimed in cases:
        departures: list[int] = []
        claims: list[int] = []
        with _provider([_cn_account()]) as (provider, _store), _stub_growth(
            buddy_info=_buddy,
            travel_status=lambda account, _s=state: _s,
            travel_depart=lambda account, location_id=0: departures.append(location_id),
            travel_claim=lambda account, record_id: claims.append(record_id) or 9.0,
        ):
            result = provider.travel("u_1")
        assert result["state"] == expected, f"{state.state} → {result}"
        assert bool(departures) is departed and bool(claims) is claimed
        if claimed:
            assert result["credit"] == 9.0


def test_travel_normalizes_growth_errors():
    def _boom(account):
        raise growth.GrowthError("HTTP 401: unauthorized", status=401)

    with _provider([_cn_account()]) as (provider, _store), _stub_growth(buddy_info=_boom):
        result = provider.travel("u_1")
    assert result["state"] == "error" and "401" in result["message"]


# ── token 保活 ──


def test_keepalive_refreshes_and_disables_on_repeated_session_death():
    def _dead(account):
        raise ValueError(
            "refresh_failed: code=12153 msg=Offline user session not found — re-login required"
        )

    with _provider([_cn_account()]) as (provider, store):
        calls: list[str] = []

        def _ok(account):
            calls.append(account.uid)
            return {"accessToken": "at2", "refreshToken": "rt2", "expiresIn": 3600}

        import providers.workbuddy.upstream as wb_upstream

        original = wb_upstream.refresh_request
        wb_upstream.refresh_request = _ok
        try:
            first = provider.keepalive("u_1")
        finally:
            wb_upstream.refresh_request = original
        assert first["state"] == "refreshed" and calls == ["u_1"]
        assert store.get_account("u_1").access_token == "at2"

        # 网络类失败不计数（一次断网不该连杀整池号）
        def _network(account):
            raise ValueError("refresh status 502: bad gateway")

        wb_upstream.refresh_request = _network
        try:
            net = provider.keepalive("u_1")
        finally:
            wb_upstream.refresh_request = original
        assert net["state"] == "failed" and store.get_account("u_1").consecutive_failures == 0

        # session 死亡：连续 3 次才禁用，中途一次成功即清零复活
        wb_upstream.refresh_request = _dead
        try:
            states = [provider.keepalive("u_1")["state"] for _ in range(3)]
        finally:
            wb_upstream.refresh_request = original
        assert states[:2] == ["failed", "failed"] and states[2] == "disabled"
        account = store.get_account("u_1")
        assert account.status == "disabled" and "session invalid" in account.status_reason
        # 已禁用的号不再续期（终态，不再打上游）
        assert provider.keepalive("u_1")["state"] == "skipped"

    with _provider([_cn_account("u_2")]) as (provider, store):
        import providers.workbuddy.upstream as wb_upstream

        original = wb_upstream.refresh_request
        wb_upstream.refresh_request = _dead
        try:
            provider.keepalive("u_2")
            provider.keepalive("u_2")
        finally:
            wb_upstream.refresh_request = original
        assert store.get_account("u_2").consecutive_failures == 2
        wb_upstream.refresh_request = lambda account: {
            "accessToken": "at3", "refreshToken": "rt3", "expiresIn": 3600
        }
        try:
            assert provider.keepalive("u_2")["state"] == "refreshed"
        finally:
            wb_upstream.refresh_request = original
        assert store.get_account("u_2").consecutive_failures == 0  # 复活路径


# ── 签到收尾：余额解冻 + 连登管家 ──


def test_checkin_all_balance_revive_and_streak_tail():
    """签到收尾：查余额 → 余额回血即解冻硬冷却 → 末尾跑连登管家。"""
    import providers.workbuddy.upstream as wb_upstream

    with _provider([_cn_account()]) as (provider, store):
        store.update_runtime(
            "u_1", cooldown_until_ms=int(time.time() * 1000) + 3600_000, cooldown_kind="hard"
        )
        original_credits = wb_upstream.fetch_credits_detail
        wb_upstream.fetch_credits_detail = lambda account, **kw: wb_upstream.CreditsDetail(
                remain=500.0, total=1000.0
            )
        try:
            with _stub_ch_checkin(), _stub_growth(
                streak_full=lambda account: growth.StreakFull(days=8),
                redeem_tier=lambda account, tier: None,
                lottery_chances=lambda account: 0,
                lottery_draw=lambda account: None,
            ):
                summary = provider.checkin_all()
        finally:
            wb_upstream.fetch_credits_detail = original_credits

        account = store.get_account("u_1")
        assert summary["claimed"] == 1
        assert summary["revived"] == 1
        assert account.cooldown_until_ms == 0 and account.cooldown_kind == ""  # 回血解冻
        assert account.credits_remain == 500.0
        assert summary["streak"]["total"] == 1  # 末尾跑了连登管家
        assert "抽奖 0 次" in summary["summary"]  # 连登结论拼进了整轮汇总


def test_checkin_all_keeps_hard_cooldown_when_credits_still_zero():
    """余额仍为 0 时不该解冻（否则下一轮又去撞 402）。"""
    import providers.workbuddy.upstream as wb_upstream

    with _provider([_cn_account()]) as (provider, store):
        until = int(time.time() * 1000) + 3600_000
        store.update_runtime("u_1", cooldown_until_ms=until, cooldown_kind="hard")
        original_credits = wb_upstream.fetch_credits_detail
        wb_upstream.fetch_credits_detail = lambda account, **kw: wb_upstream.CreditsDetail()
        try:
            with _stub_ch_checkin():
                summary = provider.checkin_all(with_streak=False)
        finally:
            wb_upstream.fetch_credits_detail = original_credits

        account = store.get_account("u_1")
        assert summary["total"] == 1
        assert account.cooldown_until_ms == until and account.cooldown_kind == "hard"


@contextmanager
def _stub_ch_checkin():
    """签到两步走：状态说未签 → 领取成功。"""
    stub = _StubHttp({
        checkin.STATUS_PATH: [
            _Resp(200, {"active": True, "today_checked_in": False}),
            _Resp(200, {"streak_days": 8, "total_credits": 520}),
        ],
        checkin.CLAIM_PATH: _Resp(200, {"credit": 20}),
    })
    original = checkin._call
    original_http = growth.crequests

    def _call(account, path, *, retries):
        # checkin._call 的契约是 (status, body) 二元组，不是 Response 对象
        response = stub._pick(path, None, {})
        return response.status_code, response.json()

    checkin._call = _call
    growth.crequests = stub  # type: ignore[assignment]
    try:
        yield
    finally:
        checkin._call = original
        growth.crequests = original_http  # type: ignore[assignment]


# ── 排程器 ──


def test_next_slot_uses_cron_and_tolerates_bad_expressions():
    """排程器这一层只关心「cron → 下一个时刻」：非法与永不触发都返回 None（不抛）。"""
    from datetime import datetime

    from providers.common import BEIJING_TZ

    now = datetime(2026, 10, 16, 20, 0, tzinfo=BEIJING_TZ)  # 周五
    assert next_slot(now, "7 9,21 * * *") == datetime(2026, 10, 16, 21, 7, tzinfo=BEIJING_TZ)
    assert next_slot(now, "7 10 * * *") == datetime(2026, 10, 17, 10, 7, tzinfo=BEIJING_TZ)
    # 空 / 非法 / 永不触发：都不是异常，只是"这个任务现在没有下次触发"
    assert next_slot(now, "") is None
    assert next_slot(now, "9,21") is None
    assert next_slot(now, "0 0 30 2 *") is None


class _FakeProvider:
    """排程器只需四个批量方法：桩掉它们，测排程本身。"""

    def __init__(self):
        self.calls: list[str] = []
        self.gate = None  # 非 None 时 body 会等它（模拟慢任务/并发）

    def _body(self, kind: str):
        if self.gate is not None:
            self.gate.wait(5)
        self.calls.append(kind)
        return {"total": 1, "summary": f"{kind} 完成"}

    def checkin_all(self, **kwargs):
        return self._body("checkin")

    def activity_report_all(self, **kwargs):
        return self._body("activity")

    def travel_all(self, **kwargs):
        return self._body("travel")

    def keepalive_all(self, **kwargs):
        return self._body("keepalive")


def test_scheduler_snapshot_follows_config():
    with tempfile.TemporaryDirectory() as tmp, _temp_config(Path(tmp) / "config.json"):
        provider = _FakeProvider()
        scheduler = WorkBuddyScheduler(provider)
        snap = scheduler.snapshot()
        by_kind = {task["kind"]: task for task in snap["tasks"]}

        assert list(by_kind) == list(TASKS)
        assert snap["timezone"] == "UTC+8"
        assert by_kind["checkin"]["cron"] == f"{WB_SCHEDULE_MINUTE} 9,21 * * *"
        assert by_kind["checkin"]["enabled"]
        assert by_kind["activity"]["cron"] == f"{WB_SCHEDULE_MINUTE} 10 * * *"
        assert by_kind["travel"]["cron"] == f"{WB_SCHEDULE_MINUTE} 9,21 * * *"
        assert by_kind["keepalive"]["cron"] == f"{WB_SCHEDULE_MINUTE} 22 * * *"
        assert by_kind["checkin"]["next_ms"] > snap["now_ms"]
        # 人话说明随快照下发（界面直接显示，不必在前端再实现一遍 cron 翻译）
        assert "每天" in by_kind["checkin"]["describe"]

        # 关掉签到：下次触发清零，重启保留（落盘）
        app_config.update_schedule({"checkin": {"enabled": False}})
        off = {task["kind"]: task for task in scheduler.snapshot()["tasks"]}
        assert off["checkin"]["enabled"] is False and off["checkin"]["next_ms"] == 0

        app_config._cache = None  # 模拟进程重启：从文件重读
        reloaded = {task["kind"]: task for task in scheduler.snapshot()["tasks"]}
        assert reloaded["checkin"]["enabled"] is False


def test_scheduler_run_records_last_and_rejects_concurrent():
    with tempfile.TemporaryDirectory() as tmp, _temp_config(Path(tmp) / "config.json"):
        provider = _FakeProvider()
        scheduler = WorkBuddyScheduler(provider)

        summary = scheduler.run("travel", source="manual")
        assert summary["summary"] == "travel 完成"
        snap = {task["kind"]: task for task in scheduler.snapshot()["tasks"]}
        assert snap["travel"]["last"]["ok"] is True
        assert snap["travel"]["last"]["text"] == "travel 完成"
        assert snap["travel"]["running"] is False

        # 未知任务 / 同名并发
        try:
            scheduler.run("nope")
            raise AssertionError("未知任务应抛 ValueError")
        except ValueError:
            pass
        import threading

        provider.gate = threading.Event()
        thread = threading.Thread(target=scheduler.run, args=("checkin",))
        thread.start()
        time.sleep(0.05)
        try:
            scheduler.run("checkin")
            raise AssertionError("同名并发应抛 TaskBusy")
        except TaskBusy as exc:
            assert "运行中" in str(exc)
        provider.gate.set()
        thread.join(5)
        assert set(provider.calls) == {"travel", "checkin"}

        # 任务体抛错：不得静默——记进 last 后以 TaskError 上抛给调用方
        def _boom(**kwargs):
            raise RuntimeError("上游炸了")

        provider.checkin_all = _boom
        try:
            scheduler.run("checkin")
            raise AssertionError("任务体异常应上抛给调用方")
        except TaskError as exc:
            assert "上游炸了" in str(exc)
        snap = {task["kind"]: task for task in scheduler.snapshot()["tasks"]}
        assert snap["checkin"]["last"]["ok"] is False and "上游炸了" in snap["checkin"]["last"]["error"]


# ── schedule 配置（core/config.py）──


def test_schedule_config_defaults_migration_and_validation():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        with _temp_config(path):
            schedule = app_config.schedule()
            assert set(schedule) == set(TASKS)
            assert schedule["checkin"] == {"enabled": True, "cron": f"{WB_SCHEDULE_MINUTE} 9,21 * * *"}

            # 老配置（hours 形态）按 WB_SCHEDULE_MINUTE 迁成 cron，且不再保留 hours 字段
            schedule = app_config.update_schedule({"checkin": {"hours": [8, 20]}})
            assert schedule["checkin"]["cron"] == f"{WB_SCHEDULE_MINUTE} 8,20 * * *"
            assert "hours" not in schedule["checkin"]

            # 非法 cron / 空 cron：保留原值（要停任务请关开关，别留一个永不触发的表达式）
            before = schedule["checkin"]["cron"]
            schedule = app_config.update_schedule({"checkin": {"cron": "9,21"}})
            assert schedule["checkin"]["cron"] == before
            schedule = app_config.update_schedule({"checkin": {"cron": "   "}})
            assert schedule["checkin"]["cron"] == before
            # enabled 非 bool 忽略；合法 cron 规范化（多空格收敛）后落盘
            schedule = app_config.update_schedule({
                "checkin": {"enabled": "yes"},
                "activity": {"cron": "15  6 * * 1-5"},
            })
            assert schedule["checkin"]["enabled"] is True
            assert schedule["activity"]["cron"] == "15 6 * * 1-5"

            app_config._cache = None  # 重启：从文件重读
            assert app_config.schedule()["activity"]["cron"] == "15 6 * * 1-5"
            assert app_config.schedule()["checkin"]["cron"] == before
            # 写坏了也不崩：坏文件回落默认值
            path.write_text("{ 不是 JSON", encoding="utf-8")
            app_config._cache = None
            assert app_config.schedule()["checkin"]["cron"] == f"{WB_SCHEDULE_MINUTE} 9,21 * * *"


def test_schedule_config_does_not_touch_proxy_section():
    with tempfile.TemporaryDirectory() as tmp, _temp_config(Path(tmp) / "config.json"):
        app_config.update({"proxy": {"mode": "manual", "url": "http://127.0.0.1:7890"}})
        app_config.update_schedule({"checkin": {"enabled": False}})
        current = app_config.get()
        assert current["proxy"] == {"mode": "manual", "url": "http://127.0.0.1:7890"}
        assert current["schedule"]["checkin"]["enabled"] is False


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
