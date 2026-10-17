"""号池共享内核：加权选号 + 在途租约 + 会话粘性。

codex 与 workbuddy 两个号池的选号流水线完全同构，差异只有几处，全部走子类钩子：

  ① 健康判定多一档（workbuddy 有模型级冷却）→ `_healthy_extra`
  ② 权重多一加项（workbuddy 按候选集最大余额归一的 credits）→ `_credits_of`
  ③ 快过期积分加成（workbuddy）→ `_expiring_now`
  ④ 成本分层与积分保底（workbuddy：实测扣费台账 + 目录倍率）→ `_cost_signal`
     / `_below_floor_blocked`
  ⑤ 软冷却兜底排除的冷却类别（codex 只排 waf；workbuddy 排 hard/waf）
     → `_soft_fallback_excluded_kinds`
  ⑥ 域过滤（workbuddy 按 realm 分册）→ `_realm_ok`

**所有新机制默认失效**：`top_candidates=0`（不截断）、`min_pick_gap_s=0`（无撞号窗口）、
`expiring_slots=1.0`（无加成）、`cost_explore_s=0`（不探索），且 `_credits_of` 默认
返回 None、`_cost_signal` 默认恒 (1, 0)。于是一个不覆写任何钩子的子类（codex）拿到的
就是历史行为逐字一致：候选集不变、权重 = 1 + 闲置补偿、抽签方式不变。**新的选号语义
只对显式打开开关的子类生效**（当前只有 WorkBuddy）。

配置常量仍留在 core.settings（各 provider 一组），由子类以关键字传给基类。

选号流水线：
  ① 粘性优先（ses_ → uid 命中且健康 → 续约直返）
  ② 过滤（非 active / 账号级冷却 / 熔断 / 在途占满 / 凭证缺失 / 积分保底）
  ③ 成本分层（默认不分层）：只留最优层（实测免费 → 无观测 → 实测收费）
  ④ 加权随机（1 + 闲置补偿 + credits 项；快过期账号再乘虚拟实例权重）
  ⑤ Top-N 短名单（默认 0 = 不截断）：等权重洗牌 → 按 (单价, 权重, uid) 排序 → 截断
  ⑥ 防惊群（默认关闭）：刚被选中的号让位；全热时按单调选中序号取最旧（LRU）
  ⑦ 全冷却兜底：最早到期的软冷却号顶班
  ⑧ 仍为空 → None（provider 译 429）

线程模型：ThreadingHTTPServer 线程/请求，一把 RLock 保护租约与粘性表；
选号的权重计算依赖 store 快照（读操作自洽）。闲置补偿与选中序号按内存 last_used
惰性计算（重启清零 = 无补偿/无序号，良性）。
"""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from providers.account_store import STATUS_ACTIVE, STATUS_DISABLED

# 等权重洗牌用的独立随机源：不消耗全局 random 的状态，加权抽签的随机序列因此
# 与「不洗牌」时保持同一语义（测试可 seed 全局源断言抽签结果不变）。
_shuffle_rng = random.Random()


