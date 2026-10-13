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
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from providers.capabilities import capability_block
from providers.codex import classify, oauth, shape
from providers.codex import upstream as cx_upstream
from providers.codex.pool import Pool
from providers.codex.store import STATUS_ACTIVE, Account, CodexStore, default_store
from providers.identity import derive_request_ids

_BEIJING_TZ = timezone(timedelta(hours=8))
# 请求路径等刷新锁的上限（秒）：另一个刷新在途时最多等这么久，超时按失败处理
_REFRESH_LOCK_WAIT_S = 30.0
# 不罚号类别（请求级错误：换号无用，直接透传）
_NO_PENALTY_KINDS = frozenset({classify.CLIENT})


@dataclass
class _Catalog:
    """模型目录快照（多账号可见模型的并集）。"""

    models: dict[str, dict[str, Any]] = field(default_factory=dict)  # 裸名 → 条目
    refreshed_at: str | None = None


def _core_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """上游 manifest 条目 → 统一核心字段条目（目录与 models.json 快照共用，
    见 models_snapshot.core_entry）。manifest 里的 base_instructions 等大块字段
    目录侧用不到（ModelInfo 只取名字），不进内存也不落盘。"""
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
    )


class CodexProvider:
    name = "codex"
    upstream_url = CODEX_BASE

    def __init__(self, store: CodexStore | None = None):
        self._store = store or default_store()
        self._pool = Pool(self._store)
        self._lock = threading.Lock()
        self._catalog = _Catalog()
        # 按账号串行化 token 刷新：refresh_token 会轮换（上游可能换发新 RT），
        # 「请求路径按需刷」与「后台巡检」并发刷同一个号会造成 RT 双花。
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._refresh_locks_guard = threading.Lock()
        from providers.codex.login import LoginManager

        self._login = LoginManager(self._store, on_account_added=self._on_account_added)

    # ── 目录 ──

    def refresh(self) -> None:
        """逐账号拉模型目录并合并（各账号可见集合可能不同）；失败保留旧册。"""
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
        if merged or (seen_account and errors == 0):
            with self._lock:
                self._catalog = _Catalog(
                    models=merged,
                    refreshed_at=datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                )
            # 目录里存的就是统一核心字段条目（见 _core_entry），直接落快照；
            # 全失败时保留旧快照
            models_snapshot.write("codex", list(merged.values()))
            logger.info(f"[codex] 模型目录刷新完成 models={len(merged)} errors={errors}")
            return
        if not seen_account:
            logger.info("[codex] 无可用账号, 跳过模型目录刷新")
        else:
            logger.warning(f"[codex] 模型目录刷新全失败, 保留旧目录 errors={errors}")
        self._load_catalog_from_snapshot()

    def _load_catalog_from_snapshot(self) -> None:
        """冷启动兜底：内存里还没有目录时，用上次落盘的快照填充。

        场景：网关刚起、账号凭证尚未生效（或上游不可达）——此时目录为空，管理台的
        模型列表与 `/v1/models` 都是空的，客户端拿不到可服务的模型名。快照是上次
        成功拉取的统一核心字段条目（id/name 足够重建列表），拿它兜底比空着强；
        真正拉到新目录后会被整体覆盖。

        只在"当前为空"时填充，所以不会把陈旧快照盖到已刷新出来的新目录上。
        """
        with self._lock:
            if self._catalog.models:
                return
        raw = models_snapshot.read().get("codex")
        if not isinstance(raw, list) or not raw:
            return
        models: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if model_id:
                models[model_id] = entry
        if not models:
            return
        with self._lock:
            if not self._catalog.models:
                # refreshed_at 留空：这是上次的快照，不是本次刷新结果，不冒充新鲜度
                self._catalog = _Catalog(models=models, refreshed_at=None)
        logger.info(f"[codex] 目录冷启动兜底, 用上次快照填充 models={len(models)}")

    def status(self) -> dict[str, Any]:
        with self._lock:
            models = len(self._catalog.models)
            refreshed_at = self._catalog.refreshed_at
        counts = self._store.count_by_status()
        now_ms = int(time.time() * 1000)
        accounts = self._store.list_accounts()
        return {
            "upstream": CODEX_BASE,
            "refreshed_at": refreshed_at,
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
        # 数值字段（上下文 / 最大输出）先落一份，再让 models.dev 补 manifest 没声明的
        # ——Codex manifest 整个 schema 就没有输出上限字段。厂商给了值就不动，
        # 补来的值在 metadata 里标注来源，展示层据此提示「第三方」。
        metadata: dict[str, Any] = {
            "name": name,
            "context_window": entry.get("context_window"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "input_modalities": entry.get("input_modalities"),
        }
        filled = models_dev.fill_limits(bare, metadata)
        if filled:
            metadata["limits_source"] = {key: "models.dev" for key in filled}
        # 能力块按目录逐条声明，不写死：manifest 里 input_modalities /
        # supported_reasoning_levels / tool_mode 都是逐模型的，写死会让 /v1/models
        # 对每个模型报同一套能力。没声明的键省略，落回档案默认。
        capabilities = capability_block(
            profiles.CODEX,
            "responses",
            {
                "input_modalities": entry.get("input_modalities"),
                "output_modalities": entry.get("output_modalities"),
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

    def derive_ids(self, headers: dict[str, str], body: dict[str, Any]) -> RequestIDs:
        return derive_request_ids(headers, body)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        # 会话相关的 prompt_cache_key 不在这里注入（shape 阶段还不知道会话）——
        # 放在 send() 里用 ids.session 注入，与出站 session 头保持同一个值。
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
        """轮换主循环（见模块 docstring）。stream 参数为客户端意图——打上游恒流式。"""
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
                self._maybe_refresh(account)
                # prompt_cache_key 用会话 id（与出站 session 头同值）：缓存按会话隔离。
                if body_dict.get("prompt_cache_key") != session_id:
                    body_dict["prompt_cache_key"] = session_id
                    body_dirty = True
                attempt_body = (
                    json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
                    if body_dirty
                    else body
                )
                try:
                    response = cx_upstream.responses_request(account, attempt_body, session_id)
                except crequests.RequestsError as exc:
                    logger.warning(f"[codex] uid={account.uid[:8]}… 传输失败: {exc}")
                    self._feed_breaker(account)
                    self._bump_used_seq(account)
                    attempts += 1
                    exclude.add(account.uid)
                    self._pool.unbind_sticky(session_id)
                    if attempts >= CODEX_ROTATE_ATTEMPTS:
                        raise
                    self._sleep_backoff(attempts)
                    continue

                status = int(response.status_code)
                if 200 <= status < 300:
                    self._note_success(account)
                    self._note_rate_limit(account, dict(response.headers))
                    self._pool.rebind_sticky(session_id, account.uid)
                    return UpstreamResponse(
                        status=status,
                        headers=dict(response.headers),
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
                self._bump_used_seq(account)
                last_error = UpstreamResponse(
                    status=status, headers=dict(response.headers), raw=_RawBytes(raw),
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

    def _bump_used_seq(self, account: Account) -> None:
        self._store.update_runtime(account.uid, used_seq=self._store.next_used_seq())

    def _sleep_backoff(self, attempts: int) -> None:
        time.sleep(CODEX_BACKOFF_BASE_S * attempts + random.uniform(0, 0.25))

    def _note_success(self, account: Account) -> None:
        self._store.update_runtime(
            account.uid, breaker_count=0, breaker_until_ms=0, consecutive_failures=0
        )

    def _note_rate_limit(self, account: Account, headers: dict[str, str]) -> None:
        """把上游响应头里的限流窗口快照落库（额度列的数据源）。

        只在 used_percent 真的动了才写——每个响应都写等于把遥测的写放大一倍。
        顺带把 x-codex-plan-type 同步到 plan_type（订阅档位以服务端为准）。
        """
        snapshot = cx_upstream.parse_rate_limits(headers)
        if not snapshot:
            return
        previous = account.rate_limit or {}
        if all(
            (previous.get(window) or {}).get("used_percent")
            == (snapshot.get(window) or {}).get("used_percent")
            for window in ("primary", "secondary")
        ):
            return
        fields: dict[str, Any] = {"rate_limit": snapshot}
        plan = str(snapshot.get("plan_type") or "")
        if plan and plan != account.plan_type:
            fields["plan_type"] = plan
        self._store.update_runtime(account.uid, **fields)

    def _penalize(self, account: Account, classified: classify.Classified, text: str) -> None:
        now_ms = int(time.time() * 1000)
        fields: dict[str, Any] = {
            "last_error": f"{classified.kind}: {classify.error_message(text)[:160]}",
        }
        kind = classified.kind
        if kind in _NO_PENALTY_KINDS:
            self._store.update_runtime(account.uid, **fields)
            return
        if kind == classify.RATE_LIMIT:
            current = self._store.get_account(account.uid)
            failures = max(current.consecutive_failures if current else 0, 0)
            if classified.retry_after_s > 0:
                cooldown_s = classified.retry_after_s
            else:
                cooldown_s = min(
                    CODEX_COOLDOWN_SOFT_BASE * (2 ** min(failures, 8)), CODEX_COOLDOWN_SOFT_MAX
                )
            fields["cooldown_until_ms"] = now_ms + int(cooldown_s * 1000)
            fields["cooldown_kind"] = "soft"
            fields["consecutive_failures"] = failures + 1
        elif kind == classify.AUTH:
            failures = account.consecutive_failures + 1
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

    def _feed_breaker(self, account: Account, fields: dict[str, Any] | None = None) -> None:
        now_ms = int(time.time() * 1000)
        current = self._store.get_account(account.uid)
        count = (current.breaker_count if current else account.breaker_count) + 1
        updates: dict[str, Any] = {"breaker_count": count}
        if count >= CODEX_BREAKER_THRESHOLD:
            over = count - CODEX_BREAKER_THRESHOLD
            duration_s = min(CODEX_BREAKER_BASE_S * (2 ** over), CODEX_BREAKER_MAX_S)
            updates["breaker_until_ms"] = now_ms + int(duration_s * 1000)
        if fields is not None:
            fields.update(updates)
            return
        self._store.update_runtime(account.uid, **updates)

    # ── token 刷新 ──

    def _refresh_lock(self, uid: str) -> threading.Lock:
        with self._refresh_locks_guard:
            lock = self._refresh_locks.get(uid)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[uid] = lock
            return lock

    def _maybe_refresh(self, account: Account) -> None:
        """请求路径：到期前主动刷新；失败不阻塞本次发送（会以 401 触发同号重试）。

        这里 wait=True——同号并发请求应当等前一个刷完拿新凭据，而不是拿着快过期的
        旧 token 打上游（那大概率 401）。等待有上限，超时按失败处理。
        """
        now_ms = int(time.time() * 1000)
        if not account.token_expiring(now_ms, CODEX_REFRESH_AHEAD_S * 1000):
            return
        if not account.refresh_token:
            return
        self._try_refresh(account, quiet=True, wait=True)

    def _try_refresh(self, account: Account, quiet: bool = False, *, wait: bool = True) -> bool:
        """用 RT 换新 AT 并落库。按账号串行化：并发调用只有一个真的去打上游。

        wait=True（请求路径）阻塞等锁（上限 _REFRESH_LOCK_WAIT_S）；
        wait=False（后台巡检）拿不到锁就跳过，留给下一轮或请求路径。
        拿到锁后**重读库**：并发下别人可能刚刷完，用库里最新的 RT 才不会再轮换一次。
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
            except Exception as exc:
                level = logger.info if quiet else logger.warning
                level(
                    f"[codex] uid={account.uid[:8]}… token 刷新失败: "
                    f"{type(exc).__name__}: {exc}"
                )
                return False
            info = oauth.parse_id_token(tokens.id_token)
            self._store.update_tokens(
                account.uid,
                tokens.access_token,
                tokens.refresh_token,
                tokens.expires_ms(),
                id_token=tokens.id_token,
                plan_type=info.plan_type,
            )
            logger.info(f"[codex] uid={account.uid[:8]}… token 已刷新")
            return True
        finally:
            lock.release()

    def refresh_expiring(self) -> int:
        """后台巡检：只刷「快过期」的号，返回实际刷新数。

        取舍照 codex2api：**不做全量刷新**——全量刷会让每个号的 refresh_token
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
            if account.account_cooldown_active(now_ms) and account.cooldown_kind != "soft":
                continue
            if not account.token_expiring(now_ms, ahead_ms):
                continue
            if self._try_refresh(account, quiet=True, wait=False):
                refreshed += 1
        return refreshed

    # ── 运维面（管理端点用）──

    def account_summaries(self) -> list[dict[str, Any]]:
        return [a.public_dict() for a in self._store.list_accounts()]

    def export_accounts(self, fmt: str) -> Any:
        """导出账号（cpa / codex2api 形状，见 providers/codex/transfer.py）。"""
        from providers.codex import transfer

        return transfer.export(self._store.list_accounts(), fmt)

    def import_accounts(self, text: str) -> dict[str, Any]:
        """从 CPA / codex2api 形状的 JSON 导入账号。

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
        self._store.upsert_account(account)
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
        else:
            raise ValueError(f"未知动作: {action}")
        updated = self._store.get_account(uid)
        return updated.public_dict() if updated else {}

    def revive_account(self, uid: str) -> bool:
        """人工恢复：清禁用/冷却/熔断/连败。"""
        self._store.update_runtime(
            uid, status="active", status_reason="", cooldown_until_ms=0,
            cooldown_kind="", breaker_count=0, breaker_until_ms=0, consecutive_failures=0,
        )
        return True

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

    def _on_account_added(self, uid: str) -> None:
        try:
            self.refresh()
        except Exception as exc:
            logger.warning(f"[codex] 新账号目录刷新失败: {exc}")


class _RawBytes:
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
