"""Codex Provider：Provider 契约实现 + 单请求轮换主循环。

上游是 OpenAI Responses 协议（`POST {base}/responses`），因此所有 codex 模型
`route.protocol = "responses"`——chat/anthropic 客户端由网关的协议桥自动转换，
本 provider 不需要任何协议代码。

send 契约（与 workbuddy 一致）：把 send 当原子操作——只在拿到非 2xx（还没给客户端
发过一个字节）时轮换，流一旦开始不换号；恒 stream=True 打上游，非流式客户端由网关
collapse_stream 折叠。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as crequests

from core.logger import logger
from core.settings import (
    CODEX_BACKOFF_BASE_S,
    CODEX_BASE,
    CODEX_BREAKER_BASE_S,
    CODEX_BREAKER_MAX_S,
    CODEX_BREAKER_THRESHOLD,
    CODEX_COOLDOWN_AUTH,
    CODEX_COOLDOWN_NOT_FOUND,
    CODEX_COOLDOWN_SOFT_BASE,
    CODEX_COOLDOWN_SOFT_MAX,
    CODEX_COOLDOWN_WAF,
    CODEX_REFRESH_AHEAD_S,
    CODEX_ROTATE_ATTEMPTS,
    CODEX_STICKY_TTL_S,
)
from gateway.base import (
    ModelInfo,
    ModelNotFound,
    RequestIDs,
    Route,
    UpstreamResponse,
    UpstreamUnavailable,
)
from providers import models_dev, models_snapshot, profiles
from providers.account_runtime import AccountRuntime
from providers.capabilities import capability_block
from providers.codex import classify, oauth, quota, shape
from providers.codex import upstream as cx_upstream
from providers.codex.pool import Pool
from providers.codex.store import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    Account,
    CodexStore,
    default_store,
    write_with_retry,
)
from providers.common import RawBytes, refresh_result, stamp, stamp_now
from providers.identity import derive_request_ids

# 请求路径等刷新锁的上限（秒）：另一个刷新在途时最多等这么久，超时按失败处理
_REFRESH_LOCK_WAIT_S = 30.0
# 不罚号类别（请求级错误：换号无用，直接透传）
_NO_PENALTY_KINDS = frozenset({classify.CLIENT})
# 额度用尽用的冷却类别。区别于 soft（429 退避）/ auth / waf：它表达的是"到窗口重置
# 时刻之前都没额度"，时长可以长达几十天，且只有"额度恢复"或"新窗口"才该解除它。
QUOTA_COOLDOWN_KIND = "quota"
# 粘性路由令牌的进程内缓存上限（超了先清过期、再丢最旧的一半）。条目量级 = 活跃
# 会话数×账号轮换跨度，几千条足够；纯内存缓存，重启后重新捕获即可。
_TURN_STATE_MAX_ENTRIES = 4096


@dataclass
class _Catalog:
    """模型目录快照（多账号可见模型的并集）。"""

    models: dict[str, dict[str, Any]] = field(default_factory=dict)  # 裸名 → 条目
    refreshed_at: str | None = None
    # 缓存内容的最后变更时间（快照 meta 的 updated_at_ms）：本进程从快照
    # 冷启动时它是唯一的时间线索，与 refreshed_at（本进程刷新时间）分开不混用。
    cached_at_ms: int = 0


def _core_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """上游 manifest 条目 → 统一核心字段条目（目录与快照共用同一套字段词表）。
    manifest 里的 base_instructions 等大块字段
    目录侧用不到（ModelInfo 只取名字），不进内存也不落盘。

    protocol 一并落盘：快照现在是冷启动的唯一数据源，读的人（包括人工排障）不用
    再去翻这家固定走 responses 的代码。
    """
    model_id = str(entry.get("id") or entry.get("slug") or "").strip()
    levels = entry.get("supported_reasoning_levels")
    efforts: list[str] | None = None
    if isinstance(levels, list):
        efforts = [
            str(level["effort"]) for level in levels
            if isinstance(level, dict) and level.get("effort")
        ]
    has_reasoning = efforts is not None or bool(entry.get("default_reasoning_level"))
    modalities = entry.get("input_modalities")
    tool_keys = ("tool_mode", "supports_parallel_tool_calls", "experimental_supported_tools")
    has_tools = any(key in entry for key in tool_keys)
    return models_snapshot.core_entry(
        id=model_id,
        name=str(entry.get("display_name") or entry.get("name") or model_id),
        description=entry.get("description"),
        context_window=entry.get("context_window"),
        input_modalities=[str(t) for t in modalities] if isinstance(modalities, list) else None,
        supports_images="image" in modalities if isinstance(modalities, list) else None,
        supports_reasoning=(
            (bool(efforts) or bool(entry.get("default_reasoning_level"))) if has_reasoning else None
        ),
        supports_tool_call=(
            bool(entry.get("tool_mode") or entry.get("supports_parallel_tool_calls")
                 or entry.get("experimental_supported_tools"))
            if has_tools else None
        ),
        reasoning_efforts=efforts,
        default_effort=entry.get("default_reasoning_level"),
        protocol="responses",
    )


class CodexProvider(AccountRuntime):
    name = "codex"
    upstream_url = CODEX_BASE
    # 熔断与退避（阈值与时长由下面这几个常量给出）
    breaker_threshold = CODEX_BREAKER_THRESHOLD
    breaker_base_s = CODEX_BREAKER_BASE_S
    breaker_max_s = CODEX_BREAKER_MAX_S
    backoff_base_s = CODEX_BACKOFF_BASE_S

    def __init__(self, store: CodexStore | None = None):
        self._store = store or default_store()
        self._pool = Pool(self._store)
        self._lock = threading.Lock()
        self._catalog = _Catalog()
        self._init_runtime()  # 刷新串行化（RT 轮换不能双花）
        # x-codex-turn-state：上游下发的粘性路由令牌，按「账号 × 会话」回显（官方
        # 客户端同款：会话级持有、换账号即重置）。
        # 纯进程内缓存——重启后重新捕获，最多丢一次粘性，无副作用。
        self._turn_states: dict[str, tuple[str, int]] = {}
        self._turn_states_lock = threading.Lock()
        from providers.codex.login import LoginManager

        # 不挂 on_account_added：新号入库不再自动拉模型目录（目录只在手动
        # 「获取模型」时刷新）。
        self._login = LoginManager(self._store)

    # ── 目录 ──

    def refresh(self) -> dict[str, Any]:
        """逐账号拉模型目录并合并（各账号可见集合可能不同）；失败保留旧册。

        手动刷新是常规路径下唯一的触发点（周期自动刷新已移除），所以失败要能回报
        给界面，不能只写日志。返回 {added, removed, failed, error} ——与另两家的
        手动刷新同一个形状，api 层按同一组键拼响应。
        """
        merged: dict[str, dict[str, Any]] = {}
        seen_account = False
        errors = 0
        for account in self._store.list_accounts():
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            seen_account = True
            try:
                entries = cx_upstream.fetch_models(account)
            except Exception as exc:
                errors += 1
                logger.warning(
                    f"[codex] uid={account.uid[:8]}… 模型目录刷新失败: {type(exc).__name__}: {exc}"
                )
                continue
            for entry in entries:
                model_id = str(entry.get("id") or "").strip()
                if model_id:
                    merged.setdefault(model_id, _core_entry(entry))
        with self._lock:
            previous = dict(self._catalog.models)
        # 空结果护栏：本地本来有目录，上游这次一个模型都没给（账号全失败/上游抽风），
        # 保留旧目录与旧快照——否则网关会变成"没有任何模型"，推理全挂。
        # 失败与"上游确实为空"要分开报：前者是故障，后者（罕见）也不该当成功处理。
        if not merged and previous:
            reason = (
                f"{errors} 个账号拉取失败（已保留原目录）" if errors
                else "上游返回空目录（已保留原目录）"
            )
            logger.warning(
                f"[codex] 模型目录本轮无产出, 保留旧目录 models={len(previous)} errors={errors}"
            )
            return refresh_result(failed=errors or 1, error=reason)
        if not merged:
            if not seen_account:
                logger.info("[codex] 无可用账号, 跳过模型目录刷新")
                return refresh_result(failed=1, error="无可用账号")
            # 上游确实一个模型都没有（新装的号可能如此）：如实写下空目录
            logger.info("[codex] 上游目录为空")
        added = sorted(set(merged) - set(previous))
        removed = sorted(set(previous) - set(merged))
        written = models_snapshot.write("codex", models_snapshot.sort_entries(list(merged.values())))
        with self._lock:
            self._catalog = _Catalog(
                models=merged,
                refreshed_at=stamp_now(),
                cached_at_ms=int(models_snapshot.read_meta("codex").get("updated_at_ms") or 0),
            )
        detail = []
        if added:
            detail.append(f"新增={len(added)}({', '.join(added[:5])})")
        if removed:
            detail.append(f"移除={len(removed)}({', '.join(removed[:5])})")
        logger.info(
            f"[codex] 模型目录刷新完成 models={len(merged)} errors={errors} "
            f"落盘={'是' if written else '无变化'} {' '.join(detail) or '无变化'}"
        )
        return refresh_result(added=len(added), removed=len(removed), failed=errors)

    def load_from_snapshot(self) -> int:
        """用上次落盘的快照填充内存目录（冷启动的唯一数据源），返回条目数。

        手动获取是唯一的常规刷新路径，所以启动时内存目录只能来自缓存——它是
        **路由依据**（route() 靠它判模型是否存在），空着等于整个网关没有可服务的
        模型。`refreshed_at` 留空、改用 `cached_at_ms` 报缓存内容时间，不冒充本次
        新鲜度。只在内存目录为空时填充，不会把旧快照盖到已刷新出来的新目录上。
        """
        with self._lock:
            if self._catalog.models:
                return len(self._catalog.models)
        raw = models_snapshot.read().get("codex")
        if not isinstance(raw, list) or not raw:
            return 0
        models: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if model_id:
                models[model_id] = entry
        if not models:
            return 0
        cached_at_ms = int(models_snapshot.read_meta("codex").get("updated_at_ms") or 0)
        with self._lock:
            if not self._catalog.models:
                self._catalog = _Catalog(
                    models=models, refreshed_at=None, cached_at_ms=cached_at_ms
                )
        logger.info(
            f"[codex] 目录从快照恢复 models={len(models)} 缓存更新于={stamp(cached_at_ms) or '未知'}"
        )
        return len(models)

    def status(self) -> dict[str, Any]:
        with self._lock:
            models = len(self._catalog.models)
            refreshed_at = self._catalog.refreshed_at
            cached_at_ms = self._catalog.cached_at_ms
        counts = self._store.count_by_status()
        now_ms = int(time.time() * 1000)
        accounts = self._store.list_accounts()
        return {
            "upstream": CODEX_BASE,
            "refreshed_at": refreshed_at,
            "cached_at": stamp(cached_at_ms),
            "models": models,
            "pool": {
                "accounts": len(accounts),
                "active": counts.get(STATUS_ACTIVE, 0),
                "paused": counts.get("paused", 0),
                "disabled": counts.get("disabled", 0),
                "cooldown": sum(1 for a in accounts if a.account_cooldown_active(now_ms)),
                "breaker": sum(1 for a in accounts if a.breaker_open(now_ms)),
                "in_flight": sum(self._pool.in_flight_count(a.uid) for a in accounts),
                "sticky_sessions": self._pool.snapshot()["sticky_sessions"],
            },
        }

    def list_models(self) -> list[ModelInfo]:
        with self._lock:
            models = dict(self._catalog.models)
        return [self._model_info(bare, entry) for bare, entry in sorted(models.items())]

    def _model_info(self, bare: str, entry: dict[str, Any]) -> ModelInfo:
        name = str(entry.get("name") or bare)
        # 数值与模态先落一份 manifest 声明值，再让 models.dev 目录**优先**：匹配到就
        # 按目录来（manifest 的 context_window / input_modalities 与目录给的常不是
        # 一个数），没匹配到回落 manifest 声明。命中目录的字段在 metadata 里标注
        # 来源，展示层据此提示出处。
        metadata: dict[str, Any] = {
            "name": name,
            "context_window": entry.get("context_window"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "input_modalities": entry.get("input_modalities"),
            "output_modalities": entry.get("output_modalities"),
        }
        filled = models_dev.fill_limits(
            bare,
            metadata,
            prefer=True,
            list_keys={
                "input_modalities": "input_modalities",
                "output_modalities": "output_modalities",
            },
        )
        if filled:
            metadata["limits_source"] = {key: "models.dev" for key in filled}
        # 能力块按目录逐条声明，不写死：manifest 里 input_modalities /
        # supported_reasoning_levels / tool_mode 都是逐模型的，写死会让 /v1/models
        # 对每个模型报同一套能力。没声明的键省略，落回档案默认。
        # 模态读上面解析后的 metadata（目录优先的结果），别退回 entry——否则展示是
        # 目录值、对外声明却是 manifest 值，两边自相矛盾。
        capabilities = capability_block(
            profiles.CODEX,
            "responses",
            {
                "input_modalities": metadata.get("input_modalities"),
                "output_modalities": metadata.get("output_modalities"),
                "supports_reasoning": entry.get("supports_reasoning"),
                "supports_function_calling": entry.get("supports_tool_call"),
                "context_window": metadata["context_window"],
                "max_output_tokens": metadata["max_output_tokens"],
            },
        )
        return ModelInfo(
            id=f"cx:{bare}",
            upstream_model=bare,
            protocol="responses",
            free=False,
            display_name=name,
            capabilities=capabilities,
            metadata=metadata,
        )

    # ── 路由 ──

    def _bare(self, model: str) -> str:
        """别名 cx:xxx（及旧式 codex/xxx）→ 上游裸名。"""
        return model.removeprefix("cx:").removeprefix(f"{self.name}/")

    def supported(self, model: str) -> bool:
        with self._lock:
            return self._bare(model) in self._catalog.models

    def route(self, model: str) -> Route:
        bare = self._bare(model)
        with self._lock:
            known = bare in self._catalog.models
        if not known:
            raise ModelNotFound(f'model "{model}" is not available')
        return Route(
            provider=self.name, model=f"cx:{bare}", upstream_model=bare,
            protocol="responses", free=False,
        )

    # ── 请求 ──

    def derive_ids(
        self, headers: dict[str, str], body: dict[str, Any], client_ip: str = ""
    ) -> RequestIDs:
        return derive_request_ids(headers, body, client_ip)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        # 会话相关的 prompt_cache_key 不在这里注入（shape 阶段还不知道客户端键）——
        # 放在 send() 里用 ids.client 注入：客户端稳定键，静态前缀跨会话共享。
        return body, shape.shape_codex_body(body, "")

    def send(
        self,
        route: Route,
        protocol: str,
        body: bytes,
        ids: RequestIDs,
        *,
        stream: bool,
    ) -> UpstreamResponse:
        """轮换主循环。stream 参数为客户端意图——打上游恒流式。"""
        try:
            body_dict = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body_dict = {}
        if not isinstance(body_dict, dict):
            body_dict = {}
        session_id = ids.session

        exclude: set[str] = set()
        refreshed: set[str] = set()
        last_error: UpstreamResponse | None = None
        attempts = 0
        body_dirty = False
        while attempts < CODEX_ROTATE_ATTEMPTS:
            account = self._pool.pick(session_id, exclude)
            if account is None:
                if last_error is not None:
                    return last_error
                raise UpstreamUnavailable("codex: no available account (all cooling down or disabled)")
            if not self._pool.acquire(account.uid, ids.request):
                exclude.add(account.uid)  # 在途占满：本轮换号
                continue
            try:
                account = self._maybe_refresh(account)
                # prompt_cache_key 用客户端键（ids.client，跨会话不变）：同一客户端的
                # 所有会话共享同一段静态前缀缓存，新会话第一发不再全量冷启。
                # 会话粘性仍由出站 session 头保证，与此互不影响。
                if body_dict.get("prompt_cache_key") != ids.client:
                    body_dict["prompt_cache_key"] = ids.client
                    body_dirty = True
                attempt_body = (
                    json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
                    if body_dirty
                    else body
                )
                try:
                    response = cx_upstream.responses_request(
                        account,
                        attempt_body,
                        session_id,
                        self._turn_state_for(account.uid, session_id),
                    )
                except crequests.RequestsError as exc:
                    logger.warning(f"[codex] uid={account.uid[:8]}… 传输失败: {exc}")
                    self._feed_breaker(account)
                    attempts += 1
                    exclude.add(account.uid)
                    self._pool.unbind_sticky(session_id)
                    if attempts >= CODEX_ROTATE_ATTEMPTS:
                        raise
                    self._sleep_backoff(attempts)
                    continue

                status = int(response.status_code)
                if 200 <= status < 300:
                    response_headers = dict(response.headers)
                    self._note_success(account)
                    self._note_rate_limit(account, response_headers)
                    self._note_turn_state(account, session_id, response_headers)
                    self._pool.rebind_sticky(session_id, account.uid)
                    return UpstreamResponse(
                        status=status,
                        headers=response_headers,
                        raw=response,
                        account=account.uid,
                    )

                raw = b"".join(chunk for chunk in response.iter_content(chunk_size=8192) if chunk)
                response.close()
                text = raw.decode("utf-8", "replace")
                classified = classify.apply_retry_after(
                    classify.classify(status, text), dict(response.headers)
                )
                logger.warning(
                    f"[codex] uid={account.uid[:8]}… 上游错误 status={status} kind={classified.kind}: "
                    f"{classify.error_message(text)[:160]}"
                )
                self._penalize(account, classified, text)
                last_error = UpstreamResponse(
                    status=status, headers=dict(response.headers), raw=RawBytes(raw),
                    account=account.uid,
                )
                attempts += 1
                # 凭证问题：刷新一次同号重试（刷新成功要清掉刚写的冷却，否则被自己挡住）。
                if classified.kind == classify.AUTH and account.uid not in refreshed:
                    refreshed.add(account.uid)
                    if self._try_refresh(account):
                        self._store.update_runtime(
                            account.uid, cooldown_until_ms=0, cooldown_kind="",
                            consecutive_failures=0,
                        )
                        attempts -= 1  # 同号重试不占换号预算（仍受总 attempts 上限约束）
                        continue
                    exclude.add(account.uid)
                if not classified.retryable or attempts >= CODEX_ROTATE_ATTEMPTS:
                    if classified.retryable:
                        self._pool.unbind_sticky(session_id)
                    return last_error
                exclude.add(account.uid)
                self._pool.unbind_sticky(session_id)
                self._sleep_backoff(attempts)
            finally:
                self._pool.release(account.uid, ids.request)
        if last_error is not None:
            return last_error
        raise UpstreamUnavailable("codex: rotation budget exhausted")

    # ── 记账与罚号 ──

    def _note_rate_limit(self, account: Account, headers: dict[str, str]) -> None:
        """把上游响应头里的限流窗口快照落库（额度列与容量估算的数据源）。

        只在 used_percent 真的动了才写——每个响应都写等于把遥测的写放大一倍。
        顺带把 x-codex-plan-type 同步到 plan_type（订阅档位以服务端为准）。
        """
        snapshot = cx_upstream.parse_rate_limits(headers)
        if not snapshot:
            return
        previous = account.rate_limit or {}
        # 响应头只有窗口三件套，/usage 才给的字段（积分、重置券、上游自己的 allowed /
        # limit_reached）在这里接过来：两条数据源写的是同一个 JSON 快照，直接覆盖等于
        # 一次普通请求就把主动查询的结果抹掉。代价是这些字段的时效跟着上次主动查询走。
        # window 是容量估算的窗口基线，同理不能丢。
        for key in ("allowed", "limit_reached", "credits", "reset_credits", "window"):
            if key in previous and key not in snapshot:
                snapshot[key] = previous[key]
        usage_unchanged = all(
            (previous.get(window) or {}).get("used_percent")
            == (snapshot.get(window) or {}).get("used_percent")
            for window in ("primary", "secondary")
        )
        # 档位变化也要落库：早退条件必须把它算进来，否则用量没动、订阅档位变了时
        # plan_type 永远不同步（响应头是档位的权威来源）。
        plan = str(snapshot.get("plan_type") or "")
        plan_changed = bool(plan) and plan != account.plan_type
        fields: dict[str, Any] = {}
        if not usage_unchanged:
            self._mark_window(account.uid, snapshot, previous)
            fields["rate_limit"] = snapshot
        if plan_changed:
            fields["plan_type"] = plan
        if not fields:
            return
        self._store.update_runtime(account.uid, **fields)
        # 额度用尽：这次快照里 allowed / limit_reached 是合并后的值，用尽就按重置时刻锁住，
        # 别让号池选到它去撞 429（选号池只认冷却，不认百分比）。恢复交给两个入口：
        # 新窗口（_mark_window 里解除）与主动查 /usage（refresh_quota 里解除）。
        if quota.exhausted(snapshot, live=True) and "rate_limit" in fields:
            self._arm_quota_cooldown(account.uid, snapshot)

    def _note_turn_state(self, account: Account, session_id: str, headers: dict[str, str]) -> None:
        """记住本次响应下发的粘性路由令牌（x-codex-turn-state）。

        官方客户端把令牌存在会话上、跟着后续请求回显，换账号即重置——同一段会话
        因此尽量落回同一个后端分片，缓存与路由都受益。网关按「账号 × 会话」存：
        换号轮换后取不到旧令牌，与官方语义一致。
        """
        state = cx_upstream.turn_state_of(headers)
        if not state or not session_id:
            return
        now_ms = int(time.time() * 1000)
        key = f"{account.uid}\x00{session_id}"
        with self._turn_states_lock:
            self._turn_states[key] = (state, now_ms)
            if len(self._turn_states) > _TURN_STATE_MAX_ENTRIES:
                self._prune_turn_states_locked(now_ms)

    def _turn_state_for(self, uid: str, session_id: str) -> str:
        """取该「账号 × 会话」的粘性令牌；过期即清（TTL 与会话粘性同源）。"""
        if not session_id:
            return ""
        key = f"{uid}\x00{session_id}"
        now_ms = int(time.time() * 1000)
        with self._turn_states_lock:
            entry = self._turn_states.get(key)
            if entry is None:
                return ""
            state, at_ms = entry
            if now_ms - at_ms > CODEX_STICKY_TTL_S * 1000:
                self._turn_states.pop(key, None)
                return ""
            return state

    def _prune_turn_states_locked(self, now_ms: int) -> None:
        """清过期；仍超上限就按时间丢最旧的一半（调用方持锁）。"""
        ttl_ms = CODEX_STICKY_TTL_S * 1000
        for key in [k for k, (_, at) in self._turn_states.items() if now_ms - at > ttl_ms]:
            self._turn_states.pop(key, None)
        overflow = len(self._turn_states) - _TURN_STATE_MAX_ENTRIES // 2
        if overflow > 0:
            oldest = sorted(self._turn_states.items(), key=lambda item: item[1][1])[:overflow]
            for key, _ in oldest:
                self._turn_states.pop(key, None)

    def _mark_window(self, uid: str, snapshot: dict[str, Any], previous: dict[str, Any]) -> None:
        """换了窗口就在快照里重立容量估算的基线（累积 token + 当时的百分比）。

        上游只给整数百分比，绝对容量只能靠"本地服务的 token ÷ 同期百分比变化"反推，
        所以基线必须是**窗口内**最早观测到的那个点。窗口的判定看
        reset_at_ms：每个窗口的重置时刻都不同，它变了就是进了新窗口（百分比被清零，
        旧基线直接失效）。基线记的是**累积** token 数，与估算函数读同一口径做差，
        所以不需要知道窗口起点精确在哪。
        """
        primary = snapshot.get("primary")
        if not isinstance(primary, dict):
            return
        reset_at = primary.get("reset_at_ms")
        old = previous.get("window")
        if isinstance(old, dict) and old.get("reset_at_ms") == reset_at:
            return
        snapshot["window"] = {
            "reset_at_ms": reset_at,
            "used_percent": primary.get("used_percent"),
            "tokens": quota.sent_tokens(self._account_totals().get(uid)),
            "at_ms": int(time.time() * 1000),
        }
        # 进了新窗口 = 额度回满，上一轮按重置时刻设的额度冷却必须一并解除，否则一个
        # 已经能用的号会被旧的 cooldown_until_ms 继续挡到那个时刻（可能又是几十天）。
        account = self._store.get_account(uid)
        if account is not None and account.cooldown_kind == QUOTA_COOLDOWN_KIND:
            self._store.update_runtime(uid, cooldown_until_ms=0, cooldown_kind="")

    def _arm_quota_cooldown(self, uid: str, snapshot: dict[str, Any]) -> bool:
        """额度确实用尽 → 按窗口重置时刻挂一条 quota 冷却（额度回满即失效）。

        为什么挂冷却而不是改 status：状态机已经有一套（冷却 / 熔断 / 暂停 / 禁用），
        选号池、界面、运维动作都认它；而"额度用尽"的本质就是"到重置时刻之前都不能用"，
        正是冷却的语义。改 `status='disabled'` 反而要额外想清楚谁来恢复、什么时候恢复。

        判定只认上游的**明确**信号（limit_reached=True / allowed=False）。不用
        "used_percent 到 100" 兜底：实测有号 100% 但仍 allowed=True，那是整数百分比
        四舍五入的边界，一并判死会白白闲置一个还能出量的号。

        重置时刻已过（快照陈旧）或缺失时不动手——那种情况宁可让它按正常路径去试一次，
        撞 429 再退避，也好过凭猜测把一个号锁起来。
        """
        reset_at = quota.reset_at_ms(snapshot)
        now_ms = int(time.time() * 1000)
        if reset_at is None or reset_at <= now_ms:
            return False
        account = self._store.get_account(uid)
        if account is not None and account.cooldown_until_ms >= reset_at:
            return False  # 已经挂到不早于重置时刻的冷却，不必重写
        logger.info(
            f"[codex] uid={uid[:8]}… 额度已用尽, 冷却至重置时刻 "
            f"(约 {(reset_at - now_ms) // 3_600_000} 小时后)"
        )
        self._store.update_runtime(
            uid, cooldown_until_ms=reset_at, cooldown_kind=QUOTA_COOLDOWN_KIND
        )
        return True

    def _clear_quota_cooldown(self, account: Account) -> None:
        """额度恢复可用（上游 allowed=True）时解除 quota 冷却。

        只解除自己挂的那种：auth / waf / 429 软冷却代表别的故障，不该被"额度恢复了"
        顺手清掉。
        """
        if account.cooldown_kind != QUOTA_COOLDOWN_KIND:
            return
        logger.info(f"[codex] uid={account.uid[:8]}… 额度已恢复, 解除额度冷却")
        self._store.update_runtime(account.uid, cooldown_until_ms=0, cooldown_kind="")

    @staticmethod
    def _account_totals() -> dict[str, dict[str, Any]]:
        """requests 库的账号维度聚合（容量估算的 token 来源）。

        放 provider 里取是为了让前端与估算读同一份数据：codex 的 requests 表由
        gateway 的 ProviderStore 持有（db.store("codex")），与账号库是两个连接。
        """
        from db import store as db_store

        try:
            return db_store("codex").account_totals()
        except KeyError:  # 库还没初始化（离线测试/工具脚本）
            return {}

    def _penalize(self, account: Account, classified: classify.Classified, text: str) -> None:
        now_ms = int(time.time() * 1000)
        fields: dict[str, Any] = {
            "last_error": f"{classified.kind}: {classify.error_message(text)[:160]}",
        }
        kind = classified.kind
        if kind in _NO_PENALTY_KINDS:
            self._store.update_runtime(account.uid, **fields)
            return
        # 统一读一次库里的最新连败计数：并发请求下 account 快照可能过期，用快照会让
        # AUTH 路径少算连败、够不到熔断阈值（同一函数内两条分支取数口径必须一致）。
        current = self._store.get_account(account.uid)
        if current is not None and current.status != STATUS_ACTIVE:
            # 号已经被别的路径处置过（刷新遭拒/人工停用）：只留错误证据，不再往它身上
            # 写冷却与连败——那会把更具体的 status_reason（"要重新授权"）覆盖成笼统文案，
            # 而停用的号本来也不会再被派出去。
            self._store.update_runtime(account.uid, **fields)
            return
        failures = max((current or account).consecutive_failures, 0)
        if kind == classify.RATE_LIMIT:
            if classified.retry_after_s > 0:
                cooldown_s = classified.retry_after_s
            else:
                cooldown_s = min(
                    CODEX_COOLDOWN_SOFT_BASE * (2 ** min(failures, 8)), CODEX_COOLDOWN_SOFT_MAX
                )
            until_ms = now_ms + int(cooldown_s * 1000)
            kind_out = "soft"
            # 额度用尽的 429 不能按"软冷却 + 指数退避"处理：退避最长 2 小时，而额度的恢复
            # 时刻由窗口重置决定（免费档 30 天）——按退避放回池子，它只会被反复选中、反复
            # 撞同一条错误。判定优先用错误体里的结构化信号（官方同款：
            # error.type=usage_limit_reached 且自带 resets_at），
            # 再退回固定文案与快照里的明确信号。
            state = current or account
            signal = classify.quota_signal(text, now_ms)
            if (
                signal is not None
                or classify.quota_exhausted_message(text)
                or quota.exhausted(state.rate_limit, live=True)
            ):
                # 错误体自带的 resets_at 最直接——新导入、还没跑过任何响应头快照的账号，
                # 首次撞 429 时只有它有；快照里的重置时刻作为回退。
                reset_at = (
                    signal.reset_at_ms if signal is not None else None
                ) or quota.reset_at_ms(state.rate_limit)
                if reset_at is not None and reset_at > until_ms:
                    until_ms = reset_at
                    kind_out = QUOTA_COOLDOWN_KIND
            # 反过来，已有的额度冷却不许被 429 退避**缩短**（同样取更晚的那个）。
            if state.cooldown_kind == QUOTA_COOLDOWN_KIND and state.cooldown_until_ms > until_ms:
                until_ms = state.cooldown_until_ms
                kind_out = QUOTA_COOLDOWN_KIND
            fields["cooldown_kind"] = kind_out
            fields["cooldown_until_ms"] = until_ms
            fields["consecutive_failures"] = failures + 1
        elif kind == classify.AUTH:
            failures += 1
            fields["cooldown_until_ms"] = now_ms + CODEX_COOLDOWN_AUTH * 1000
            fields["cooldown_kind"] = "auth"
            fields["consecutive_failures"] = failures
            if failures >= CODEX_BREAKER_THRESHOLD:
                fields["status"] = "disabled"
                fields["status_reason"] = "auth failed (re-authorize required)"
        elif kind == classify.NOT_FOUND:
            fields["cooldown_until_ms"] = now_ms + CODEX_COOLDOWN_NOT_FOUND * 1000
            fields["cooldown_kind"] = "soft"
        elif kind == classify.WAF:
            fields["cooldown_until_ms"] = now_ms + CODEX_COOLDOWN_WAF * 1000
            fields["cooldown_kind"] = "waf"
        elif kind == classify.SERVER:
            self._feed_breaker(account, fields)
            self._store.update_runtime(account.uid, **fields)
            return
        self._store.update_runtime(account.uid, **fields)

    # ── token 刷新 ──

    def _maybe_refresh(self, account: Account) -> Account:
        """请求路径：到期前主动刷新，返回**本次请求该用**的账号对象。

        刷新成功要重新读库把新凭据带回来：调用方手里那份是刷新前的快照，拿着它
        发请求等于明知过期还去撞 401，之后还会被判 AUTH 再触发一次同号刷新——
        新票立刻被再轮换一次，白白多消耗一轮配额（先落库再把它发布到运行时账号，
        才是正确的顺序）。失败不阻塞本次发送（401 会触发同号重试）。
        """
        now_ms = int(time.time() * 1000)
        if not account.token_expiring(now_ms, CODEX_REFRESH_AHEAD_S * 1000):
            return account
        if not account.refresh_token:
            return account
        if not self._try_refresh(account, quiet=True, wait=True):
            return account
        return self._store.get_account(account.uid) or account

    def _try_refresh(self, account: Account, quiet: bool = False, *, wait: bool = True) -> bool:
        """用 RT 换新 AT 并落库。按账号串行化：并发调用只有一个真的去打上游。

        wait=True（请求路径）阻塞等锁（上限 _REFRESH_LOCK_WAIT_S）；
        wait=False（后台巡检）拿不到锁就跳过，留给下一轮或请求路径。
        拿到锁后**重读库**：并发下别人可能刚刷完，用库里最新的 RT 才不会再轮换一次。

        失败分三档处置（永久 / 不确定 / session 兜底）：
        - 上游明确拒绝（TokenRejected）：票已死，直接停用待人工重新授权；
        - 结果不确定（传输失败/5xx/响应畸形）：可能已经把票轮换走了，也可能没发出，
          不据此判死——留给下一轮试，真死了会以 invalid_grant 收敛到上一档；
        - 换票成功但落库失败：票已经在上游轮换过，库里那份是废票，必须停用（再用
          旧票只会拿到拒绝），且不能让这个写悄悄失败。
        """
        if not account.refresh_token:
            return False
        lock = self._refresh_lock(account.uid)
        if wait:
            acquired = lock.acquire(timeout=_REFRESH_LOCK_WAIT_S)
        else:
            acquired = lock.acquire(blocking=False)
        if not acquired:
            return False
        try:
            current = self._store.get_account(account.uid)
            refresh_token = (current or account).refresh_token
            if not refresh_token:
                return False
            try:
                tokens = oauth.refresh(refresh_token)
            except oauth.TokenRejected as exc:
                reason = f"refresh_token rejected: {exc}"
                # 2026-10-17 案例的高频根因：同一份 auth.json 同时活在第二个系统里
                # （另一个 Codex 客户端/工具），那边的刷新把这里的 RT 轮换掉——导入的
                # 票第一天就死。先把"另一边在跑"这个最可能的原因告诉运维。
                if "invalid_refresh_token" in reason:
                    reason += (
                        "——该 refresh token 已失效或被源系统轮换：确认这个账号是否"
                        "还在 CLIProxyAPI/官方 CLI 等其他系统里运行（那边每刷新一次，"
                        "导入的 RT 即失效）。停掉另一边后在本系统重新授权"
                    )
                self._mark_needs_reauth(account, reason)
                return False
            except Exception as exc:
                level = logger.info if quiet else logger.warning
                level(
                    f"[codex] uid={account.uid[:8]}… token 刷新失败（结果不确定，下轮再试）: "
                    f"{type(exc).__name__}: {exc}"
                )
                return False
            info = oauth.parse_id_token(tokens.id_token)
            try:
                write_with_retry(
                    lambda: self._store.update_tokens(
                        account.uid,
                        tokens.access_token,
                        tokens.refresh_token,
                        tokens.expires_ms(),
                        id_token=tokens.id_token,
                        plan_type=info.plan_type,
                    )
                )
            except Exception as exc:
                self._mark_needs_reauth(
                    account, f"tokens rotated but persist failed: {type(exc).__name__}: {exc}"
                )
                return False
            logger.info(f"[codex] uid={account.uid[:8]}… token 已刷新")
            return True
        finally:
            lock.release()

    def _mark_needs_reauth(self, account: Account, reason: str) -> None:
        """凭证终局：停用该号，等人工重新授权/重新导入。

        不走 auth 冷却的连败阶梯慢慢爬——上游明确拒绝、或新票已经换出来却没落库，
        都是终局证据；留着只会让巡检每 2 分钟白打一次上游、请求路径反复拿死票撞 401。
        UI 靠 status + status_reason 直接看到"要重新授权"，不用去翻日志。
        """
        current = self._store.get_account(account.uid)
        if current is not None and current.status != STATUS_ACTIVE:
            return  # 已经被别的路径处置过（人工停用/别的请求判死），不覆盖既有状态
        logger.error(f"[codex] uid={account.uid[:8]}… 需要重新授权: {reason}")
        self._store.update_runtime(
            account.uid,
            status=STATUS_DISABLED,
            status_reason=reason[:200],
            last_error=reason[:200],
            cooldown_until_ms=0,
            cooldown_kind="",
        )

    def refresh_expiring(self) -> int:
        """后台巡检：只刷「快过期」的号，返回实际刷新数。

        取舍：**不做全量刷新**——全量刷会让每个号的 refresh_token
        周期性轮换（上游可能因此判失效），也白打上游请求。所以只在距过期不足
        CODEX_REFRESH_AHEAD_S 时才动（与请求路径同一个门槛，巡检才真正替请求
        挡掉同步刷新）。

        跳过：非 active、无 refresh_token、以及**非限流类**冷却中的号——auth/waf
        冷却说明刚刷过且失败，每轮再刷等于持续锤上游；限流类冷却（soft）反而要刷，
        好让冷却结束时手里是张新票。熔断中的号照刷（凭证本身没问题，留着待用）。
        """
        now_ms = int(time.time() * 1000)
        ahead_ms = CODEX_REFRESH_AHEAD_S * 1000
        refreshed = 0
        for account in self._store.list_accounts():
            if account.status != STATUS_ACTIVE or not account.refresh_token:
                continue
            # 冷却中的号照样刷 token：soft（429 退避）与 quota（额度等到重置）都不代表
            # 凭证有问题，而额度冷却可能持续几十天——把它排除在外，等于长期不管这些号的
            # token 过期，等额度回来时反而先被 401 挡一道。
            if (
                account.account_cooldown_active(now_ms)
                and account.cooldown_kind not in ("soft", QUOTA_COOLDOWN_KIND)
            ):
                continue
            if not account.token_expiring(now_ms, ahead_ms):
                continue
            # 单号异常不拖垮整轮：巡检一趟要照看全池，一个号撞上意外的库/网络异常
            # 不该让排在它后面的号这轮全被跳过（外层 main 的巡循环只兜住整轮）。
            try:
                if self._try_refresh(account, quiet=True, wait=False):
                    refreshed += 1
            except Exception as exc:
                logger.warning(
                    f"[codex] uid={account.uid[:8]}… 巡检刷新异常: {type(exc).__name__}: {exc}"
                )
        return refreshed

    def refresh_quota(self, uid: str | None = None) -> dict[str, int]:
        """主动拉额度快照落库（额度列的主动数据源，uid 省略 = 全池）。

        与响应头回填互补：响应头只在账号真跑过成功请求后才有数据，刚导入的号
        只能干等。逐账号 GET /usage，单个失败只告警不中断（其余账号照刷）。
        """
        accounts = self._store.list_accounts()
        if uid:
            accounts = [a for a in accounts if a.uid == uid]
        refreshed = failed = 0
        for account in accounts:
            if not account.access_token:
                continue
            try:
                snapshot = cx_upstream.fetch_usage(account)
            except Exception as exc:  # 单号失败不影响其余号
                failed += 1
                logger.warning(
                    f"[codex] uid={account.uid[:8]}… 额度查询失败: {type(exc).__name__}: {exc}"
                )
                continue
            if not snapshot:
                failed += 1
                continue
            # 主动查询是唯一能"提前"建立容量估算基线的时机：响应头回填只在账号真跑过
            # 请求后才有数据，而基线必须立在窗口内尽可能早的位置，越早越准。
            self._mark_window(account.uid, snapshot, account.rate_limit or {})
            fields: dict[str, Any] = {"rate_limit": snapshot}
            plan = str(snapshot.get("plan_type") or "")
            if plan and plan != account.plan_type:
                fields["plan_type"] = plan
            self._store.update_runtime(account.uid, **fields)
            # /usage 是唯一带 allowed / limit_reached 的数据源，所以额度的"用尽"与"恢复"
            # 都在这里判定：用尽按重置时刻锁住（别让号池选到它去撞 429），恢复则解除冷却
            # ——否则一个已经能用的号会被上一轮的冷却继续挡到那个时刻（可能又是几十天）。
            fresh = self._store.get_account(account.uid) or account
            if quota.exhausted(snapshot, live=True):
                self._arm_quota_cooldown(account.uid, snapshot)
            else:
                self._clear_quota_cooldown(fresh)
            refreshed += 1
        return {"refreshed": refreshed, "failed": failed}

    # ── 运维面（管理端点用）──

    def account_summaries(self) -> list[dict[str, Any]]:
        """账号行 + 额度容量估算 + 撑到重置投影。

        估算与投影都按需算（纯函数、无副作用）：容量的输入是"窗口基线的累积 token"
        与"当前累积 token"，两者都从 requests 库现取——比把推算结果写回
        快照更省心，也不会出现"存下来的估算与实际用量脱节"。整表聚合一次，别在循环
        里逐账号查库。投影（能否撑到窗口重置）只读快照里的基线，不查库。
        """
        totals = self._account_totals()
        summaries = []
        for account in self._store.list_accounts():
            item = account.public_dict()
            item["quota_estimate"] = quota.estimate(account.rate_limit, totals.get(account.uid))
            item["quota_projection"] = quota.project_to_reset(account.rate_limit)
            summaries.append(item)
        return summaries

    def export_accounts(self, fmt: str) -> Any:
        """导出账号（cpa / codex2api 两种形状）。"""
        from providers.codex import transfer

        return transfer.export(self._store.list_accounts(), fmt)

    def import_accounts(self, text: str) -> dict[str, Any]:
        """从 cpa / codex2api 形状的 JSON 导入账号。

        宽容解析（单对象 / 数组 / 带 accounts 的封装），逐条校验；坏条目只记进
        errors 不中断整批。落库走 upsert（INSERT OR REPLACE）——与手动导入同口径：
        重新导入即覆盖凭证并清掉旧的冷却/熔断（人工恢复语义）。
        """
        from providers.codex import transfer

        entries = transfer.parse_entries(text)  # 形状不对时抛 ValueError
        imported = updated = 0
        errors: list[str] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            try:
                account = transfer.to_account(entry)
            except ValueError as exc:
                errors.append(f"第 {index} 条：{exc}")
                continue
            if account.uid in seen:
                errors.append(f"第 {index} 条：同批次内 {account.uid} 重复，已跳过")
                continue
            seen.add(account.uid)
            existed = self._store.get_account(account.uid) is not None
            self._store.upsert_account(account)
            if existed:
                updated += 1
            else:
                imported += 1
        logger.info(
            f"[codex] 账号导入完成 total={len(entries)} imported={imported} "
            f"updated={updated} skipped={len(errors)}"
        )
        return {
            "total": len(entries),
            "imported": imported,
            "updated": updated,
            "skipped": len(errors),
            "errors": errors[:20],
        }

    def account_busy(self, uid: str) -> bool:
        return self._pool.in_flight_count(uid) > 0

    def manual_add_account(self, payload: dict[str, Any]) -> Account:
        """手动导入凭证：优先 refresh_token（会立即刷新换取 AT 并解析账号信息）；
        只有 access_token 时需显式给 account_id。"""
        refresh_token = str(payload.get("refresh_token") or "").strip()
        access_token = str(payload.get("access_token") or "").strip()
        if refresh_token:
            try:
                tokens = oauth.refresh(refresh_token)
            except oauth.TokenRejected as exc:
                raise ValueError(f"refresh_token 已被上游拒绝（{exc}）；请重新授权或重新导出") from exc
            except Exception as exc:
                raise ValueError(f"refresh_token 校验失败: {exc}") from exc
            info = oauth.parse_id_token(tokens.id_token)
            account = Account(
                uid=oauth.account_uid(info) or str(payload.get("uid") or "").strip(),
                account_id=info.account_id or str(payload.get("account_id") or "").strip(),
                label=str(payload.get("label") or info.email or ""),
                email=info.email,
                plan_type=info.plan_type,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token or refresh_token,
                id_token=tokens.id_token,
                token_expires_ms=tokens.expires_ms(),
            )
        elif access_token:
            account_id = str(payload.get("account_id") or "").strip()
            if not account_id:
                raise ValueError("只提供 access_token 时必须同时给 account_id")
            account = Account(
                uid=account_id,
                account_id=account_id,
                label=str(payload.get("label") or ""),
                email=str(payload.get("email") or ""),
                plan_type=str(payload.get("plan_type") or ""),
                access_token=access_token,
                token_expires_ms=0,
            )
        else:
            raise ValueError("必须提供 refresh_token 或 access_token")
        if not account.uid:
            raise ValueError("无法确定账号标识（refresh_token 未带 account_id/email）")
        try:
            write_with_retry(lambda: self._store.upsert_account(account))
        except Exception as exc:
            # 上面那次兑换已经把这枚一次性 RT 用掉了：没落库就等于票丢了，
            # 必须明说让用户重新导出一份，不能报个笼统的"保存失败"。
            raise ValueError(
                f"凭据已换新但保存失败（{type(exc).__name__}: {exc}）；"
                "本次兑换已作废原 refresh_token，请重新导出/授权后再导入"
            ) from exc
        return account

    def account_action(self, uid: str, action: str) -> dict[str, Any]:
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        if action == "pause":
            self._store.update_runtime(uid, status="paused", status_reason="manual")
        elif action == "resume":
            self._store.update_runtime(uid, status="active", status_reason="")
        elif action == "disable":
            self._store.update_runtime(uid, status="disabled", status_reason="manual")
        elif action == "revive":
            self.revive_account(uid)
        elif action == "refresh":
            if not self._try_refresh(account):
                raise RuntimeError("token 刷新失败（凭证可能已失效，请重新授权）")
        elif action == "quota":
            result = self.refresh_quota(uid)
            if result["failed"] and not result["refreshed"]:
                raise RuntimeError("额度查询失败（凭证或上游不可用）")
        else:
            raise ValueError(f"未知动作: {action}")
        updated = self._store.get_account(uid)
        return updated.public_dict() if updated else {}

    def login_start(self) -> dict[str, Any]:
        return self._login.start()

    def login_poll(self, state: str) -> dict[str, Any]:
        return self._login.poll(state)

    def login_cancel(self, session_id: str) -> bool:
        return self._login.cancel(session_id)

    def login_exchange(self, session_id: str, callback: str, state: str = "") -> dict[str, Any]:
        return self._login.exchange(session_id, callback, state)

    def pool_snapshot(self) -> dict[str, Any]:
        return self._pool.snapshot()
