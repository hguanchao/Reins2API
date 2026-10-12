"""WorkBuddy Provider：Provider 协议实现 + 单请求轮换主循环。

M1 形态：单账号直连（选号 = 最小 used_seq 的健康号），token 过期前主动刷新、
session 失效刷新一次重试、错误分类罚号（冷却/熔断落 accounts 表）。M2 换
pool.py 的加权选号 + 会话粘性，send 主循环结构不变。

send 契约（D3）：网关把 send 当原子操作——只在拿到非 2xx（还没给客户端发过
一个字节）时轮换，流一旦开始不换号；恒 stream=True 打上游（D4），非流式
客户端由网关 collapse_stream 折叠。
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
    WB_BACKOFF_BASE_S,
    WB_BREAKER_BASE_S,
    WB_BREAKER_MAX_S,
    WB_BREAKER_THRESHOLD,
    WB_CHECKIN_STAGGER_MS,
    WB_COOLDOWN_404,
    WB_COOLDOWN_SOFT_BASE,
    WB_COOLDOWN_SOFT_MAX,
    WB_COOLDOWN_WAF,
    WB_ROTATE_ATTEMPTS,
    WB_SESSION_INVALID_LIMIT,
    WB_UPSTREAM_BASE,
)
from gateway.base import (
    ModelInfo,
    ModelNotFound,
    RequestIDs,
    Route,
    UpstreamResponse,
    UpstreamUnavailable,
)
from providers.opencode.identity import derive_request_ids  # 通用会话派生（复用）
from providers.workbuddy import checkin, classify, dsml, shape
from providers.workbuddy import upstream as wb_upstream
from providers.workbuddy.pool import Pool
from providers.workbuddy.store import (
    STATUS_ACTIVE,
    Account,
    WorkBuddyStore,
    default_store,
)

_BEIJING_TZ = timezone(timedelta(hours=8))
# token 到期前主动刷新窗口(对齐 wb2api keepalive 语义)
_REFRESH_AHEAD_MS = 10 * 60 * 1000
# 不罚号类别(classify.RETRYABLE 之外且非 CLIENT 透传类的请求级错误)
_NO_PENALTY_KINDS = frozenset({
    classify.PROMPT_TOO_LONG, classify.IMAGE_INVALID,
    classify.CONTENT_BLOCKED, classify.BAD_PARAMS, classify.CLIENT,
})

# ── 模型别名前缀 ↔ realm(客户端规范名 cn:/glb:,与 gateway.base.ALIAS_PREFIXES 一致)──
_ALIAS_REALM = {"cn": "cn", "glb": "global"}
_REALM_ALIAS = {"cn": "cn", "global": "glb"}


@dataclass
class _Catalog:
    """单 realm 的目录快照(模型与 realm 独立:CN/global 两套上游各自成册)。"""

    models: dict[str, dict[str, Any]] = field(default_factory=dict)      # 裸名 → 条目
    efforts: dict[str, list[str]] = field(default_factory=dict)          # 裸名 → supportedEfforts
    default_efforts: dict[str, str] = field(default_factory=dict)        # 裸名 → defaultEffort
    refreshed_at: str | None = None


def _parse_prefix(model: str) -> tuple[str | None, str]:
    """剥别名前缀 → (realm, 裸名)。

    `cn:xxx` / `glb:xxx` → (cn/global, xxx);其余(裸名、workbuddy/xxx、无关冒号名)
    原样返回,realm 视作未声明(None),由 WorkBuddyProvider._parse 再兜底。"""
    if ":" in model:
        head, tail = model.split(":", 1)
        realm = _ALIAS_REALM.get(head)
        if realm is not None:
            return realm, tail
    return None, model


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


class WorkBuddyProvider:
    name = "workbuddy"
    upstream_url = WB_UPSTREAM_BASE

    def __init__(self, store: WorkBuddyStore | None = None):
        self._store = store or default_store()
        self._pool = Pool(self._store)
        self._lock = threading.Lock()
        # 目录按 realm 分册:CN/global 是两套独立上游,模型表各自成册(见 upstream chat_base)。
        self._catalogs: dict[str, _Catalog] = {"cn": _Catalog(), "global": _Catalog()}
        from providers.workbuddy.login import LoginManager

        self._login_manager = LoginManager(self._store, on_account_added=self._on_account_added)

    # ── 目录 ──

    def refresh(self) -> None:
        """拉每个 realm 的模型目录(各需一个该域可用账号的 Bearer);失败保留旧册。"""
        seen_realm = False
        for realm in ("cn", "global"):
            account = self._any_active_account(realm)
            if account is None:
                continue  # 该域无账号:保留旧册(cn 无账号时也不清空)
            seen_realm = True
            try:
                entries = wb_upstream.fetch_models(account)
            except Exception as exc:
                logger.warning(
                    f"[workbuddy] {realm} 模型目录刷新失败(保留旧目录): "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            models: dict[str, dict[str, Any]] = {}
            efforts: dict[str, list[str]] = {}
            default_efforts: dict[str, str] = {}
            for entry in entries:
                model_id = str(entry.get("id") or "").strip()
                if not model_id:
                    continue
                models[model_id] = entry
                reasoning = entry.get("reasoning")
                if isinstance(reasoning, dict):
                    supported = reasoning.get("supportedEfforts")
                    if isinstance(supported, list) and supported:
                        efforts[model_id] = [str(e) for e in supported]
                    default = str(reasoning.get("defaultEffort") or reasoning.get("effort") or "")
                    if default:
                        default_efforts[model_id] = default
            with self._lock:
                self._catalogs[realm] = _Catalog(
                    models=models, efforts=efforts, default_efforts=default_efforts,
                    refreshed_at=datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                )
            logger.info(f"[workbuddy] {realm} 模型目录刷新完成 models={len(models)}")
        if not seen_realm:
            logger.info("[workbuddy] 无可用账号,跳过模型目录刷新")

    def _any_active_account(self, realm: str | None = None) -> Account | None:
        for account in self._store.list_accounts():
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            if realm is None or account.realm == realm:
                return account
        return None

    def _catalog(self, realm: str) -> _Catalog:
        return self._catalogs.get(realm) or self._catalogs["cn"]

    def _realm_aliases(self, bare: str) -> list[str]:
        """裸名在哪些 realm 的目录里存在 → 其客户端别名(按 cn 先于 glb 排)。"""
        out: list[str] = []
        for realm in ("cn", "global"):
            if bare in self._catalog(realm).models:
                out.append(f"{_REALM_ALIAS[realm]}:{bare}")
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            per_realm = {
                realm: {"models": len(cat.models), "refreshed_at": cat.refreshed_at}
                for realm, cat in self._catalogs.items()
            }
        counts = self._store.count_by_status()
        now_ms = int(time.time() * 1000)
        accounts = self._store.list_accounts()
        return {
            "upstream": WB_UPSTREAM_BASE,
            "refreshed_at": per_realm["cn"]["refreshed_at"],
            "models": per_realm["cn"]["models"] + per_realm["global"]["models"],
            "realms": per_realm,
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
        out: list[ModelInfo] = []
        with self._lock:
            catalogs = {realm: cat for realm, cat in self._catalogs.items()}
        seen: set[str] = set()
        for realm in ("cn", "global"):
            cat = catalogs.get(realm) or _Catalog()
            alias = _REALM_ALIAS[realm]
            for bare, entry in sorted(cat.models.items()):
                if bare in seen:
                    continue  # 同名跨域:首见(cn)已列,metadata.aliases 记全部域
                out.append(self._model_info(bare, entry, alias))
                seen.add(bare)
        return out

    def _model_info(self, bare: str, entry: dict[str, Any], realm_alias: str) -> ModelInfo:
        reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
        return ModelInfo(
            id=f"{realm_alias}:{bare}",
            upstream_model=bare,
            protocol="chat",
            free=False,
            metadata={
                "name": entry.get("name") or bare,
                "realm": "global" if realm_alias == "glb" else "cn",
                "aliases": self._realm_aliases(bare),
                "context_window": entry.get("maxInputTokens") or 0,
                "max_output_tokens": entry.get("maxOutputTokens") or 0,
                "credits": entry.get("credits") or "",
                "tags": entry.get("tags") or [],
                "vendor": entry.get("vendor") or "",
                "supports_images": bool(entry.get("supportsImages")),
                "supports_reasoning": bool(entry.get("supportsReasoning")),
                "supports_tool_call": bool(entry.get("supportsToolCall")),
                "efforts": reasoning.get("supportedEfforts") or [],
                "default_effort": reasoning.get("defaultEffort") or reasoning.get("effort") or "",
                "promo_label": entry.get("promo_label") or "",
                "promo_credits": entry.get("promo_credits") or "",
                "promo_factor": entry.get("promo_factor"),
            },
        )

    # ── 路由 ──

    def supported(self, model: str) -> bool:
        try:
            self.route(model)
            return True
        except ModelNotFound:
            return False

    def route(self, model: str) -> Route:
        realm, bare = self._parse(model)
        cat = self._catalog(realm)
        if bare not in cat.models:
            raise ModelNotFound(f'model "{model}" is not available')
        alias = _REALM_ALIAS[realm]
        return Route(
            provider=self.name, model=f"{alias}:{bare}", upstream_model=bare,
            protocol="chat", free=False, realm=realm,
        )

    def _parse(self, model: str) -> tuple[str, str]:
        """客户端规范名 → (realm, 裸名)。

        cn:/glb: 显式域;workbuddy/xxx 与裸名走老朋友 workbuddy/ 剥离,域按
        「哪个册里有这个模型」判定(cn 优先);都没有则落 cn,由 route 报未知。"""
        realm, bare = _parse_prefix(model)
        if realm is not None:
            return realm, bare
        bare = model.removeprefix(f"{self.name}/")
        if bare in self._catalog("cn").models:
            return "cn", bare
        if bare in self._catalog("global").models:
            return "global", bare
        return "cn", bare

    # ── 请求 ──

    def derive_ids(self, headers: dict[str, str], body: dict[str, Any]) -> RequestIDs:
        return derive_request_ids(headers, body)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        realm, bare = self._parse(str(body.get("model") or ""))
        catalog = self._catalog(realm)
        with self._lock:
            efforts = dict(catalog.efforts)
            default_efforts = dict(catalog.default_efforts)
        # 模型名此刻是客户端规范名(cn:xxx / workbuddy/xxx),efforts 表按裸名索引——先剥再查。
        body["model"] = bare
        changed = shape.shape_workbuddy_body(body, efforts, default_efforts)
        changed = shape.inject_prompt_cache_key(body, "", "") or changed
        return body, changed

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
        conversation_id = ""
        for key in ("conversation_id", "conversationId"):
            value = body_dict.get(key)
            if isinstance(value, str) and value:
                conversation_id = value
                break
        conversation_request_id = wb_upstream.message_id()
        allowed_tools = dsml.tool_name_allowlist(body_dict)
        bare_model = route.upstream_model
        realm = route.realm or "cn"  # 别名前缀声明的域:cn 号只服务 cn 模型,glb 只服务 global

        exclude: set[str] = set()
        refreshed: set[str] = set()
        last_error: UpstreamResponse | None = None
        attempts = 0
        while attempts < WB_ROTATE_ATTEMPTS:
            account = self._pool.pick(bare_model, ids.session, exclude, realm=realm)
            if account is None:
                if last_error is not None:
                    return last_error
                raise UpstreamUnavailable(
                    f"workbuddy: no available {realm} account (all cooling down or disabled)"
                )
            if not self._pool.acquire(account.uid, ids.request):
                exclude.add(account.uid)  # 在途占满：本轮换号
                continue
            try:
                self._maybe_refresh(account)
                # global 域兜底 system 注入（幂等）：首条非 system 时前置中性 system
                attempt_body = body
                if account.realm == "global":
                    shape.ensure_console_system(body_dict)
                    attempt_body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
                try:
                    response = wb_upstream.chat_request(
                        account, attempt_body,
                        conversation_id=conversation_id,
                        conversation_request_id=conversation_request_id,
                    )
                except crequests.RequestsError as exc:
                    logger.warning(f"[workbuddy] uid={account.uid[:8]} 传输失败: {exc}")
                    self._penalize_transport(account)
                    self._bump_used_seq(account)
                    attempts += 1
                    exclude.add(account.uid)
                    self._pool.unbind_sticky(ids.session)
                    if attempts >= WB_ROTATE_ATTEMPTS:
                        raise
                    self._sleep_backoff(attempts)
                    continue

                status = int(response.status_code)
                if 200 <= status < 300:
                    self._note_success(account)
                    self._pool.rebind_sticky(ids.session, account.uid)
                    repair = dsml.enabled_repair(allowed_tools)
                    from providers.workbuddy.streamwrap import SseRepairStream

                    wrapped = SseRepairStream(
                        response.iter_content(chunk_size=8192),
                        repair,
                        on_error_frame=lambda payload, acct=account, model=bare_model:
                            self._note_stream_error(acct, model, payload),
                        closer=response.close,
                    )
                    return UpstreamResponse(
                        status=status,
                        headers=dict(response.headers),
                        raw=wrapped,
                        account=account.uid,
                    )

                # 非 2xx：整包读体（stream 模式下 iter_content 才有数据），分类 → 罚号 → 轮换。
                raw = b"".join(chunk for chunk in response.iter_content(chunk_size=8192) if chunk)
                response.close()
                text = raw.decode("utf-8", "replace")
                classified = classify.apply_retry_after(
                    classify.classify(status, text), dict(response.headers)
                )
                logger.warning(
                    f"[workbuddy] uid={account.uid[:8]} 上游 {status} {classified.kind}: {text[:160]}"
                )
                self._penalize(account, classified, bare_model, text)
                self._bump_used_seq(account)
                last_error = UpstreamResponse(
                    status=status, headers=dict(response.headers), raw=_RawBytes(raw),
                    account=account.uid,
                )
                attempts += 1
                # session 失效：refresh 一次成功则原请求重试（换新 token，同号）。
                # 刷新成功要清掉刚写的冷却/连败计数，否则重试会被自己的冷却挡住。
                if classified.kind == classify.SESSION_DEAD and account.uid not in refreshed:
                    refreshed.add(account.uid)
                    if self._try_refresh(account):
                        self._store.update_runtime(
                            account.uid, cooldown_until_ms=0, cooldown_kind="",
                            consecutive_failures=0,
                        )
                        attempts -= 1  # 同号重试不计入换号预算（仍受总 attempts 上限约束）
                        continue
                    exclude.add(account.uid)
                if not classified.retryable or attempts >= WB_ROTATE_ATTEMPTS:
                    if classified.retryable:
                        self._pool.unbind_sticky(ids.session)
                    return last_error
                exclude.add(account.uid)
                self._pool.unbind_sticky(ids.session)
                self._sleep_backoff(attempts)
            finally:
                self._pool.release(account.uid, ids.request)
        # 预算耗尽：把最后一次上游错误原样透传（无错误则资源不可用）。
        if last_error is not None:
            return last_error
        raise UpstreamUnavailable("workbuddy: rotation budget exhausted")

    # ── 选号已下沉 pool.py（加权随机 + 租约 + 粘性）──

    def _bump_used_seq(self, account: Account) -> None:
        seq = self._store.next_used_seq()
        self._store.update_runtime(account.uid, used_seq=seq)

    def _sleep_backoff(self, attempts: int) -> None:
        delay = WB_BACKOFF_BASE_S * attempts + random.uniform(0, 0.25)
        time.sleep(delay)

    # ── token 刷新 ──

    def _maybe_refresh(self, account: Account) -> None:
        """到期前主动刷新（对齐 wb2api keepalive）；失败不阻塞本次发送。"""
        now_ms = int(time.time() * 1000)
        if account.token_expires_ms and now_ms + _REFRESH_AHEAD_MS < account.token_expires_ms:
            return
        if not account.refresh_token:
            return
        self._try_refresh(account, quiet=True)

    def _try_refresh(self, account: Account, quiet: bool = False,
                     sync_credits: bool = False) -> bool:
        try:
            data = wb_upstream.refresh_request(account)
        except Exception as exc:
            level = logger.info if quiet else logger.warning
            level(f"[workbuddy] uid={account.uid[:8]} token 刷新失败: {type(exc).__name__}: {exc}")
            return False
        expires_ms = wb_upstream.refresh_expires_ms(data.get("expiresIn"))
        self._store.update_tokens(
            account.uid,
            str(data.get("accessToken") or ""),
            str(data.get("refreshToken") or ""),
            expires_ms,
            str(data.get("domain") or ""),
        )
        logger.info(f"[workbuddy] uid={account.uid[:8]} token 已刷新")
        # 积分只在此刻顺带取回（拿到新 token 是唯一时机）：手动刷新同步等结果，
        # 自动刷新走后台线程——billing RPC 的延迟不进聊天路径。
        if sync_credits:
            self._refresh_credits(account.uid, quiet=quiet)
        else:
            self._refresh_credits_async(account.uid)
        return True

    # ── 积分（只在取得新 token 时拉取：登录入库 / 手动导入 / token 刷新）──

    def _refresh_credits_async(self, uid: str) -> None:
        threading.Thread(
            target=self._refresh_credits, args=(uid,), daemon=True,
            name=f"wb-credits-{uid[:8]}",
        ).start()

    def _refresh_credits(self, uid: str, quiet: bool = True) -> None:
        """取积分并落库。失败只记日志，绝不影响刷号/聊天主流程。

        必须从库里重读账号：refresh 刚轮换过 access_token，旧对象带的是已作废凭证。
        空 Accounts（remain/total 双 0，企业号或无资源包）不覆盖——credits 保持
        None（未知）语义，权重按 1.0，credit_floor 也不误伤企业号。
        """
        account = self._store.get_account(uid)
        if account is None:  # 刷新与落库之间账号可能被删
            return
        try:
            remain, total = wb_upstream.fetch_credits(account)
        except Exception as exc:
            level = logger.info if quiet else logger.warning
            level(f"[workbuddy] uid={uid[:8]} 积分查询失败: {type(exc).__name__}: {exc}")
            return
        if remain <= 0 and total <= 0:
            return
        self._store.update_runtime(
            uid, credits_remain=remain, credits_total=total,
            credits_updated_ms=int(time.time() * 1000),
        )

    # ── 签到（billing 域每日一次；接口契约与跳过规则见 checkin.py）──

    def checkin(self, uid: str, *, force: bool = False) -> dict[str, Any]:
        """单账号签到。幂等，自动调度与「立即签到」共用。

        本地台账命中今天即短路，不发任何请求——一天多个触发档靠它压成零成本。
        force 供人工按钮使用，绕过短路交给上游幂等兜底：台账与上游不一致时
        （换机导入、客户端自己领过）以调用上游为准。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        today = checkin.beijing_today()
        if not force and account.checked_in_on(today):
            return {
                "uid": uid, "local": True, "state": checkin.STATE_ALREADY,
                "message": f"{today} 已签到（本地台账）",
                "credit": None, "streak_days": account.checkin_streak,
                "total_credits": account.checkin_total_credits,
                "is_streak_day": False, "http_status": 0,
            }
        # 签到是一条独立路径，不能指望聊天流量顺带续命：到期前先刷新 token
        self._maybe_refresh(account)
        try:
            outcome = checkin.run_checkin(account)
        except checkin.CheckinError as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]} 签到失败: {exc}")
            return {
                "uid": uid, "local": False, "state": checkin.STATE_ERROR,
                "message": str(exc), "credit": None,
                "streak_days": account.checkin_streak,
                "total_credits": account.checkin_total_credits,
                "is_streak_day": False, "http_status": 0,
            }
        # 只有真的对上过上游才落台账：跳过/未开启不写，免得把"没签"记成"已签"
        if outcome.state in (checkin.STATE_CLAIMED, checkin.STATE_ALREADY):
            fields: dict[str, Any] = {
                "last_checkin_day": today,
                "checkin_updated_ms": int(time.time() * 1000),
            }
            if outcome.streak_days is not None:
                fields["checkin_streak"] = outcome.streak_days
            if outcome.total_credits is not None:
                fields["checkin_total_credits"] = outcome.total_credits
            self._store.update_runtime(uid, **fields)
        if outcome.state == checkin.STATE_CLAIMED:
            logger.info(f"[workbuddy] uid={uid[:8]} {outcome.message}")
            # 签到直接改余额：拉一次让面板与选号权重立刻跟上（失败只记日志）
            self._refresh_credits_async(uid)
        return {"uid": uid, "local": False, **outcome.to_dict()}

    def checkin_all(self, *, force: bool = False) -> dict[str, Any]:
        """全池签到：只动 active 号，账号间错峰，串行执行。

        串行而非并发：签到是低频后台动作，"不像机器"比吞吐重要——并发打上游
        既是风控特征，也会和聊天流量抢连接与代理。暂停/禁用的号是人工决定，
        不代签。
        """
        accounts = [a for a in self._store.list_accounts() if a.status == STATUS_ACTIVE]
        items: list[dict[str, Any]] = []
        tally = dict.fromkeys(
            (checkin.STATE_CLAIMED, checkin.STATE_ALREADY, checkin.STATE_SKIPPED,
             checkin.STATE_INACTIVE, checkin.STATE_ERROR),
            0,
        )
        for index, account in enumerate(accounts):
            if index:
                time.sleep(WB_CHECKIN_STAGGER_MS / 1000.0)
            try:
                result = self.checkin(account.uid, force=force)
            except Exception as exc:  # 单号异常不中断整轮
                logger.warning(
                    f"[workbuddy] uid={account.uid[:8]} 签到异常: {type(exc).__name__}: {exc}"
                )
                result = {"uid": account.uid, "state": checkin.STATE_ERROR,
                          "message": f"{type(exc).__name__}: {exc}"}
            state = result.get("state") or checkin.STATE_ERROR
            tally[state] = tally.get(state, 0) + 1
            items.append(result)
        return {
            "total": len(accounts),
            "claimed": tally[checkin.STATE_CLAIMED],
            "already": tally[checkin.STATE_ALREADY],
            "skipped": tally[checkin.STATE_SKIPPED],
            "inactive": tally[checkin.STATE_INACTIVE],
            "failed": tally[checkin.STATE_ERROR],
            "credits": sum(float(item.get("credit") or 0) for item in items),
            "summary": (
                f"共 {len(accounts)} 号：领取 {tally[checkin.STATE_CLAIMED]}，"
                f"已签 {tally[checkin.STATE_ALREADY]}，"
                f"不适用 {tally[checkin.STATE_SKIPPED] + tally[checkin.STATE_INACTIVE]}，"
                f"失败 {tally[checkin.STATE_ERROR]}"
            ),
            "items": items,
        }

    # ── 罚号与记账 ──

    def _note_success(self, account: Account) -> None:
        self._store.update_runtime(
            account.uid, breaker_count=0, breaker_until_ms=0, consecutive_failures=0
        )

    def _penalize_transport(self, account: Account) -> None:
        """传输层失败按 SERVER 口径喂熔断（不写冷却——下一轮换号已排除本号）。"""
        self._feed_breaker(account)

    def _note_stream_error(self, account: Account, model: str, payload: str) -> None:
        """流内 error 帧（6004 限流/内容拦截等真实形态）——罚号依据 D6。"""
        try:
            classified = classify.classify(200, payload)
        except Exception:
            return
        if classified.kind in (classify.NONE,):
            return
        self._penalize(account, classified, model, payload)

    def _penalize(self, account: Account, classified: classify.Classified,
                  model: str, text: str) -> None:
        now_ms = int(time.time() * 1000)
        fields: dict[str, Any] = {
            "last_error": f"{classified.kind}: {text[:160]}",
        }
        kind = classified.kind
        if kind in _NO_PENALTY_KINDS:
            self._store.update_runtime(account.uid, **fields)
            return
        if kind == classify.HARD_CREDIT:
            fields["cooldown_until_ms"] = classify.hard_cooldown_until_ms()
            fields["cooldown_kind"] = "hard"
        elif kind == classify.SOFT_RATE:
            failures = max(self._store.get_account(account.uid).consecutive_failures
                           if self._store.get_account(account.uid) else 0, 0)
            if classified.retry_after_s > 0:
                cooldown_s = classified.retry_after_s
            else:
                cooldown_s = min(
                    WB_COOLDOWN_SOFT_BASE * (2 ** min(failures, 8)), WB_COOLDOWN_SOFT_MAX
                )
            if classified.model_rate_limit:
                # 模型级 6004：只挡该模型（重置墙钟优先），其他模型豁免；账号级不冷却。
                cooldowns = dict(account.model_cooldowns)
                cooldowns[model] = {
                    "until_ms": classified.model_reset_at_ms or now_ms + int(cooldown_s * 1000),
                    "kind": "6004",
                }
                fields["model_cooldowns"] = cooldowns
            else:
                fields["cooldown_until_ms"] = now_ms + int(cooldown_s * 1000)
                fields["cooldown_kind"] = "soft"
        elif kind == classify.NOT_FOUND:
            fields["cooldown_until_ms"] = now_ms + WB_COOLDOWN_404 * 1000
            fields["cooldown_kind"] = "soft"
        elif kind == classify.WAF_BLOCK:
            fields["cooldown_until_ms"] = now_ms + WB_COOLDOWN_WAF * 1000
            fields["cooldown_kind"] = "waf"
        elif kind == classify.SERVER:
            self._feed_breaker(account, fields)
            self._store.update_runtime(account.uid, **fields)
            return
        elif kind in (classify.SESSION_DEAD, classify.ACCOUNT_FAULT):
            failures = account.consecutive_failures + 1
            fields["consecutive_failures"] = failures
            fields["cooldown_until_ms"] = now_ms + WB_COOLDOWN_SOFT_BASE * 1000
            fields["cooldown_kind"] = "soft"
            if kind == classify.SESSION_DEAD and failures >= WB_SESSION_INVALID_LIMIT:
                fields["status"] = "disabled"
                fields["status_reason"] = "session invalid (refresh exhausted)"
        self._store.update_runtime(account.uid, **fields)

    def _feed_breaker(self, account: Account, fields: dict[str, Any] | None = None) -> None:
        now_ms = int(time.time() * 1000)
        current = self._store.get_account(account.uid)
        count = (current.breaker_count if current else account.breaker_count) + 1
        updates: dict[str, Any] = {"breaker_count": count}
        if count >= WB_BREAKER_THRESHOLD:
            over = count - WB_BREAKER_THRESHOLD
            duration_s = min(WB_BREAKER_BASE_S * (2 ** over), WB_BREAKER_MAX_S)
            updates["breaker_until_ms"] = now_ms + int(duration_s * 1000)
        if fields is not None:
            fields.update(updates)
            return
        self._store.update_runtime(account.uid, **updates)

    # ── 运维面（管理端点用）──

    def account_summaries(self) -> list[dict[str, Any]]:
        """脱敏账号列表（统计由 API 层从 requests 表聚合后并入）。"""
        return [a.public_dict() for a in self._store.list_accounts()]

    def manual_add_account(self, payload: dict[str, Any]) -> Account:
        """手动导入凭证（面板粘贴形态）。uid 白名单校验防路径滥用。"""
        from providers.workbuddy.store import valid_uid

        uid = str(payload.get("uid") or "").strip()
        if not valid_uid(uid):
            raise ValueError("uid 必须匹配 [A-Za-z0-9_-] 且长度 ≤ 64")
        realm = str(payload.get("realm") or "cn")
        if realm not in ("cn", "global"):
            raise ValueError("realm 必须为 cn / global")
        account = Account(
            uid=uid,
            label=str(payload.get("label") or ""),
            realm=realm,
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            enterprise_id=str(payload.get("enterprise_id") or ""),
            device_token=str(payload.get("device_token") or ""),
        )
        if not account.access_token and not account.refresh_token:
            raise ValueError("access_token 与 refresh_token 至少填一项")
        self._store.upsert_account(account)  # INSERT OR REPLACE = 重新导入即人工恢复
        # 手动导入 = 一次拿新 token：后台顺带拉积分，不阻塞导入响应
        self._refresh_credits_async(uid)
        return account

    def _on_account_added(self, uid: str) -> None:
        """OAuth 登录完成入库（新 token 时刻）：拉目录 + 后台顺带拉积分。"""
        try:
            self.refresh()
        finally:
            self._refresh_credits_async(uid)

    def account_busy(self, uid: str) -> bool:
        return self._pool.in_flight_count(uid) > 0

    def account_models(self, uid: str) -> list[dict[str, Any]]:
        """按账号实时查询其可用模型（用该账号的 token 打上游 /v3/config）。

        不同账号可见目录可能不同（企业号/试用横幅/账号级 trial），故不做缓存。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        out: list[dict[str, Any]] = []
        for entry in wb_upstream.fetch_models(account):
            reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
            out.append({
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or ""),
                "context_window": int(entry.get("maxInputTokens") or 0),
                "max_output_tokens": int(entry.get("maxOutputTokens") or 0),
                "credits": str(entry.get("credits") or ""),
                "efforts": [str(e) for e in (reasoning.get("supportedEfforts") or [])],
                "tags": [str(t) for t in (entry.get("tags") or [])],
                # 限时优惠（生效价 = 牌价 × factor；badge-only 类无 factor 仅标签）
                "promo_label": str(entry.get("promo_label") or ""),
                "promo_credits": str(entry.get("promo_credits") or ""),
                "promo_factor": entry.get("promo_factor"),
                "promo_note": str(entry.get("promo_note") or ""),
            })
        return out

    def account_action(self, uid: str, action: str) -> dict[str, Any]:
        """账号动作：pause / resume / disable / revive / refresh（顺带拉积分）。"""
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
            # 同步等积分结果：返回的 public_dict 直接带最新余额，前端按钮立即可见
            if not self._try_refresh(account, sync_credits=True):
                raise RuntimeError("token 刷新失败（凭证可能已失效，请重新登录）")
        else:
            raise ValueError(f"未知动作: {action}")
        updated = self._store.get_account(uid)
        return updated.public_dict() if updated else {}

    def login_start(self, realm: str = "cn") -> dict[str, str]:
        return self._login_manager.start(realm)

    def login_poll(self, state: str) -> dict[str, Any]:
        return self._login_manager.poll(state)

    def revive_account(self, uid: str) -> bool:
        """人工恢复：清禁用/冷却/熔断/连败（wb2api Revive 语义）。"""
        self._store.update_runtime(
            uid, status="active", status_reason="", cooldown_until_ms=0,
            cooldown_kind="", breaker_count=0, breaker_until_ms=0,
            consecutive_failures=0,
        )
        return True

    def pool_snapshot(self) -> dict[str, Any]:
        return self._pool.snapshot()
