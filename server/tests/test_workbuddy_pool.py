"""workbuddy 选号机制测试：快过期加成 / 成本分层 / Top-N / 防惊群 / 积分保底。

这些机制全部住共用的 `providers/account_pool.py`（codex 与 workbuddy 同内核），
但**只有 WorkBuddy 打开开关**——所以本文件同时锁两件事：

  1. WorkBuddy 的新语义：权重公式、快过期虚拟实例、实测成本三层过滤 + 条件探索、
     Top-N 短名单（等权重洗牌）、撞号窗口与全候选 LRU 兜底、保底只拦收费模型；
  2. **Codex 行为逐字不变**：不覆写钩子的池 = 候选集不截断、权重恒为「1+闲置补偿」、
     粘性命中不推进「最近使用」。

运行：cd server && uv run python -m pytest tests/test_workbuddy_pool.py -q
（无 pytest 时：uv run python tests/test_workbuddy_pool.py 直接执行内建 main）
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.base import RequestIDs, Route
from providers.codex.pool import Pool as CodexPool
from providers.codex.store import Account as CodexAccount
from providers.codex.store import CodexStore
from providers.workbuddy import classify
from providers.workbuddy import pool as wb_pool
from providers.workbuddy import upstream as wb_upstream
from providers.workbuddy.pool import Pool
from providers.workbuddy.store import Account, WorkBuddyStore

# ── 桩与工具 ──


class _Resp:
    def __init__(self, status: int, payload=None, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@contextmanager
def _stub_http(payload):
    """把 upstream 的 HTTP 层换成固定响应（余额查询）。"""
    class _Stub:
        RequestsError = RuntimeError

        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append({"url": url, "body": kwargs.get("json"),
                               "headers": kwargs.get("headers") or {}})
            return _Resp(200, payload) if not callable(payload) else _Resp(200, payload())

    stub = _Stub()
    original = wb_upstream.crequests
    wb_upstream.crequests = stub  # type: ignore[assignment]
    try:
        yield stub
    finally:
        wb_upstream.crequests = original  # type: ignore[assignment]


def _resource(accounts: list[dict]) -> dict:
    return {"code": 0, "msg": "ok", "data": {"Response": {"Data": {"Accounts": accounts}}}}


def _cn(uid: str, **overrides) -> Account:
    base: dict = {"uid": uid, "realm": "cn", "access_token": "at", "refresh_token": "rt"}
    base.update(overrides)
    return Account(**base)


@contextmanager
def _wb_pool(accounts: list[Account]):
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            for account in accounts:
                store.upsert_account(account)
            yield Pool(store)
        finally:
            store.close()


@contextmanager
def _codex_pool(accounts: list[CodexAccount]):
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            for account in accounts:
                store.upsert_account(account)
            yield CodexPool(store)
        finally:
            store.close()


# ── 上游：快过期分桶 ──


def test_fetch_credits_detail_buckets_packages_by_expiry():
    """包级到期解析：窗口内的余额进 expiring，最早的到期包给出 earliest_*。"""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    soon = (datetime.now(tz) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    later = (datetime.now(tz) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    payload = _resource([
        # 2 天后到期、余额 300 → 窗口（7 天）内 → 计入 expiring，且是最早到期批次
        {"CycleCapacitySize": 500, "CycleCapacityRemain": 300, "CycleEndTime": soon},
        {"CycleCapacitySize": 1000, "CycleCapacityRemain": 800, "CycleEndTime": later},
        # 到期时间解析不出：只累加余额，不进任何到期分桶
        {"CycleCapacitySize": 100, "CycleCapacityRemain": 100, "CycleEndTime": "坏格式"},
        # 空包（余额 0）到期：完全不影响选号
        {"CapacitySize": 50, "CapacityRemain": 0, "CycleEndTime": soon},
    ])
    account = _cn("u_1")
    with _stub_http(payload):
        detail = wb_upstream.fetch_credits_detail(account, expiring_within_ms=7 * 24 * 3600 * 1000)

    assert detail.remain == 1200.0 and detail.total == 1650.0
    assert detail.expiring == 300.0  # 只有 2 天后那批在窗口内
    assert detail.earliest_remaining == 300.0
    expected_ms = int(
        (datetime.now(tz) + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp() * 1000
    )
    assert abs(detail.earliest_at_ms - expected_ms) < 24 * 3600 * 1000  # 同一批（±1 天）

    # 窗口 0 = 不分桶（expiring 恒 0，聚合口径与旧行为一致）
    with _stub_http(payload):
        plain = wb_upstream.fetch_credits_detail(account)
    assert plain.remain == 1200.0 and plain.expiring == 0.0
    # 兼容口径：fetch_credits 仍返回二元组
    with _stub_http(payload):
        assert wb_upstream.fetch_credits(account) == (1200.0, 1650.0)


def test_fetch_credits_detail_tolerates_bad_payload():
    account = _cn("u_1")
    with _stub_http({"code": 12000, "msg": "internal"}):
        try:
            wb_upstream.fetch_credits_detail(account)
            raise AssertionError("业务码非 0 应抛错")
        except ValueError as exc:
            assert "12000" in str(exc)
    # 空 Accounts（企业号/无资源包）：全零，不抛错
    with _stub_http({"code": 0, "data": {"Response": {"Data": {"Accounts": None}}}}):
        detail = wb_upstream.fetch_credits_detail(account)
    assert detail.remain == 0.0 and detail.total == 0.0 and detail.expiring == 0.0


def test_enterprise_quota_uses_enterprise_endpoint():
    """企业额度走 get-enterprise-user-usage（个人端点数企业号恒返回空 Accounts）。"""
    enterprise = _cn("u_ent", enterprise_id="ent-1")
    payload = {"code": 0, "msg": "OK", "data": {
        "credit": 812.45, "limitNum": 2000,
        "cycleEndTime": "2099-10-26 23:59:59", "cycleResetTime": "2099-10-27 00:00:00",
    }}
    with _stub_http(payload) as stub:
        detail = wb_upstream.fetch_credits_detail(enterprise)
    # 已用 812.45 四舍五入到 812 → 剩余 1188（向下取整会低报额度）
    assert detail.remain == 1188.0 and detail.total == 2000.0
    assert detail.earliest_at_ms > int(time.time() * 1000)  # 周期到期批次
    call = stub.calls[0]
    assert call["url"].endswith("/v2/billing/meter/get-enterprise-user-usage")
    assert call["headers"].get("x-enterprise-id") == "ent-1"  # 企业头必须带上
    assert call["body"] == {}

    # 不限量（limit_num 负值）→ 大剩余量 + 总量 -1：权重与积分保底都不构成限制
    with _stub_http({"code": 0, "data": {"credit": 5, "limit_num": -1}}):
        unlimited = wb_upstream.fetch_credits_detail(enterprise)
    assert unlimited.total < 0 and unlimited.remain > 10 ** 12
    assert unlimited.expiring == 0.0  # 不限量没有到期批次

    # 额度字段缺失（坏负载）按失败处理，不当成"余额 0"
    with _stub_http({"code": 0, "data": {}}):
        try:
            wb_upstream.fetch_credits_detail(enterprise)
            raise AssertionError("缺 limit 字段应抛错")
        except ValueError as exc:
            assert "limit" in str(exc)

    # 个人号仍走个人端点（企业分支不误伤）
    with _stub_http({"code": 0, "data": {"Response": {"Data": {"Accounts": [
        {"CycleCapacitySize": 100, "CycleCapacityRemain": 40}
    ]}}}}) as stub2:
        personal = wb_upstream.fetch_credits_detail(_cn("u_p"))
    assert personal.remain == 40.0 and personal.total == 100.0
    assert stub2.calls[0]["url"].endswith("/v2/billing/meter/get-user-resource")


# ── 权重：credits 项 + 快过期虚拟实例 ──


def test_weight_uses_credits_normalized_by_candidate_max():
    with _wb_pool([
        _cn("u_rich", credits_remain=1000.0, credits_total=1000.0),
        _cn("u_poor", credits_remain=100.0, credits_total=1000.0),
        _cn("u_unknown"),  # 企业号/未观测：credits 保持 None
    ]) as pool:
        pool._last_used["u_rich"] = pool._last_used["u_poor"] = pool._last_used["u_unknown"] = time.monotonic()
        now_ms = int(time.time() * 1000)
        rich = pool._weight_of(pool._store.get_account("u_rich"), 1000.0, 1000.0, now_ms)
        poor = pool._weight_of(pool._store.get_account("u_poor"), 100.0, 1000.0, now_ms)
        unknown = pool._weight_of(pool._store.get_account("u_unknown"), None, 1000.0, now_ms)
    # 满额 +10、一成 +1；未知余额不加这项（与历史「未知按 1.0」一致）
    assert abs(rich - 11.0) < 1e-6
    assert abs(poor - 2.0) < 1e-6
    assert abs(unknown - 1.0) < 1e-6


def test_expiring_account_gets_virtual_slot_boost():
    future = int(time.time() * 1000) + 3 * 24 * 3600 * 1000
    with _wb_pool([
        _cn("u_expiring", credits_remain=100.0, credits_expiring=100.0,
            credits_earliest_at_ms=future, credits_earliest_remaining=100.0),
        _cn("u_expired", credits_remain=100.0, credits_expiring=100.0,
            credits_earliest_at_ms=int(time.time() * 1000) - 1000,  # 已过期的批次不算
            credits_earliest_remaining=100.0),
        _cn("u_none", credits_remain=100.0),
    ]) as pool:
        for uid in ("u_expiring", "u_expired", "u_none"):
            pool._last_used[uid] = time.monotonic()
        now_ms = int(time.time() * 1000)
        expiring = pool._weight_of(pool._store.get_account("u_expiring"), 100.0, 100.0, now_ms)
        expired = pool._weight_of(pool._store.get_account("u_expired"), 100.0, 100.0, now_ms)
        plain = pool._weight_of(pool._store.get_account("u_none"), 100.0, 100.0, now_ms)
    assert abs(expiring - 11.0 * wb_pool.WB_EXPIRING_SLOTS) < 1e-6
    assert abs(expired - 11.0) < 1e-6
    assert abs(plain - 11.0) < 1e-6


def test_expiring_boost_is_opt_in():
    """关掉开关的池（expiring_slots=1.0）完全不受快过期字段影响。"""
    future = int(time.time() * 1000) + 3600_000
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(_cn("u_1", credits_remain=100.0, credits_expiring=100.0,
                                     credits_earliest_at_ms=future, credits_earliest_remaining=100.0))
            pool = Pool(store)
            pool._expiring_slots = 1.0
            pool._last_used["u_1"] = time.monotonic()
            now_ms = int(time.time() * 1000)
            weight = pool._weight_of(store.get_account("u_1"), 100.0, 100.0, now_ms)
        finally:
            store.close()
    assert abs(weight - 11.0) < 1e-6


# ── 成本分层 ──


def test_cost_tier_keeps_only_measured_free_layer():
    with _wb_pool([
        _cn("u_free"), _cn("u_unknown"), _cn("u_paid"),
    ]) as pool:
        pool._cost_explore_s = 0.0  # 探索单独有用例；这里只验三层过滤
        pool.note_model_cost("u_free", "glm-5.2", 0.0, 1000)   # 实测免费
        pool.note_model_cost("u_paid", "glm-5.2", 5.0, 1000)   # 实测收费 5/千 token
        picks = {pool.pick("glm-5.2", f"ses_{i}").uid for i in range(30)}
    assert picks == {"u_free"}  # 只留最优层（免费层垄断）


def test_cost_tier_prefers_unobserved_over_measured_paid():
    with _wb_pool([_cn("u_unknown"), _cn("u_paid")]) as pool:
        pool.note_model_cost("u_paid", "glm-5.2", 5.0, 1000)
        picks = {pool.pick("glm-5.2", f"ses_{i}").uid for i in range(30)}
    # 无观测排在实测收费之前：已知收费的号恒压过未知号的话，免费号永远轮不到
    assert picks == {"u_unknown"}


def test_cost_ledger_is_per_account_and_model():
    with _wb_pool([_cn("u_a"), _cn("u_b")]) as pool:
        pool.note_model_cost("u_a", "glm-5.2", 2.0, 1000)
        assert pool.model_cost_of("u_a", "glm-5.2") == (2.0, True)
        assert pool.model_cost_of("u_a", "hy3")[1] is False   # 同号不同模型：无观测
        assert pool.model_cost_of("u_b", "glm-5.2")[1] is False  # 不同号：无观测


def test_cost_ledger_ttl_and_disabled_ledger():
    with _wb_pool([_cn("u_a")]) as pool:
        pool._model_cost_ttl_s = 0.0
        pool.note_model_cost("u_a", "glm-5.2", 3.0, 1000)
        assert pool.model_cost_of("u_a", "glm-5.2")[1] is False  # 台账未启用 = 无观测
        pool._model_cost_ttl_s = 0.05
        pool.note_model_cost("u_a", "glm-5.2", 3.0, 1000)
        assert pool.model_cost_of("u_a", "glm-5.2")[1] is True
        time.sleep(0.08)
        assert pool.model_cost_of("u_a", "glm-5.2")[1] is False  # 过期观测不再参与分层


def test_cost_exploration_periodically_tries_unobserved_account():
    with _wb_pool([_cn("u_free"), _cn("u_unknown")]) as pool:
        pool.note_model_cost("u_free", "glm-5.2", 0.0, 1000)
        first = pool.pick("glm-5.2", "ses_a").uid
        rest = {pool.pick("glm-5.2", f"ses_{i}").uid for i in range(10)}
    assert first == "u_unknown"  # 首次探索：把这一轮改道给无观测号去实测
    assert rest == {"u_free"}  # 窗口内不再探索（默认 30 分钟）


def test_cost_exploration_disabled_when_interval_zero():
    with _wb_pool([_cn("u_free"), _cn("u_unknown")]) as pool:
        pool._cost_explore_s = 0.0
        pool.note_model_cost("u_free", "glm-5.2", 0.0, 1000)
        picks = {pool.pick("glm-5.2", f"ses_{i}").uid for i in range(20)}
    assert picks == {"u_free"}


def test_model_rate_parses_catalog_display_strings():
    with _wb_pool([_cn("u_a")]) as pool:
        pool.set_model_rate(lambda realm, model: {"paid": "x1.62 credits", "free": "x0 credits",
                                                  "junk": "内部模型"}.get(model, ""))
        assert pool.model_rate_of("cn", "paid") == 1.62
        assert pool.model_rate_of("cn", "free") == 0.0
        assert pool.model_rate_of("cn", "junk") is None
        assert pool.model_rate_of("cn", "missing") is None
    with _wb_pool([_cn("u_a")]) as pool:
        assert pool.model_rate_of("cn", "paid") is None  # 未注入查询 = 未知


# ── 积分保底：触底号只拒绝收费模型 ──


def test_floor_blocks_paid_model_but_allows_free_and_unknown():
    with _wb_pool([_cn("u_low", credits_remain=10.0, credits_total=100.0)]) as pool:
        pool.set_model_rate(lambda realm, model: {"paid": "x1.62", "free": "x0"}.get(model))
        original = wb_pool.WB_CREDIT_FLOOR
        wb_pool.WB_CREDIT_FLOOR = 100
        try:
            assert pool.pick("paid", "ses_1") is None       # 触底 + 目录倍率收费 → 拦
            assert pool.pick("free", "ses_2") is not None   # 免费模型照放行（保底的目的）
            assert pool.pick("unknown-model", "ses_3") is not None  # 未知倍率不拦
            # 实测台账优先于目录倍率：实测免费 → 放行
            pool.note_model_cost("u_low", "paid", 0.0, 1000)
            assert pool.pick("paid", "ses_4") is not None
        finally:
            wb_pool.WB_CREDIT_FLOOR = original
    # 保底关闭（默认 0）时一切照旧
    with _wb_pool([_cn("u_low", credits_remain=1.0, credits_total=100.0)]) as pool:
        pool.set_model_rate(lambda realm, model: "x1.62")
        assert pool.pick("paid", "ses_1") is not None


def test_floor_applies_to_soft_fallback_too():
    """兜底是最后一道：触底号不该被"捞回来"去接收费模型。"""
    now_ms = int(time.time() * 1000)
    with _wb_pool([_cn("u_low", credits_remain=10.0, cooldown_until_ms=now_ms + 60_000,
                       cooldown_kind="soft")]) as pool:
        pool.set_model_rate(lambda realm, model: "x1.62")
        original = wb_pool.WB_CREDIT_FLOOR
        wb_pool.WB_CREDIT_FLOOR = 100
        try:
            assert pool.pick("paid", "ses_1") is None
            pool.set_model_rate(lambda realm, model: "x0")
            assert pool.pick("free", "ses_2") is not None
        finally:
            wb_pool.WB_CREDIT_FLOOR = original


# ── Top-N 短名单 + 防惊群 ──


def test_top_n_shortlist_does_not_starve_equal_weight_accounts():
    """等权重候选超过短名单长度时，洗牌保证短名单不是固定的一批（否则靠后的号饿死）。"""
    accounts = [_cn(f"u_{index:02d}") for index in range(8)]  # 全等权重（都从未用过）
    with _wb_pool(accounts) as pool:
        picked = {pool.pick("m", f"ses_{index}").uid for index in range(400)}
    assert picked == {a.uid for a in accounts}


def test_min_pick_gap_sends_back_to_back_picks_to_other_accounts():
    with _wb_pool([_cn("u_a"), _cn("u_b")]) as pool:
        first = pool.pick("m", "ses_1")
        second = pool.pick("m", "ses_2")
    assert {first.uid, second.uid} == {"u_a", "u_b"}  # 第二个请求不撞刚选中的号


def test_min_pick_gap_lru_fallback_covers_full_candidate_pool():
    """短名单全热时越过短名单、按单调选中序号取最旧——而不是集中回同一个号。"""
    with _wb_pool([_cn(f"u_{i}") for i in range(4)]) as pool:
        pool._top_candidates = 2  # 短名单只有 2，第 3 次选号时它俩都在撞号窗口内
        pool._min_pick_gap_s = 5.0
        picks = [pool.pick("m", f"ses_{i}").uid for i in range(3)]
        picked_seq = dict(pool._picked_seq)
    # 连续三次选号没有一次撞上前一次选中的号（短名单 2 个、4 个候选：第 3 次短名单
    # 全热 → 越过短名单走全候选 LRU，而不是死盯短名单里那个）
    assert picks[1] != picks[0] and picks[2] != picks[1]
    assert len(picked_seq) >= 2 and all(seq > 0 for seq in picked_seq.values())


def test_lru_fallback_uses_monotonic_seq_not_wall_clock():
    """墙钟精度不足时（连续选号发生在同一毫秒）仍必须分散——序号是权威依据。"""
    with _wb_pool([_cn(f"u_{i}") for i in range(5)]) as pool:
        pool._min_pick_gap_s = 60.0  # 全部号都"刚被用过"
        pool._last_used = {f"u_{i}": time.monotonic() for i in range(5)}  # 同一时刻标记
        picks = [pool.pick("m", f"ses_{i}").uid for i in range(5)]
    assert len(set(picks)) == 5


def test_sticky_hit_marks_used_only_when_enabled():
    with _wb_pool([_cn("u_a"), _cn("u_b")]) as pool:
        pool.rebind_sticky("ses_1", "u_a")
        assert pool.pick("m", "ses_1").uid == "u_a"
        assert "u_a" in pool._last_used  # WB：粘性命中也推进最近使用
    with _codex_pool([CodexAccount(uid="c_a", access_token="at"),
                      CodexAccount(uid="c_b", access_token="at")]) as pool:
        pool.rebind_sticky("ses_1", "c_a")
        assert pool.pick("ses_1").uid == "c_a"
        assert "c_a" not in pool._last_used  # Codex：历史行为不变


# ── Codex 行为不变（默认失效的钩子）──


def test_codex_pool_keeps_legacy_selection_semantics():
    with _codex_pool([CodexAccount(uid=f"c_{i}", access_token="at") for i in range(8)]) as pool:
        assert pool._top_candidates == 0 and pool._min_pick_gap_s == 0.0
        assert pool._expiring_slots == 1.0 and pool._cost_explore_s == 0.0
        assert pool._credits_of(pool._store.get_account("c_0")) is None
        assert pool._cost_signal(pool._store.get_account("c_0"), "gpt-5", 0) == (1, 0.0)
        now_ms = int(time.time() * 1000)
        account = pool._store.get_account("c_0")
        # 权重恒为「1 + 闲置补偿」：从未用过 = 满值 5.0
        assert abs(pool._weight_of(account, None, 0.0, now_ms) - 6.0) < 1e-6
        # 候选集不截断、无撞号过滤：连抽 400 次每个号都拿得到
        picked = {pool.pick(f"ses_{i}").uid for i in range(400)}
    assert picked == {f"c_{i}" for i in range(8)}


def test_codex_pool_ignores_model_costs_and_rates():
    with _codex_pool([CodexAccount(uid="c_a", access_token="at"),
                      CodexAccount(uid="c_b", access_token="at")]) as pool:
        pool.note_model_cost("c_a", "gpt-5", 9.0, 1000)  # 台账写进去也不参与分层
        pool.set_model_rate(lambda realm, model: "x9")
        picks = {pool.pick(f"ses_{i}").uid for i in range(40)}
    assert picks == {"c_a", "c_b"}


# ── 服务侧：末帧 usage.credit → 成本台账 ──


def test_usage_frame_observer_feeds_ledger():
    from providers.workbuddy.streamwrap import SseRepairStream

    seen: list[dict] = []
    frames = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":30,"credit":2.5}}\n\n',
        b"data: [DONE]\n\n",
    ]
    stream = SseRepairStream(iter(frames), None, on_usage_frame=seen.append)
    list(stream)
    assert len(seen) == 1 and seen[0]["credit"] == 2.5


def test_provider_records_measured_cost_per_1k_tokens():
    with _wb_pool([_cn("u_1")]) as pool:
        # 30 token 花了 2.5 积分 → 83.33/千 token
        pool.note_model_cost("u_1", "glm-5.2", 2.5, 30)
        cost, observed = pool.model_cost_of("u_1", "glm-5.2")
    assert observed and abs(cost - 2.5 / 30 * 1000) < 1e-6


def test_provider_usage_hook_requires_credit_and_tokens():
    from providers.workbuddy.provider import WorkBuddyProvider, _usage_total_tokens

    with _wb_pool([_cn("u_1")]) as pool:
        provider = WorkBuddyProvider(store=pool._store)
        provider._pool = pool
        account = pool._store.get_account("u_1")
        # 缺 credit = 没观测到，不写台账（"测不到"不等于"测得 0"）
        provider._note_usage_cost(account, "glm-5.2", {"prompt_tokens": 10, "completion_tokens": 10})
        assert pool.model_cost_of("u_1", "glm-5.2")[1] is False
        # 有 credit 但没有 token 数：单价无从归一，同样不写
        provider._note_usage_cost(account, "glm-5.2", {"credit": 1.0})
        assert pool.model_cost_of("u_1", "glm-5.2")[1] is False
        # credit=0 是有效观测（限免期），必须入账
        provider._note_usage_cost(account, "glm-5.2", {"credit": 0, "total_tokens": 20})
        assert pool.model_cost_of("u_1", "glm-5.2") == (0.0, True)
        # total_tokens 缺失时用 prompt + completion
        assert _usage_total_tokens({"prompt_tokens": 4, "completion_tokens": 6}) == 10
        assert _usage_total_tokens({"total_tokens": 7}) == 7


# ── 11102 负缓存 + 模型级阻塞口径 ──


def test_model_blocked_reports_only_when_every_account_is_cooled():
    now_ms = int(time.time() * 1000)
    cooled = {"glm-5.2": {"until_ms": now_ms + 6 * 3600 * 1000, "kind": "model_blocked",
                          "hits": 1, "reason": '{"code":11102}'}}
    with _wb_pool([
        _cn("u_a", model_cooldowns=cooled),
        _cn("u_b", model_cooldowns=cooled),
    ]) as pool:
        blocked = pool.model_blocked("glm-5.2")
        assert blocked["blocked"] is True and blocked["count"] == 2
        assert blocked["reason"] == '{"code":11102}'
        assert 0 < blocked["until_ms"] - now_ms <= 6 * 3600 * 1000
        # 别的模型不受影响；池里没有该模型的冷却时不算阻塞
        assert pool.model_blocked("hy3")["blocked"] is False
        assert pool.model_blocked("")["blocked"] is False

    # 只要有一个号还能服务该模型 → 不算模型级阻塞（选号失败另有原因）
    with _wb_pool([_cn("u_a", model_cooldowns=cooled), _cn("u_b")]) as pool:
        assert pool.model_blocked("glm-5.2")["blocked"] is False
    # 禁用号不参与：它的不可用与模型无关
    with _wb_pool([_cn("u_a", model_cooldowns=cooled),
                   _cn("u_b", status="disabled")]) as pool:
        assert pool.model_blocked("glm-5.2")["blocked"] is True
    # 过期条目不算阻塞
    stale = {"glm-5.2": {"until_ms": now_ms - 1000, "kind": "model_blocked"}}
    with _wb_pool([_cn("u_a", model_cooldowns=stale)]) as pool:
        assert pool.model_blocked("glm-5.2")["blocked"] is False


def test_blocked_model_is_not_selected_and_cleared_on_success():
    """11102 负缓存的完整闭环：记入 → 选号避开 → 成功清除（半开探测路径）。"""
    from providers.workbuddy.provider import WorkBuddyProvider

    with _wb_pool([_cn("u_a"), _cn("u_b")]) as pool:
        provider = WorkBuddyProvider(store=pool._store)
        provider._pool = pool
        account_a = pool._store.get_account("u_a")

        provider._penalize(
            account_a, classify.classify(400, '{"code":"11102","msg":"service info not found"}'),
            "glm-5.2", '{"code":"11102","msg":"service info not found"}',
        )
        stored = pool._store.get_account("u_a").model_cooldowns["glm-5.2"]
        assert stored["kind"] == "model_blocked" and stored["hits"] == 1
        assert stored["until_ms"] > int(time.time() * 1000)

        picks = {pool.pick("glm-5.2", f"ses_{i}").uid for i in range(20)}
        assert picks == {"u_b"}  # 被负缓存的号不再被选中
        assert {pool.pick("hy3", f"ses_h{i}").uid for i in range(20)} != {"u_b"}  # 其它模型不受影响

        # 第二轮命中：退避翻倍（6h → 12h）
        provider._penalize(
            pool._store.get_account("u_a"),
            classify.classify(400, '{"code":"11102","msg":"service info not found"}'),
            "glm-5.2", "boom",
        )
        assert pool._store.get_account("u_a").model_cooldowns["glm-5.2"]["hits"] == 2
        assert pool._store.get_account("u_a").model_cooldowns["glm-5.2"]["until_ms"] > stored["until_ms"]

        # 成功请求清除负缓存（6004 条目必须保留：有上游重置墙钟语义）
        pool._store.update_runtime("u_a", model_cooldowns={
            "glm-5.2": stored,
            "hy3": {"until_ms": int(time.time() * 1000) + 60_000, "kind": "6004"},
        })
        provider._clear_model_block(pool._store.get_account("u_a"), "glm-5.2")
        left = pool._store.get_account("u_a").model_cooldowns
        assert "glm-5.2" not in left and "hy3" in left


def test_provider_reports_model_unavailable_instead_of_no_account():
    """号都在、只是都不服务这个模型 → ModelUnavailable（400 口径），而不是 429。"""
    import json as _json

    from gateway.base import ModelUnavailable, UpstreamUnavailable
    from providers.workbuddy.provider import WorkBuddyProvider

    with _wb_pool([_cn("u_a")]) as pool:
        provider = WorkBuddyProvider(store=pool._store)
        provider._pool = pool
        route = Route(provider="workbuddy", model="cn:glm-5.2",
                      upstream_model="glm-5.2", protocol="chat", free=False, realm="cn")
        ids = RequestIDs(session="ses_t", request="msg_t", project="prj_t")
        # 号对该模型处于负缓存 → 选号直接落空
        provider._penalize(
            pool._store.get_account("u_a"),
            classify.classify(400, '{"code":"11102","msg":"service info not found"}'),
            "glm-5.2", '{"code":"11102","msg":"service info not found"}',
        )
        try:
            provider.send(route, "chat",
                          _json.dumps({"model": "glm-5.2", "messages": [], "stream": True}).encode(),
                          ids, stream=True)
            raise AssertionError("应抛 ModelUnavailable")
        except ModelUnavailable as exc:
            assert "try another model" in str(exc)
            assert "1 个账号" in exc.hint and "11102" in exc.hint

        # 号级原因（禁用）仍是"无可用账号"口径，不冒充模型问题
        pool._store.update_runtime("u_a", status="disabled")
        try:
            provider.send(route, "chat",
                          _json.dumps({"model": "glm-5.2", "messages": [], "stream": True}).encode(),
                          ids, stream=True)
            raise AssertionError("应抛 UpstreamUnavailable")
        except UpstreamUnavailable:
            pass


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