class AccountPool:
    def __init__(
        self,
        store: Any,
        *,
        max_in_flight: int,
        sticky_enabled: bool,
        sticky_ttl_s: int,
        sticky_gc_s: int,
        idle_boost_max: float,
        idle_boost_per_hour: float,
        top_candidates: int = 0,
        min_pick_gap_s: float = 0.0,
        expiring_slots: float = 1.0,
        cost_explore_s: float = 0.0,
        model_cost_ttl_s: float = 0.0,
        sticky_marks_used: bool = False,
    ):
        self._store = store
        self._max_in_flight = max_in_flight
        self._sticky_enabled = sticky_enabled
        self._sticky_ttl_s = sticky_ttl_s
        self._sticky_gc_s = sticky_gc_s
        self._idle_boost_max = idle_boost_max
        self._idle_boost_per_hour = idle_boost_per_hour
        self._top_candidates = int(top_candidates)
        self._min_pick_gap_s = float(min_pick_gap_s)
        self._expiring_slots = float(expiring_slots)
        self._cost_explore_s = float(cost_explore_s)
        self._model_cost_ttl_s = float(model_cost_ttl_s)
        self._sticky_marks_used = bool(sticky_marks_used)
        self._lock = threading.RLock()
        self._in_flight: dict[str, set[str]] = {}   # uid → 在途 request_id 集
        self._last_used: dict[str, float] = {}      # uid → monotonic 最近派号时刻
        self._picked_seq: dict[str, int] = {}       # uid → 单调选中序号（防惊群 LRU 依据）
        self._pick_seq = 0
        self._sticky: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._last_gc = time.monotonic()
        # 成本台账：uid → {模型裸名: (每千 token 积分单价, 观测时刻 monotonic)}
        self._model_cost: dict[str, dict[str, tuple[float, float]]] = {}
        self._cost_guard = threading.Lock()
        # (realm, 模型) → 上次条件探索时刻。探索=把一个真实请求改道给「无观测」号，
        # 零额外上游调用地发现免费号。
        self._explored_at: dict[tuple[str, str], float] = {}
        # 目录倍率查询（上游目录的积分倍率字符串 → 数字）；未注入时按未知处理
        self._model_rate: Callable[[str, str], str | None] | None = None

    # ── 子类钩子 ──

    def _realm_ok(self, account: Any, realm: str | None) -> bool:
        """该账号是否属于请求声明的域（realm=None = 不限域）。"""
        return realm is None or getattr(account, "realm", None) == realm

    def _healthy_extra(self, account: Any, model: str | None, now_ms: int) -> bool:
        """基类健康判定之外的额外条件（模型级冷却等）。"""
        return True

    def _credits_of(self, account: Any) -> float | None:
        """参与权重加成的余额；None = 该池没有 credits 概念（不加这一项）。"""
        return None

    def _expiring_now(self, account: Any, now_ms: int) -> bool:
        """该账号是否存在「窗口内仍有效」的快过期积分批次。"""
        return False

    def _cost_signal(self, account: Any, model: str | None, now_ms: int) -> tuple[int, float]:
        """成本分层信号 → (层, 每千 token 单价)。

        层语义（对齐参考实现）：0 = 已实测免费，1 = 无观测，2 = 已实测收费。
        默认恒 (1, 0) = 全员同层 → 分层退化为不过滤。
        """
        return 1, 0.0

    def _below_floor_blocked(self, account: Any, model: str | None) -> bool:
        """积分保底拦截：余额触底的号是否该拒绝本次模型（默认不拦）。"""
        return False

    def _soft_fallback_excluded_kinds(self) -> frozenset[str]:
        """软冷却兜底时永不顶班的冷却类别（罚期语义重的）。"""
        return frozenset({"waf"})

    def _soft_fallback_eligible_kinds(self) -> frozenset[str]:
        """哪些冷却类别允许在"全池都在冷却"时顶班。

        白名单而不是黑名单：兜底意味着**明知会失败也去试**，只有"短暂的、试一下可能就
        好了"的冷却（429 退避）配得上它。新增一个冷却类别时默认不参与顶班，是更安全的
        默认——比如额度冷却（quota，等到窗口重置、可能几十天），把它算进来就等于把
        "全池额度用尽"翻译成"硬选一个必然 429 的号，还要记它一次失败"。
        """
        return frozenset({"soft"})

    # ── 选号 ──

    def pick(
        self,
        session_id: str,
        *,
        model: str | None = None,
        exclude: set[str] | None = None,
        realm: str | None = None,
    ) -> Any | None:
        """选一个健康账号（粘性优先 → 成本分层 → 加权随机 → 防惊群 → 软冷却兜底）。"""
        exclude = exclude or set()
        now_ms = int(time.time() * 1000)
        accounts = {
            a.uid: a for a in self._store.list_accounts() if self._realm_ok(a, realm)
        }

        # ① 粘性优先
        sticky_uid = self.sticky_uid(session_id)
        if sticky_uid and sticky_uid not in exclude:
            candidate = accounts.get(sticky_uid)
            if (
                candidate is not None
                and self._healthy(candidate, model, now_ms)
                and not self._below_floor_blocked(candidate, model)
            ):
                # 粘性命中是否推进「最近使用」：参考实现推进（已用时刻是"这个号刚
                # 干过活"的权威依据，撞号窗口/LRU 都靠它）；本仓库历史行为不推进，
                # 故默认关闭——只有开了撞号窗口的池（WorkBuddy）才打开，Codex 的
                # 闲置补偿动力学保持原样。
                if self._sticky_marks_used:
                    self._note_used(candidate.uid)
                return candidate
            self.unbind_sticky(session_id)  # 绑定号不健康：解绑走正常选号

        candidates = [a for uid, a in accounts.items() if uid not in exclude]
        healthy = [
            a for a in candidates
            if self._healthy(a, model, now_ms) and not self._below_floor_blocked(a, model)
        ]
        if healthy:
            chosen = self._select(healthy, model, realm, now_ms)
            if chosen is not None:
                return chosen
        # ⑦ 全冷却兜底：软冷却号顶班（宁可短冷却排队也不 429）。
        return self._soft_fallback(candidates, now_ms, model)

    def _healthy(self, account: Any, model: str | None, now_ms: int) -> bool:
        return (
            account.status == STATUS_ACTIVE
            and bool(account.access_token)
            and not account.account_cooldown_active(now_ms)
            and not account.breaker_open(now_ms)
            and not self.in_flight_full(account.uid)
            and self._healthy_extra(account, model, now_ms)
        )

    # ── 成本分层 + 加权抽签 + 短名单 + 防惊群 ──

    def _select(self, healthy: list[Any], model: str | None, realm: str | None,
                now_ms: int) -> Any | None:
        """从健康号里选一个（分层 → 权重 → 短名单 → 撞号窗口 → 抽签）。"""
        # 权重归一的基准用**分层前的全集口径**：截断排序与抽签权重共享同一基准，
        # 两个阶段的分值可比（对齐参考实现的 maxCredits 口径）。
        max_credits = 0.0
        credits: dict[str, float | None] = {}
        for account in healthy:
            value = self._credits_of(account)
            credits[account.uid] = value
            if value is not None and value > max_credits:
                max_credits = value

        signals = {a.uid: self._cost_signal(a, model, now_ms) for a in healthy}
        pool = healthy
        if model:
            best_tier = min(tier for tier, _cost in signals.values())
            explore_key = (realm or "", model)
            if (
                self._cost_explore_s > 0
                and best_tier == 0
                and any(tier == 1 for tier, _cost in signals.values())
                and self._explore_due(explore_key, now_ms)
            ):
                # 条件探索：tier 0 垄断层存在时，把这一轮改道给「无观测」号去实测。
                # 搭车一个真实用户请求（零新增上游请求），成功即毕业为 0/2 层。
                best_tier = 1
                self._mark_explored(explore_key, now_ms)
            tiered = [a for a in healthy if signals[a.uid][0] == best_tier]
            if tiered:
                pool = tiered
        if not pool:
            return None

        weights = {
            a.uid: self._weight_of(a, credits.get(a.uid), max_credits, now_ms) for a in pool
        }
        ordered = pool
        if 0 < self._top_candidates < len(pool):
            # 等权重会互相饿死：只按 (单价, 权重) 截断时，并列的号里固定那批会一直
            # 占着短名单（惊群集中到少数号的根因之一）。截断前对存在并列的候选洗一次，
            # 并让排序保持稳定（并列维持洗牌后的相对次序）——排序键里若再挂一个 uid
            # 全序兜底，洗牌会被完全覆盖掉，等于没洗。
            ordered = list(pool)
            values = list(weights.values())
            if len(set(values)) < len(values):
                _shuffle_rng.shuffle(ordered)
            ordered = sorted(ordered, key=lambda a: (signals[a.uid][1], -weights[a.uid]))
            shortlist = ordered[: self._top_candidates]
        else:
            shortlist = pool

        if self._min_pick_gap_s > 0:
            now_mono = time.monotonic()
            eligible = [
                a for a in shortlist
                if now_mono - self._last_used.get(a.uid, 0.0) >= self._min_pick_gap_s
            ]
            if not eligible:
                # 短名单全热（同一瞬间的并发都挤在这里）：越过短名单，按**分层后的
                # 全候选**取最久没被选中的号。用单调选中序号而不是墙钟：Windows 上
                # time.time() 精度约 0.5ms，连续选号时墙钟可能完全相等，比较恒假会
                # 退化成"永远选第一个"（集中到单号）。
                picked = min(ordered, key=lambda a: self._picked_seq.get(a.uid, 0))
                self._note_used(picked.uid)
                return picked
            shortlist = eligible

        chosen = self._weighted_choice(shortlist, weights)
        if chosen is not None:
            self._note_used(chosen.uid)
        return chosen

    def _weight_of(self, account: Any, credits: float | None, max_credits: float,
                   now_ms: int) -> float:
        """weight = 1 + 闲置补偿 + credits 项；快过期账号再乘虚拟实例权重。

        credits 项按候选集最大余额归一（避免量纲爆炸）：满额号 +10，约为闲置补偿
        满值（5.0）的两倍——"余额多的号更值得用"但压不过"闲置很久的号"。
        未知余额（None，如企业号）不加这一项（按 1.0 计），与历史语义一致。

        now_ms 只用于快过期有效期判定（store 侧时间是墙钟毫秒）；闲置补偿一律走
        单调时钟——它与 `_last_used` 同源，混用墙钟会把闲置时长算成几十年。
        """
        weight = 1.0 + self._idle_boost(account.uid)
        if credits is not None and max_credits > 0:
            weight += credits / max_credits * 10.0
        if self._expiring_slots != 1.0 and self._expiring_now(account, now_ms):
            # 快过期的那部分余额优先花掉，否则到期作废（官方赠送/补偿积分常带期限）
            weight *= self._expiring_slots
        return weight

    def _weighted_choice(self, candidates: list[Any], weights: dict[str, float]) -> Any | None:
        if not candidates:
            return None
        # 权重起手 1.0（见 _weight_of），抽签池恒非空权重——无需全零兜底
        draw = [max(weights.get(a.uid, 1.0), 0.0) for a in candidates]
        return random.choices(candidates, weights=draw, k=1)[0]

    def _idle_boost(self, uid: str) -> float:
        """闲置补偿：按距上次被选中的时长线性增长，封顶见 settings。

        时钟必须与 `_last_used` 同源（都是 time.monotonic）：monotonic 是进程内
        相对时钟，拿墙钟去减会得到「闲置了几十年」→ 补偿恒封顶。
        """
        last_used = self._last_used.get(uid)
        if last_used is None:
            return self._idle_boost_max  # 从未被本进程用过 = 最闲置
        idle_hours = max(time.monotonic() - last_used, 0.0) / 3600.0
        return min(idle_hours * self._idle_boost_per_hour, self._idle_boost_max)

    def _soft_fallback(self, candidates: list[Any], now_ms: int,
                       model: str | None = None) -> Any | None:
        """全冷却兜底：最早到期的软冷却号（罚期语义重的类别不参与顶班）。"""
        excluded = self._soft_fallback_excluded_kinds()
        eligible = self._soft_fallback_eligible_kinds()
        fallback: Any | None = None
        for account in candidates:
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            if account.breaker_open(now_ms) or account.cooldown_kind in excluded:
                continue
            if not account.account_cooldown_active(now_ms):
                continue  # 完全健康的号在上面已返回；此处只收冷却中的
            if account.cooldown_kind not in eligible:
                continue  # 不在白名单里的类别（如额度冷却）不顶班
            if not self._healthy_extra(account, model, now_ms):
                # 模型级冷却（11102 负缓存 / 6004 模型限流）也拦：顶班等于明知这个号
                # 不服务这个模型还发出去，必然白撞一次上游、再记一笔失败。拦下后
                # pick 返回 None，provider 才能判成"模型不可用"（400 model_unavailable）
                # 而不是"号全挂了"（429 + 换号重试）。
                continue
            if self._below_floor_blocked(account, model):
                continue  # 兜底是最后一道：保底在这里也要拦，否则触底号被"捞回来"
            if fallback is None or account.cooldown_until_ms < fallback.cooldown_until_ms:
                fallback = account
        if fallback is not None:
            self._note_used(fallback.uid)
        return fallback

    def _note_used(self, uid: str) -> None:
        with self._lock:
            self._last_used[uid] = time.monotonic()
            self._pick_seq += 1
            self._picked_seq[uid] = self._pick_seq

    # ── 模型级阻塞概览 ──

    def model_blocked(self, model: str) -> dict[str, Any]:
        """某模型是否在全池范围内被模型级冷却挡住 → {blocked, count, until_ms, reason}。

        选号返回 None 有两种完全不同的成因：(a) 池子真没可用号（账号级冷却/在途占满/
        积分保底）；(b) 号都在，但每个号都对这个模型处于模型级冷却（11102 负缓存 /
        6004 独立冷却）。两者对调用方的处置相反——前者该等，后者换模型才有用——所以
        调用方（provider）需要能分辨，否则"模型不可用"会被读成"号全挂了"。

        判定口径：**只要有任意一个非禁用账号还能服务该模型，就不算模型级阻塞**
        （此时选号失败另有原因，交回原有的"无可用账号"口径）。池里压根没有非禁用
        账号同理——那是"真的没号"，不是模型问题。
        """
        if not model:
            return {"blocked": False, "count": 0, "until_ms": 0, "reason": ""}
        now_ms = int(time.time() * 1000)
        count = 0
        unblock_ms = 0
        reason = ""
        for account in self._store.list_accounts():
            if account.status == STATUS_DISABLED:
                continue  # 禁用号的不可用与模型无关，不该算进"模型被挡"
            entry = (getattr(account, "model_cooldowns", None) or {}).get(model)
            if not entry or int(entry.get("until_ms") or 0) <= now_ms:
                return {"blocked": False, "count": 0, "until_ms": 0, "reason": ""}
            count += 1
            if not reason:
                reason = str(entry.get("reason") or "")
            until = int(entry.get("until_ms") or 0)
            if until and (unblock_ms == 0 or until < unblock_ms):
                unblock_ms = until
        if count == 0:
            return {"blocked": False, "count": 0, "until_ms": 0, "reason": ""}
        return {"blocked": True, "count": count, "until_ms": unblock_ms, "reason": reason}

    # ── 成本台账（实测扣费 + 目录倍率兜底）──

    def set_model_rate(self, lookup: Callable[[str, str], str | None] | None) -> None:
        """注入目录倍率查询：lookup(realm, 裸模型名) → 倍率字符串（"0.79" / "" = 未知）。"""
        self._model_rate = lookup

    def note_model_cost(self, uid: str, model: str, credit: float, tokens: int) -> None:
        """记录一次实测扣费观测（成功响应的 usage.credit）→ 每千 token 单价。

        单价按每千 token 归一，消除请求长度差异；credit ≤ 0 是「实测免费」的正面
        证据（限免期/夜间免费），同样入账（区分"观测到 0"与"没有观测"）。
        """
        if not uid or not model or tokens <= 0 or credit < 0:
            return
        per_1k = credit / float(tokens) * 1000.0
        now = time.monotonic()
        with self._cost_guard:
            bucket = self._model_cost.setdefault(uid, {})
            bucket[model] = (per_1k, now)

    def model_cost_of(self, uid: str, model: str, now: float | None = None) -> tuple[float, bool]:
        """该 (账号, 模型) 的有效实测单价 → (单价, 是否有有效观测)。

        观测只保留 model_cost_ttl_s 内（默认 0 = 台账关闭，恒「无观测」）：
        "夜间免费"这类时段性优惠过期后不该继续影响选号。
        """
        if self._model_cost_ttl_s <= 0:
            return 0.0, False
        now = time.monotonic() if now is None else now
        with self._cost_guard:
            entry = self._model_cost.get(uid, {}).get(model)
        if entry is None:
            return 0.0, False
        cost, seen = entry
        if now - seen > self._model_cost_ttl_s:
            return 0.0, False
        return cost, True

    def model_rate_of(self, realm: str, model: str) -> float | None:
        """目录倍率（数字）；未注入 / 目录未覆盖 / 解析失败 → None（未知）。"""
        if self._model_rate is None:
            return None
        raw = self._model_rate(realm, model)
        text = str(raw or "").strip()
        if not text:
            return None
        # 上游下发的是展示串（"x0.34 credits"、"x0 credits"）；抽出 x 后的数字
        digits = ""
        for index, char in enumerate(text.lower()):
            if char == "x":
                digits = text[index + 1:]
                break
        digits = digits.strip().split(" ")[0] if digits else text.split(" ")[0]
        try:
            return float(digits)
        except ValueError:
            return None

    def _explore_due(self, key: tuple[str, str], now_ms: int) -> bool:
        if self._cost_explore_s <= 0:
            return False
        last = self._explored_at.get(key, 0.0)
        return last <= 0 or now_ms / 1000.0 - last >= self._cost_explore_s

    def _mark_explored(self, key: tuple[str, str], now_ms: int) -> None:
        self._explored_at[key] = now_ms / 1000.0

    # ── 在途租约 ──

    def in_flight_full(self, uid: str) -> bool:
        with self._lock:
            return len(self._in_flight.get(uid, ())) >= self._max_in_flight

    def acquire(self, uid: str, request_id: str) -> bool:
        """占在途名额（占满返回 False，调用方换号）。"""
        with self._lock:
            slots = self._in_flight.setdefault(uid, set())
            if len(slots) >= self._max_in_flight:
                return False
            slots.add(request_id)
            return True

    def release(self, uid: str, request_id: str) -> None:
        with self._lock:
            slots = self._in_flight.get(uid)
            if slots is not None:
                slots.discard(request_id)
                if not slots:
                    self._in_flight.pop(uid, None)

    def in_flight_count(self, uid: str) -> int:
        with self._lock:
            return len(self._in_flight.get(uid, ()))

    # ── 会话粘性 ──

    def rebind_sticky(self, session_id: str, uid: str) -> None:
        if not self._sticky_enabled or not session_id:
            return
        with self._lock:
            self._sticky_gc_locked(force=True)
            self._sticky[session_id] = (uid, time.monotonic() + self._sticky_ttl_s)
            self._sticky.move_to_end(session_id)

    def unbind_sticky(self, session_id: str) -> None:
        with self._lock:
            self._sticky.pop(session_id, None)

    def sticky_uid(self, session_id: str) -> str | None:
        if not self._sticky_enabled or not session_id:
            return None
        with self._lock:
            entry = self._sticky.get(session_id)
            if entry is None:
                return None
            uid, expires = entry
            if expires < time.monotonic():
                del self._sticky[session_id]
                return None
            self._sticky.move_to_end(session_id)
            return uid

    def _sticky_gc_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_gc < self._sticky_gc_s:
            return
        self._last_gc = now
        while self._sticky:
            _, (_, expires) = next(iter(self._sticky.items()))
            if expires >= now:
                break
            self._sticky.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        """运维快照（面板 state 端点）。"""
        with self._lock:
            out = {
                "sticky_sessions": len(self._sticky),
                "in_flight": {uid: len(slots) for uid, slots in self._in_flight.items() if slots},
            }
        if self._model_cost_ttl_s > 0:
            now = time.monotonic()
            with self._cost_guard:
                costs = {
                    uid: {
                        model: round(cost, 4)
                        for model, (cost, seen) in bucket.items()
                        if now - seen <= self._model_cost_ttl_s
                    }
                    for uid, bucket in self._model_cost.items()
                }
            out["model_costs"] = {uid: bucket for uid, bucket in costs.items() if bucket}
        return out
