"""WorkBuddy Provider：Provider 协议实现 + 单请求轮换主循环。

选号已下沉 pool.py（加权随机 + 在途租约 + 会话粘性）：本文件的 send 主循环只负责
token 过期前主动刷新、session 失效刷新一次重试、错误分类罚号（冷却/熔断落
accounts 表）。

send 契约（D3）：网关把 send 当原子操作——只在拿到非 2xx（还没给客户端发过
一个字节）时轮换，流一旦开始不换号；恒 stream=True 打上游（D4），非流式
客户端由网关 collapse_stream 折叠。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as crequests

from core.logger import logger
from core.settings import (
    WB_ACTIVITY_MODEL_ID,
    WB_ACTIVITY_MODEL_NAME,
    WB_ADOPT_REPORT_GAP_MS,
    WB_BACKOFF_BASE_S,
    WB_BREAKER_BASE_S,
    WB_BREAKER_MAX_S,
    WB_BREAKER_THRESHOLD,
    WB_CHECKIN_STAGGER_MS,
    WB_COOLDOWN_404,
    WB_COOLDOWN_SOFT_BASE,
    WB_COOLDOWN_SOFT_MAX,
    WB_COOLDOWN_WAF,
    WB_NEGATIVE_CACHE_BASE_S,
    WB_NEGATIVE_CACHE_MAX_S,
    WB_NEGATIVE_CACHE_SHIFT,
    WB_ROTATE_ATTEMPTS,
    WB_SESSION_INVALID_LIMIT,
    WB_SOFT_RETRY_SLEEP_S,
    WB_UPSTREAM_BASE,
)
from gateway.base import (
    ModelInfo,
    ModelNotFound,
    ModelUnavailable,
    RequestIDs,
    Route,
    UpstreamResponse,
    UpstreamUnavailable,
)
from providers import models_dev, models_snapshot, profiles
from providers.account_runtime import AccountRuntime
from providers.capabilities import as_flag, capability_block
from providers.common import RawBytes, refresh_result, stamp, stamp_now
from providers.identity import derive_request_ids  # 通用会话派生（provider 共享层）
from providers.workbuddy import checkin, classify, dsml, growth, sanitize, shape
from providers.workbuddy import pool as wb_pool
from providers.workbuddy import upstream as wb_upstream
from providers.workbuddy.pool import Pool
from providers.workbuddy.store import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PAUSED,
    Account,
    WorkBuddyStore,
    default_store,
)

# token 到期前主动刷新窗口(对齐 wb2api keepalive 语义)
_REFRESH_AHEAD_MS = 10 * 60 * 1000
# 请求路径等刷新锁的上限（秒）：另一个刷新在途时最多等这么久，超时按失败处理
_REFRESH_LOCK_WAIT_S = 30.0
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


def _snapshot_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """上游 /v3/config 条目 → models.json 统一核心字段（见 models_snapshot.core_entry）。

    倍率/活动/credits 这些上游侧字段进 `extras`：冷启动不再打上游，内存目录只能
    由快照重建，缺了它们重启后 _model_info 的对应列就空着（见 _entry_from_snapshot）。
    """
    reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
    efforts = reasoning.get("supportedEfforts")
    default_effort = str(reasoning.get("defaultEffort") or reasoning.get("effort") or "")
    extras = {
        key: entry.get(key)
        for key in ("credits", "tags", "vendor", "promo_label", "promo_credits",
                    "promo_factor", "promo_note")
        if entry.get(key) is not None
    }
    return models_snapshot.core_entry(
        id=str(entry.get("id") or "").strip() or None,
        name=entry.get("name"),
        description=entry.get("descriptionZh") or entry.get("descriptionEn"),
        context_window=entry.get("maxInputTokens"),
        max_output_tokens=entry.get("maxOutputTokens"),
        supports_images=as_flag(entry.get("supportsImages")),
        supports_reasoning=as_flag(entry.get("supportsReasoning")),
        supports_tool_call=as_flag(entry.get("supportsToolCall")),
        reasoning_efforts=(
            [str(e) for e in efforts] if isinstance(efforts, list) and efforts else None
        ),
        default_effort=default_effort or None,
        protocol="chat",
        extras=extras or None,
    )


def _entry_from_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """快照统一字段条目 → 内存目录条目（_model_info 读的那套上游侧键名）。

    冷启动的逆向投影：把统一字段翻回 `maxInputTokens` / `descriptionZh` /
    `reasoning.supportedEfforts` 等原始键名，并把 extras 原样贴回。语义上等价于
    一次真实刷新的产出（等价性由 tests 的「缓存重建」用例兜住）。
    """
    extras = entry.get("extras") if isinstance(entry.get("extras"), dict) else {}
    out: dict[str, Any] = {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "descriptionZh": entry.get("description"),
        "maxInputTokens": entry.get("context_window"),
        "maxOutputTokens": entry.get("max_output_tokens"),
        "supportsImages": entry.get("supports_images"),
        "supportsReasoning": entry.get("supports_reasoning"),
        "supportsToolCall": entry.get("supports_tool_call"),
    }
    efforts = entry.get("reasoning_efforts")
    default_effort = entry.get("default_effort")
    if efforts or default_effort:
        out["reasoning"] = {"supportedEfforts": efforts, "defaultEffort": default_effort}
    out.update(extras)
    return {key: value for key, value in out.items() if value is not None}


def _as_number(value: Any) -> float | None:
    """上游数值字段 → float；非数值/布尔返回 None（不臆测）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _usage_total_tokens(usage: dict[str, Any]) -> int:
    """usage 里的总 token 数：优先 total_tokens，缺失时用 prompt + completion。"""
    total = _as_number(usage.get("total_tokens"))
    if total is not None and total > 0:
        return int(total)
    prompt = _as_number(usage.get("prompt_tokens"))
    completion = _as_number(usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return 0
    return int(max(prompt, 0.0) + max(completion, 0.0))


def _compact_payload(value: Any, limit: int = 220) -> str:
    """把上游奖品负载压成单行日志文本（形状随活动期变，不做字段假设）。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


class WorkBuddyProvider(AccountRuntime):
    name = "workbuddy"
    upstream_url = WB_UPSTREAM_BASE
    # 熔断与退避（语义见 providers/account_runtime.py）
    breaker_threshold = WB_BREAKER_THRESHOLD
    breaker_base_s = WB_BREAKER_BASE_S
    breaker_max_s = WB_BREAKER_MAX_S
    backoff_base_s = WB_BACKOFF_BASE_S

    def __init__(self, store: WorkBuddyStore | None = None):
        self._store = store or default_store()
        self._pool = Pool(self._store)
        self._lock = threading.Lock()
        # 领养当日失败台账（uid → 北京日期）：门槛未达的号当日不再重试，避免同日多趟
        # 反复撞门槛。进程内即可，重启清零无副作用（参考实现同为内存态）。
        self._adopt_tried: dict[str, str] = {}
        self._adopt_guard = threading.Lock()
        from providers.workbuddy.scheduler import WorkBuddyScheduler

        # 排程器由 main 显式 start()（构造即起线程会让测试/导入副作用起后台任务）
        self.scheduler = WorkBuddyScheduler(self)
        # 目录按 realm 分册:CN/global 是两套独立上游,模型表各自成册(见 upstream chat_base)。
        self._catalogs: dict[str, _Catalog] = {"cn": _Catalog(), "global": _Catalog()}
        # 选号的积分保底要按目录倍率判「这个模型收不收费」（实测台账无观测时的兜底），
        # 池不持有目录，把查询注入进去（参考实现 modelRateOf 同款接线）。
        self._pool.set_model_rate(self._catalog_model_rate)
        # 快照内容的最后变更时间(models.json 的 meta.updated_at_ms);本进程从快照
        # 冷启动时它是唯一的时间线索,与各册的 refreshed_at(本进程刷新时间)分开。
        self._cached_at_ms = 0
        # RT 轮换不能双花：池允许同号多请求在途（WB_MAX_IN_FLIGHT）
        self._init_runtime()
        from providers.workbuddy.login import LoginManager

        # 登录完成只顺带拉积分（见 _on_account_added）：不再自动拉模型目录
        # （目录只在手动「获取模型」时刷新，见 refresh 的 docstring）。
        self._login_manager = LoginManager(self._store, on_account_added=self._on_account_added)

    # ── 目录 ──

    def refresh(self) -> dict[str, Any]:
        """拉每个 realm 的模型目录(各需一个该域可用账号的 Bearer);失败保留旧册。

        返回 {added, removed, failed, error}（手动刷新是常规路径下唯一的触发点，
        失败要能回报给界面，不能只写日志；形状与另两家一致）。
        """
        # 快照按 realm 别名分键(cn/glb);某域本轮没拉到就保留上一次的好数据。
        current = models_snapshot.read().get("workbuddy")
        snapshot_by_alias: dict[str, Any] = {
            "cn": list((current or {}).get("cn") or []) if isinstance(current, dict) else [],
            "glb": list((current or {}).get("glb") or []) if isinstance(current, dict) else [],
        }
        with self._lock:
            previous = {realm: set(cat.models) for realm, cat in self._catalogs.items()}
        seen_realm = False
        added = removed = failed = 0
        errors: list[str] = []
        for realm in ("cn", "global"):
            account = self._any_active_account(realm)
            if account is None:
                continue  # 该域无账号:保留旧册(cn 无账号时也不清空)
            seen_realm = True
            try:
                entries = wb_upstream.fetch_models(account)
            except Exception as exc:
                failed += 1
                errors.append(f"{realm}: {type(exc).__name__}")
                logger.warning(
                    f"[workbuddy] realm={realm} 模型目录刷新失败(保留旧目录): "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            # 空结果护栏：该域本来有目录而上游这次一条都没给（凭证异常/上游抽风），
            # 保留旧册——空目录会让该域的模型全部不可路由。
            if not entries and previous.get(realm):
                failed += 1
                errors.append(f"{realm}: 上游返回空目录")
                logger.warning(
                    f"[workbuddy] realm={realm} 上游返回空目录, 保留旧目录 "
                    f"models={len(previous[realm])}"
                )
                continue
            # 统一核心字段条目(快照用;上游原样条目只进内存目录)
            snapshot_by_alias[_REALM_ALIAS[realm]] = models_snapshot.sort_entries(
                [_snapshot_entry(e) for e in entries]
            )
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
            now = set(models)
            realm_added = sorted(now - previous.get(realm, set()))
            realm_removed = sorted(previous.get(realm, set()) - now)
            added += len(realm_added)
            removed += len(realm_removed)
            with self._lock:
                self._catalogs[realm] = _Catalog(
                    models=models, efforts=efforts, default_efforts=default_efforts,
                    refreshed_at=stamp_now(),
                )
            detail = []
            if realm_added:
                detail.append(f"新增={len(realm_added)}({', '.join(realm_added[:5])})")
            if realm_removed:
                detail.append(f"移除={len(realm_removed)}({', '.join(realm_removed[:5])})")
            logger.info(
                f"[workbuddy] realm={realm} 模型目录刷新完成 models={len(models)} "
                f"{' '.join(detail) or '无变化'}"
            )
        if not seen_realm:
            failed += 1
            errors.append("无可用账号")
            logger.info("[workbuddy] 无可用账号, 跳过模型目录刷新")
        written = models_snapshot.write("workbuddy", snapshot_by_alias)
        self._load_cached_at()
        if written:
            logger.info("[workbuddy] 快照已更新(旧文件轮转为 models.json.bak)")
        return refresh_result(added=added, removed=removed, failed=failed, error="; ".join(errors))

    def load_from_snapshot(self) -> int:
        """用上次落盘的快照填充内存目录（冷启动的唯一数据源），返回条目数。

        手动获取是唯一的常规刷新路径，启动时内存目录只能来自缓存——它是路由依据
        （route() 靠它判模型是否存在），空着等于该域模型全部不可服务。`refreshed_at`
        留空、改用 cached_at 报缓存内容时间，不冒充本次新鲜度。只在内存目录为空时
        填充，不会把旧快照盖到已刷新出来的新目录上。
        """
        raw = models_snapshot.read().get("workbuddy")
        if not isinstance(raw, dict):
            return 0
        total = 0
        with self._lock:
            empty = all(not cat.models for cat in self._catalogs.values())
        if not empty:
            return 0
        restored: dict[str, _Catalog] = {"cn": _Catalog(), "global": _Catalog()}
        for realm in ("cn", "global"):
            entries = raw.get(_REALM_ALIAS[realm])
            if not isinstance(entries, list):
                continue
            models: dict[str, dict[str, Any]] = {}
            efforts: dict[str, list[str]] = {}
            default_efforts: dict[str, str] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                if not model_id:
                    continue
                models[model_id] = _entry_from_snapshot(item)
                supported = item.get("reasoning_efforts")
                if isinstance(supported, list) and supported:
                    efforts[model_id] = [str(e) for e in supported]
                default = item.get("default_effort")
                if isinstance(default, str) and default:
                    default_efforts[model_id] = default
            restored[realm] = _Catalog(
                models=models, efforts=efforts, default_efforts=default_efforts,
                refreshed_at=None,
            )
            total += len(models)
        if not total:
            return 0
        with self._lock:
            if all(not cat.models for cat in self._catalogs.values()):
                self._catalogs = restored
        self._load_cached_at()
        logger.info(f"[workbuddy] 目录从快照恢复 models={total}")
        return total

    def _load_cached_at(self) -> None:
        """把快照的内容变更时间读进内存（status 展示用）。"""
        self._cached_at_ms = int(models_snapshot.read_meta("workbuddy").get("updated_at_ms") or 0)

    def _any_active_account(self, realm: str | None = None) -> Account | None:
        for account in self._store.list_accounts():
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            if realm is None or account.realm == realm:
                return account
        return None

    def _catalog(self, realm: str) -> _Catalog:
        return self._catalogs.get(realm) or self._catalogs["cn"]

    def _catalog_model_rate(self, realm: str, model: str) -> str | None:
        """目录里的模型积分倍率字符串（含活动后的生效倍率），无条目/未知 → None。

        给池的积分保底用：触底号要拦住"收费模型"，无实测观测时以牌价判收费。
        活动期（promo_factor）优先——它才是此刻的真实倍率。
        """
        entry = self._catalog(realm).models.get(model)
        if not isinstance(entry, dict):
            return None
        factor = entry.get("promo_factor")
        if factor is not None:
            return str(factor)
        return str(entry.get("credits") or "")

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
            "cached_at": stamp(self._cached_at_ms),
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
        name = str(entry.get("name") or bare)
        metadata: dict[str, Any] = {
            "name": name,
            "description": entry.get("descriptionZh") or entry.get("descriptionEn") or "",
            "realm": "global" if realm_alias == "glb" else "cn",
            "aliases": self._realm_aliases(bare),
            # 0 = 上游没给（未知）；随后交给 models.dev 兜底补，厂商给了值就不动
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
            "promo_note": entry.get("promo_note") or "",
        }
        filled = models_dev.fill_limits(bare, metadata)
        if filled:
            metadata["limits_source"] = {key: "models.dev" for key in filled}
        # 厂商字段名 → 标准化能力名，只在这一层翻译一次；缺省/None 的字段不输出
        capabilities = capability_block(
            profiles.WORKBUDDY,
            "chat",
            {
                "input_modalities": ["text", "image"] if entry.get("supportsImages") else ["text"],
                "output_modalities": ["text"],
                "supports_reasoning": as_flag(entry.get("supportsReasoning")),
                "supports_function_calling": as_flag(entry.get("supportsToolCall")),
                "context_window": metadata["context_window"] or None,
                "max_output_tokens": metadata["max_output_tokens"] or None,
            },
        )
        return ModelInfo(
            id=f"{realm_alias}:{bare}",
            upstream_model=bare,
            protocol="chat",
            free=False,
            display_name=name,
            capabilities=capabilities,
            metadata=metadata,
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

    def derive_ids(
        self, headers: dict[str, str], body: dict[str, Any], client_ip: str = ""
    ) -> RequestIDs:
        return derive_request_ids(headers, body, client_ip)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        realm, bare = self._parse(str(body.get("model") or ""))
        catalog = self._catalog(realm)
        with self._lock:
            efforts = dict(catalog.efforts)
            default_efforts = dict(catalog.default_efforts)
        # 模型名此刻是客户端规范名(cn:xxx / workbuddy/xxx),efforts 表按裸名索引——先剥再查。
        body["model"] = bare
        changed = shape.shape_workbuddy_body(body, efforts, default_efforts)
        # prompt_cache_key 不在这里注入：缓存键按账号硬隔离，而账号要到 send() 选号后
        # 才知道（shape 阶段 uid 为空会让键退化成全局常量，跨账号复用同一前缀缓存）。
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
        # 出站脱敏：上游按逐字匹配拦整单（400 11128 unapproved channel），历史每轮
        # 重发 ⇒ 会话里只要出现过指纹就是粘性 400。这里洗一次覆盖全部换号重试与
        # 三种客户端协议（词表与实测记录见 tests/upstream_evidence.md）。
        if sanitize.sanitize_body(body_dict):
            body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
            logger.warning("[workbuddy] 出站脱敏: 请求体命中上游审核指纹, 已改写后转发")
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
        soft_retried: set[str] = set()
        last_error: UpstreamResponse | None = None
        attempts = 0
        body_dirty = False
        while attempts < WB_ROTATE_ATTEMPTS:
            account = self._pool.pick(bare_model, ids.session, exclude, realm=realm)
            if account is None:
                if last_error is not None:
                    return last_error
                blocked = self._pool.model_blocked(bare_model)
                if blocked.get("blocked"):
                    # 号都在、只是都不服务这个模型：报模型不可用（400，不可重试），
                    # 别伪装成"没有可用账号"——后者会让客户端退避重试同一个模型。
                    hint = f"model_blocked: {blocked['count']} 个账号对该模型处于冷却"
                    if blocked.get("until_ms"):
                        hint += (
                            "，最早解封 "
                            + time.strftime("%Y-%m-%d %H:%M", time.localtime(
                                blocked["until_ms"] / 1000
                            ))
                        )
                    if blocked.get("reason"):
                        hint += f"；上游原文: {blocked['reason']}"
                    raise ModelUnavailable(
                        f"model {bare_model} is unavailable on every account "
                        f"(per-model cooldown), try another model",
                        hint=hint,
                    )
                raise UpstreamUnavailable(
                    f"workbuddy: no available {realm} account (all cooling down or disabled)"
                )
            if not self._pool.acquire(account.uid, ids.request):
                exclude.add(account.uid)  # 在途占满：本轮换号
                continue
            try:
                self._maybe_refresh(account)
                # 出站请求体在拿到账号后才最终定形：prompt_cache_key 需要 uid 做隔离，
                # global 域还要补 system——两者都改 body_dict，因此按需重新序列化。
                # 缺 conversation_id 的客户端（Anthropic / Claude Code 流量）回退到
                # 客户端键（ids.client，跨会话不变）：键里仍有 uid 做账号隔离，同时
                # 同一客户端的新会话第一发就能吃上共享的静态前缀缓存。若回退到会话键，
                # 每个新会话都是一个新键、注定全量冷启动。
                if shape.inject_prompt_cache_key(
                    body_dict, account.uid, conversation_id or ids.client
                ):
                    body_dirty = True
                if account.realm == "global" and shape.ensure_console_system(body_dict):
                    body_dirty = True
                attempt_body = (
                    json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
                    if body_dirty
                    else body
                )
                try:
                    response = wb_upstream.chat_request(
                        account, attempt_body,
                        conversation_id=conversation_id,
                        conversation_request_id=conversation_request_id,
                    )
                except crequests.RequestsError as exc:
                    logger.warning(f"[workbuddy] uid={account.uid[:8]}… 传输失败: {exc}")
                    self._penalize_transport(account)
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
                    # 成功即证明「这个号此刻服务这个模型」→ 清掉它的 11102 负缓存
                    self._clear_model_block(account, bare_model)
                    self._pool.rebind_sticky(ids.session, account.uid)
                    repair = dsml.enabled_repair(allowed_tools)
                    from providers.workbuddy.streamwrap import SseRepairStream

                    wrapped = SseRepairStream(
                        response.iter_content(chunk_size=8192),
                        repair,
                        on_error_frame=lambda payload, acct=account, model=bare_model:
                            self._note_stream_error(acct, model, payload),
                        on_usage_frame=lambda usage, acct=account, model=bare_model:
                            self._note_usage_cost(acct, model, usage),
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
                    f"[workbuddy] uid={account.uid[:8]}… 上游错误 status={status} kind={classified.kind}: {text[:160]}"
                )
                self._penalize(account, classified, bare_model, text)
                last_error = UpstreamResponse(
                    status=status, headers=dict(response.headers), raw=RawBytes(raw),
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
                # 软限流（429/14003）：上游只回 "too many requests" + retry_policy.retry=true，
                # 是瞬时限流，等几秒同号重试就能过。但换号轮换在单号池里不成立：刚罚完的号
                # 进了 exclude，第二轮 pick 直接返回 None，于是 429 原样透传给客户端
                # （"一上来就 Too Many Requests" 的成因）。这里补一次同号重试：先清掉刚写下的
                # 软冷却——否则会被自己的冷却挡住，和上面 session 刷新那条路是同一个坑——
                # 歇一下再打一次。
                if classified.kind == classify.SOFT_RATE and account.uid not in soft_retried:
                    soft_retried.add(account.uid)
                    self._store.update_runtime(
                        account.uid, cooldown_until_ms=0, cooldown_kind="",
                    )
                    attempts -= 1  # 同号重试不占换号预算（仍受总 attempts 上限约束）
                    exclude.discard(account.uid)
                    time.sleep(WB_SOFT_RETRY_SLEEP_S)
                    continue
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

    # ── token 刷新 ──

    def _needs_refresh(self, account: Account) -> bool:
        """是否已到期或临近到期（token_expires_ms=0 未知，视为需刷新）。"""
        if not account.refresh_token:
            return False
        if not account.token_expires_ms:
            return True
        return int(time.time() * 1000) + _REFRESH_AHEAD_MS >= account.token_expires_ms

    def _maybe_refresh(self, account: Account) -> None:
        """到期前主动刷新（对齐 wb2api keepalive）；失败不阻塞本次发送。"""
        if not self._needs_refresh(account):
            return
        self._try_refresh(account, quiet=True)

    def _try_refresh(self, account: Account, quiet: bool = False,
                     sync_credits: bool = False, *, force: bool = False) -> bool:
        """用 RT 换新 AT 并落库（bool 形态，见 _try_refresh_ex）。"""
        ok, _ = self._try_refresh_ex(
            account, quiet=quiet, sync_credits=sync_credits, force=force
        )
        return ok

    def _try_refresh_ex(self, account: Account, quiet: bool = False,
                        sync_credits: bool = False, *, force: bool = False) -> tuple[bool, str]:
        """用 RT 换新 AT 并落库。按账号串行化：并发调用只有一个真的去打上游。

        拿到锁后**重读库**：并发下别人可能刚刷完，用库里最新的 RT 才不会再轮换一次；
        非 force（请求路径/巡检）在重读后若已不过期就直接返回，省掉一次多余刷新。
        force=True（人工「刷新」按钮 / 保活排程）无论如何都打一次上游。

        返回 (是否成功, 失败原因文本)：保活任务要拿原因判「session 真的死了」还是
        网络抖动（前者才该累计到自动禁用），其余调用方只看 bool。
        """
        lock = self._refresh_lock(account.uid)
        if not lock.acquire(timeout=_REFRESH_LOCK_WAIT_S):
            return False, "等刷新锁超时"
        try:
            fresh = self._store.get_account(account.uid) or account
            if not fresh.refresh_token:
                return False, "缺少 refresh_token"
            if not force and not self._needs_refresh(fresh):
                return True, ""  # 已被并发刷新过
            try:
                data = wb_upstream.refresh_request(fresh)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                level = logger.info if quiet else logger.warning
                level(f"[workbuddy] uid={fresh.uid[:8]}… token 刷新失败: {message}")
                return False, message
            expires_ms = wb_upstream.refresh_expires_ms(data.get("expiresIn"))
            self._store.update_tokens(
                fresh.uid,
                str(data.get("accessToken") or ""),
                str(data.get("refreshToken") or ""),
                expires_ms,
                str(data.get("domain") or ""),
            )
            logger.info(f"[workbuddy] uid={fresh.uid[:8]}… token 已刷新")
            # 积分只在此刻顺带取回（拿到新 token 是唯一时机）：手动刷新同步等结果，
            # 自动刷新走后台线程——billing RPC 的延迟不进聊天路径。
            if sync_credits:
                self._refresh_credits(fresh.uid, quiet=quiet)
            else:
                self._refresh_credits_async(fresh.uid)
            return True, ""
        finally:
            lock.release()

    # ── 积分（只在取得新 token 时拉取：登录入库 / 手动导入 / token 刷新）──

    def _refresh_credits_async(self, uid: str) -> None:
        threading.Thread(
            target=self._refresh_credits, args=(uid,), daemon=True,
            name=f"wb-credits-{uid[:8]}",
        ).start()

    def _refresh_credits(self, uid: str, quiet: bool = True) -> wb_upstream.CreditsDetail | None:
        """取积分并落库（含快过期分桶）→ 返回本次观测；失败/空包返回 None。

        必须从库里重读账号：refresh 刚轮换过 access_token，旧对象带的是已作废凭证。
        **企业号走企业额度端点**（get-enterprise-user-usage：credit 已用 / limitNum 额度），
        个人号走资源包端点；两者都返回空（remain/total 双 0，无资源包）时不覆盖——
        credits 保持 None（未知）语义（权重不加 credits 项、积分保底也不误伤），
        并返回 None 让调用方（解冻判定）知道"没有新观测"，不去动旧值。

        快过期分桶（窗口见 WB_EXPIRING_SOON_H）落库后供选号加权：到期越近的余额
        越该先花掉，官方赠送/补偿的积分常带期限。
        """
        account = self._store.get_account(uid)
        if account is None:  # 刷新与落库之间账号可能被删
            return None
        try:
            detail = wb_upstream.fetch_credits_detail(
                account, expiring_within_ms=wb_pool.expiring_window_ms()
            )
        except Exception as exc:
            level = logger.info if quiet else logger.warning
            level(f"[workbuddy] uid={uid[:8]}… 积分查询失败: {type(exc).__name__}: {exc}")
            return None
        if detail.remain <= 0 and detail.total <= 0:
            return None
        self._store.update_runtime(
            uid, credits_remain=detail.remain, credits_total=detail.total,
            credits_updated_ms=int(time.time() * 1000),
            credits_expiring=detail.expiring,
            credits_earliest_at_ms=detail.earliest_at_ms,
            credits_earliest_remaining=detail.earliest_remaining,
        )
        return detail

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
            logger.warning(f"[workbuddy] uid={uid[:8]}… 签到失败: {exc}")
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
            logger.info(f"[workbuddy] uid={uid[:8]}… 签到成功: {outcome.message}")
            # 签到直接改余额：拉一次让面板与选号权重立刻跟上（失败只记日志）
            self._refresh_credits_async(uid)
        return {"uid": uid, "local": False, **outcome.to_dict()}

    def checkin_all(self, *, force: bool = False, with_balance: bool = True,
                    with_streak: bool = True) -> dict[str, Any]:
        """全池签到：只动 active 号，账号间错峰，串行执行。

        串行而非并发：签到是低频后台动作，"不像机器"比吞吐重要——并发打上游
        既是风控特征，也会和聊天流量抢连接与代理。暂停/禁用的号是人工决定，
        不代签。

        收尾按参考实现的整轮语义：签到 → 逐个查余额（余额回血即解冻硬冷却）→
        连登管家（兑换已解锁档位 + 抽完抽奖次数）。连登管家排在最后是因为「档位
        解锁」以当天签到后的天数为准，且它本身幂等，多跑无害。

        with_balance / with_streak 只给测试与「只要签到结论」的调用方留口子，
        排程与手动按钮都走默认（全开）。
        """
        accounts = [a for a in self._store.list_accounts() if a.status == STATUS_ACTIVE]
        tally = dict.fromkeys(
            (checkin.STATE_CLAIMED, checkin.STATE_ALREADY, checkin.STATE_SKIPPED,
             checkin.STATE_INACTIVE, checkin.STATE_ERROR),
            0,
        )
        revived = 0

        def _checkin_one(account: Account) -> dict[str, Any]:
            nonlocal revived
            result = self.checkin(account.uid, force=force)
            if with_balance:
                revived += 1 if self._refresh_balance(account.uid) else 0
            return result

        items = self._run_pool_task(
            accounts, _checkin_one, label="签到", error_state=checkin.STATE_ERROR
        )
        for item in items:
            state = item.get("state") or checkin.STATE_ERROR
            tally[state] = tally.get(state, 0) + 1
        summary: dict[str, Any] = {
            "total": len(accounts),
            "claimed": tally[checkin.STATE_CLAIMED],
            "already": tally[checkin.STATE_ALREADY],
            "skipped": tally[checkin.STATE_SKIPPED],
            "inactive": tally[checkin.STATE_INACTIVE],
            "failed": tally[checkin.STATE_ERROR],
            "credits": sum(float(item.get("credit") or 0) for item in items),
        }
        if with_balance:
            summary["revived"] = revived
        if with_streak:
            try:
                summary["streak"] = self.streak_bonus_all()
            except Exception as exc:  # 收尾失败不回滚签到结论
                logger.warning(f"[workbuddy] 连登管家异常: {type(exc).__name__}: {exc}")
                summary["streak"] = {"total": 0, "summary": f"连登管家异常: {exc}", "items": []}
        summary["summary"] = (
            f"共 {len(accounts)} 号：领取 {tally[checkin.STATE_CLAIMED]}，"
            f"已签 {tally[checkin.STATE_ALREADY]}，"
            f"不适用 {tally[checkin.STATE_SKIPPED] + tally[checkin.STATE_INACTIVE]}，"
            f"失败 {tally[checkin.STATE_ERROR]}"
            + (f"，解冻 {revived}" if revived else "")
            + (f"；{summary['streak'].get('summary')}" if with_streak else "")
        )
        # items 放最后（前端表格按它逐行展示）
        return {**summary, "items": items}

    def _refresh_balance(self, uid: str) -> bool:
        """查余额并落库，余额回血时解冻硬冷却（返回是否解冻）。失败只记日志。

        用本次**新拿到的**观测判解冻：查失败时 store 里可能还留着旧值（签到前
        刚清零的号也常是 0），拿旧值解冻等于凭空放一个号出去撞 402。
        """
        detail = self._refresh_credits(uid, quiet=True)
        if detail is None or detail.remain <= 0:
            return False
        account = self._store.get_account(uid)
        if account is None:
            return False
        return self._revive_hard_cooldown(account, detail.remain)

    # ── 定时任务（连登管家 / 活跃上报 / 猫猫旅行 / token 保活）──
    # 上游契约见 growth.py；这层只做账号门控、限速、汇总与落库。四类任务的时点与
    # 开关由 scheduler.py 驱动（config.json 的 schedule 段），管理台「立即运行」
    # 走同一批 run_*_all，与自动排程共用一条管线。
    #
    # 账号门控（与参考实现同口径）：
    #   global 域 — 无 CN 任务体系，除活跃上报外一律不发请求（国际版 /v2/report 实测可用）
    #   企业号   — 无成长中心（growth 全 403），签到与成长任务都跳过
    #   paused   — 暂停是「别拿去跑对话」，签到/成长/保活照常（保号）；disabled 一律跳过

    def _task_accounts(self, *, include_paused: bool = True) -> list[Account]:
        wanted = {STATUS_ACTIVE, STATUS_PAUSED} if include_paused else {STATUS_ACTIVE}
        return [a for a in self._store.list_accounts() if a.status in wanted]

    def _run_pool_task(
        self,
        accounts: list[Account],
        run: Callable[[Account], dict[str, Any]],
        *,
        label: str,
        error_state: str = "error",
        error_extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """逐账号跑一次池任务：账号之间错开（整点齐射是最容易被记成机器特征的形态），
        单号异常转成 error 结果、不中断整轮。

        五类任务（签到 / 连登管家 / 活跃上报 / 猫猫旅行 / token 保活）的节奏与容错
        逐字相同，差异只有"跑什么"和"错误结果长什么样"——模板在这里，任务本体是 run。
        """
        items: list[dict[str, Any]] = []
        for index, account in enumerate(accounts):
            if index:
                time.sleep(WB_CHECKIN_STAGGER_MS / 1000.0)
            try:
                result = run(account)
            except Exception as exc:  # 单号异常不中断整轮
                logger.warning(
                    f"[workbuddy] uid={account.uid[:8]}… {label}异常: {type(exc).__name__}: {exc}"
                )
                result = {
                    "uid": account.uid,
                    "state": error_state,
                    "message": f"{type(exc).__name__}: {exc}",
                    **(error_extra or {}),
                }
            items.append(result)
        return items

    def _growth_skip(self, account: Account, *, need_access: bool = True) -> str:
        """CN 成长任务的账号级门控（非空 = 跳过原因，一个请求都不发）。

        只读的连登天数回读在国际版也能用（实测 workbuddy.ai 的 /activity/growth/streak
        正常返回天数），被挡的是**写侧任务**：档位兑换、抽奖、猫猫旅行/领养都是 CN
        个人号专属，企业号连回读都 403。
        """
        if account.realm == "global":
            return "global 域无档位兑换/旅行（CN 专属）"
        if account.enterprise_id:
            return "企业账号无成长中心"
        if need_access and not account.access_token:
            return "缺少 access_token"
        return ""

    def _revive_hard_cooldown(self, account: Account, remain: float) -> bool:
        """余额恢复即解冻「积分耗尽」硬冷却（软冷却/waf 不动：那是限流，不是没额度）。

        402 硬冷却的本质是「等签到回血」，回血到账却不解冻，账号会白挂到次日 04:00。
        """
        if remain <= 0 or account.cooldown_kind != "hard":
            return False
        self._store.update_runtime(account.uid, cooldown_until_ms=0, cooldown_kind="")
        logger.info(f"[workbuddy] uid={account.uid[:8]}… 余额已恢复，解除硬冷却")
        return True

    # ── 连登管家 ──

    def streak_bonus(self, uid: str) -> dict[str, Any]:
        """单账号连登管家：兑换所有已解锁档位 → 抽完所有抽奖次数。

        档位（7d/14d/28d）按连续登录天数解锁，兑换发 credit/energy/补签卡/抽奖次数，
        而抽奖次数只能从兑换获得——所以「兑换 → 抽奖」必须连着跑。整条链路幂等：
        已 locked/claimed 的档位跳过，无次数时抽奖循环直接不进入。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        reason = self._growth_skip(account)
        if reason:
            return {"uid": uid, "state": "skipped", "message": reason,
                    "redeemed": [], "drawn": 0, "streak_days": None}

        self._maybe_refresh(account)
        try:
            full = growth.streak_full(account)
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 连登状态查询失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc),
                    "redeemed": [], "drawn": 0, "streak_days": None}

        redeemed: list[str] = []
        for tier in full.redeemable_tiers():
            try:
                growth.redeem_tier(account, tier.tier)
            except Exception as exc:
                # 403 = 天数不足（本地状态与上游不一致时的兜底），属预期，不刷 warning
                level = logger.info if growth.is_tier_locked(exc) else logger.warning
                level(f"[workbuddy] uid={uid[:8]}… 兑换 {tier.tier} 档失败: {exc}")
                continue
            redeemed.append(tier.tier)
            logger.info(
                f"[workbuddy] uid={uid[:8]}… 连登兑换 {tier.tier} 档"
                + (f"（{tier.reward_text()}）" if tier.reward_text() else "")
            )

        drawn = 0
        prizes: list[str] = []
        try:
            chances = growth.lottery_chances(account)
        except Exception as exc:
            # 兑换已经成功，抽奖查询失败不能把整轮改写成失败
            logger.warning(f"[workbuddy] uid={uid[:8]}… 抽奖次数查询失败: {exc}")
            chances = 0
        for index in range(chances):
            try:
                prize = growth.lottery_draw(account)
            except Exception as exc:
                logger.warning(f"[workbuddy] uid={uid[:8]}… 第{index + 1}抽失败: {exc}")
                break
            drawn += 1
            prizes.append(_compact_payload(prize))
            logger.info(f"[workbuddy] uid={uid[:8]}… 第{drawn}抽 {prizes[-1]}")
        state = "claimed" if (redeemed or drawn) else "idle"
        return {
            "uid": uid, "state": state, "message": self._streak_message(redeemed, drawn),
            "redeemed": redeemed, "drawn": drawn, "prizes": prizes,
            "streak_days": full.days,
            "redeemable": [tier.tier for tier in full.redeemable_tiers()],
        }

    @staticmethod
    def _streak_message(redeemed: list[str], drawn: int) -> str:
        bits = []
        if redeemed:
            bits.append("兑换 " + "/".join(redeemed) + " 档")
        if drawn:
            bits.append(f"抽奖 {drawn} 次")
        return "、".join(bits) if bits else "无可兑换档位、无抽奖次数"

    def streak_bonus_all(self) -> dict[str, Any]:
        """全池连登管家：签到排程的收尾动作（挂它才「到天数那天自动兑换→抽奖」）。"""
        accounts = self._task_accounts()
        items: list[dict[str, Any]] = []
        redeemed = drawn = failed = skipped = 0
        items = self._run_pool_task(
            accounts, lambda account: self.streak_bonus(account.uid),
            label="连登管家", error_extra={"redeemed": [], "drawn": 0},
        )
        for item in items:
            redeemed += len(item.get("redeemed") or [])
            drawn += int(item.get("drawn") or 0)
            skipped += 1 if item.get("state") == "skipped" else 0
            failed += 1 if item.get("state") == "error" else 0
        return {
            "total": len(accounts),
            "redeemed": redeemed,
            "drawn": drawn,
            "skipped": skipped,
            "failed": failed,
            "summary": (
                f"共 {len(accounts)} 号：兑换 {redeemed} 档，抽奖 {drawn} 次，"
                f"不适用 {skipped}，失败 {failed}"
            ),
            "items": items,
        }

    # ── 对话活跃上报 ──

    def activity_report(self, uid: str) -> dict[str, Any]:
        """单账号活跃上报（chat_request_send）+ 连登自检。

        一条上报同时点亮 growth 连登天数并解锁 first_buddy 任务（领养前置）。
        每号每天 1 次即可：多时点高频上报是风控特征，收益为零。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        if account.enterprise_id:
            return {"uid": uid, "state": "skipped", "message": "企业账号无成长中心",
                    "streak_days": None, "suspect": False}
        if not account.access_token:
            return {"uid": uid, "state": "skipped", "message": "缺少 access_token",
                    "streak_days": None, "suspect": False}

        self._maybe_refresh(account)
        conversation_id = f"wb2api-{int(time.time() * 1000)}"
        try:
            growth.report_chat_activity(
                account, conversation_id, model_id=WB_ACTIVITY_MODEL_ID,
                model_name=WB_ACTIVITY_MODEL_NAME,
            )
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 活跃上报失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc),
                    "streak_days": None, "suspect": False}

        # 自检：上报 200 ≠ 计分（缺 userId 时上游 200 但静默丢弃），回读连登天数是
        # 唯一能发现这件事的观测点。只读、不重试——上报按天幂等，明天还会再报。
        # 「回读失败」不算可疑（growth 端点在部分账号上本就不适用），只有 days=0
        # 才是静默丢弃的证据。
        try:
            days = growth.streak_days(account)
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 上报成功但连登回读失败: {exc}")
            return {"uid": uid, "state": "reported", "message": "已上报（连登回读失败）",
                    "streak_days": None, "suspect": False}
        if days == 0:
            logger.warning(
                f"[workbuddy] uid={uid[:8]}… 上报成功但 streak.days=0（事件被静默丢弃？）"
            )
            return {"uid": uid, "state": "reported", "message": "已上报，但连登天数为 0（疑似被丢弃）",
                    "streak_days": 0, "suspect": True}
        return {"uid": uid, "state": "reported", "message": f"已上报，连登 {days} 天",
                "streak_days": days, "suspect": False}

    def activity_report_all(self) -> dict[str, Any]:
        """全池活跃上报。global 域也报（国际版 /v2/report 可用），企业号跳过。"""
        accounts = self._task_accounts()
        items: list[dict[str, Any]] = []
        reported = skipped = failed = suspect = 0
        items = self._run_pool_task(
            accounts, lambda account: self.activity_report(account.uid),
            label="活跃上报", error_extra={"suspect": False},
        )
        for item in items:
            state = item.get("state")
            reported += 1 if state == "reported" else 0
            skipped += 1 if state == "skipped" else 0
            failed += 1 if state == "error" else 0
            suspect += 1 if item.get("suspect") else 0
        return {
            "total": len(accounts),
            "reported": reported,
            "skipped": skipped,
            "failed": failed,
            "suspect": suspect,
            "summary": (
                f"共 {len(accounts)} 号：上报 {reported}，不适用 {skipped}，失败 {failed}"
                + (f"，可疑 {suspect}（连登未点亮）" if suspect else "")
            ),
            "items": items,
        }

    # ── 猫猫旅行 ──

    def travel(self, uid: str) -> dict[str, Any]:
        """单账号单趟旅行状态机：无猫 → 领养；有猫 → 派出 / 领奖（不轮询不等待）。

        一趟只推进一步：到站领奖与再次派出是两次巡游的事，硬等会把一个账号的
        行程拖成分钟级。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        # 旅行全是短 RPC（状态/派出/领奖），AT 过期由 _maybe_refresh 兜住，
        # 没有 AT 也能靠 RT 现换，所以门控不要求 access_token。
        reason = self._growth_skip(account, need_access=False)
        if reason:
            return {"uid": uid, "state": "skipped", "message": reason, "credit": 0.0}

        self._maybe_refresh(account)
        try:
            buddy = growth.buddy_info(account)
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 猫档案查询失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}
        if buddy is None:
            return self._travel_adopt(account)

        try:
            state = growth.travel_status(account)
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 旅行状态查询失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}

        if state.state == growth.TRAVEL_ARRIVED:
            if state.record_id <= 0:
                return {"uid": uid, "state": "arrived", "message": "已到站但缺 record_id，跳过",
                        "credit": 0.0}
            try:
                credit = growth.travel_claim(account, state.record_id)
            except Exception as exc:
                logger.warning(f"[workbuddy] uid={uid[:8]}… 旅行领奖失败: {exc}")
                return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}
            logger.info(
                f"[workbuddy] uid={uid[:8]}… 旅行领奖 +{checkin.fmt_credit(credit)} 积分"
                f"（record={state.record_id}）"
            )
            return {"uid": uid, "state": "claimed",
                    "message": f"到站领奖 +{checkin.fmt_credit(credit)}", "credit": credit}

        if state.state == growth.TRAVEL_IDLE:
            if state.daily_limit_reached:
                return {"uid": uid, "state": "idle", "message": "今日已派出过，跳过", "credit": 0.0}
            try:
                growth.travel_depart(account, growth.TRAVEL_LOCATION_ID)
            except Exception as exc:
                logger.warning(f"[workbuddy] uid={uid[:8]}… 旅行派出失败: {exc}")
                return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}
            logger.info(f"[workbuddy] uid={uid[:8]}… 已派出猫猫旅行")
            return {"uid": uid, "state": "departed", "message": "已派出", "credit": 0.0}

        if state.state == growth.TRAVEL_TRAVELING:
            return {"uid": uid, "state": "traveling", "message": "在途，等下一趟",
                    "credit": 0.0}
        return {"uid": uid, "state": "unknown",
                "message": f"未知状态 {state.state or '(空)'}", "credit": 0.0}

    def _travel_adopt(self, account: Account) -> dict[str, Any]:
        """无猫时领养：上报（解锁 first_buddy）→ 同意协议 → 领养。

        上报必须在前：first_buddy 任务的门槛真实来源是「当日有活跃上报」，
        没上报时 buddy/first 一律 400「task not completed yet」。门槛未达记一次
        当日已试，不再重试——同日多趟反复撞门槛就是给上游送风控特征。
        """
        uid = account.uid
        today = checkin.beijing_today()
        with self._adopt_guard:
            if self._adopt_tried.get(uid) == today:
                return {"uid": uid, "state": "adopt-blocked",
                        "message": "今日已尝试领养（活跃门槛未达）", "credit": 0.0}
        try:
            growth.report_chat_activity(
                account, f"wb2api-adopt-{int(time.time() * 1000)}",
                model_id=WB_ACTIVITY_MODEL_ID, model_name=WB_ACTIVITY_MODEL_NAME,
            )
            time.sleep(WB_ADOPT_REPORT_GAP_MS / 1000.0)  # 给上游事件处理留时间
        except Exception as exc:
            # 前置失败不阻塞：让 buddy/first 按既有错误路径把真实原因暴露出来
            logger.info(f"[workbuddy] uid={uid[:8]}… 领养前置上报失败: {exc}")
        try:
            growth.buddy_agreement(account)
        except Exception as exc:
            logger.warning(f"[workbuddy] uid={uid[:8]}… 领养协议失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}
        try:
            growth.buddy_first(account)
        except Exception as exc:
            if growth.is_task_incomplete(exc):
                with self._adopt_guard:
                    self._adopt_tried[uid] = today
                logger.info(f"[workbuddy] uid={uid[:8]}… 领养门槛未达（活跃不足），明天再试")
                return {"uid": uid, "state": "adopt-blocked",
                        "message": "活跃门槛未达，明天再试", "credit": 0.0}
            logger.warning(f"[workbuddy] uid={uid[:8]}… 领养失败: {exc}")
            return {"uid": uid, "state": "error", "message": str(exc), "credit": 0.0}
        logger.info(f"[workbuddy] uid={uid[:8]}… 已领养猫猫（+300 积分）")
        return {"uid": uid, "state": "adopted", "message": "已领养（+300 积分）", "credit": 300.0}

    def travel_all(self) -> dict[str, Any]:
        """全池旅行巡检（独立排程：09/21 点各一趟，凑出「派出 → 到站领奖」闭环）。"""
        accounts = self._task_accounts()
        items: list[dict[str, Any]] = []
        tally: dict[str, int] = {}
        credits = 0.0
        items = self._run_pool_task(
            accounts, lambda account: self.travel(account.uid),
            label="旅行", error_extra={"credit": 0.0},
        )
        for item in items:
            state = str(item.get("state") or "error")
            tally[state] = tally.get(state, 0) + 1
            credits += float(item.get("credit") or 0)
        return {
            "total": len(accounts),
            "adopted": tally.get("adopted", 0),
            "departed": tally.get("departed", 0),
            "claimed": tally.get("claimed", 0),
            "traveling": tally.get("traveling", 0),
            "skipped": tally.get("skipped", 0),
            "blocked": tally.get("adopt-blocked", 0),
            "failed": tally.get("error", 0),
            "credits": credits,
            "summary": (
                f"共 {len(accounts)} 号：领养 {tally.get('adopted', 0)}，"
                f"派出 {tally.get('departed', 0)}，领奖 {tally.get('claimed', 0)}"
                + (f"（+{checkin.fmt_credit(credits)} 积分）" if credits else "")
                + f"，在途 {tally.get('traveling', 0)}，"
                f"不适用 {tally.get('skipped', 0) + tally.get('adopt-blocked', 0)}，"
                f"失败 {tally.get('error', 0)}"
            ),
            "items": items,
        }

    # ── token 保活 ──

    def keepalive(self, uid: str) -> dict[str, Any]:
        """单账号续期：无条件用 RT 换新 AT。

        与请求路径的「快过期才刷」不同：保活的目的就是让闲置号别过期，所以不看
        剩余时长。失败按连续计数走 session 死亡判定（一次失败不杀号，连续
        WB_SESSION_INVALID_LIMIT 次才禁用，误判的号刷新成功即清零复活）。
        """
        account = self._store.get_account(uid)
        if account is None:
            raise KeyError(f"账号不存在: {uid}")
        if account.status == STATUS_DISABLED:
            return {"uid": uid, "state": "skipped", "message": "账号已禁用"}
        if not account.refresh_token:
            return {"uid": uid, "state": "skipped", "message": "缺少 refresh_token"}

        ok, error = self._try_refresh_ex(account, quiet=True, force=True)
        if ok:
            if account.consecutive_failures:
                self._store.update_runtime(uid, consecutive_failures=0)
            return {"uid": uid, "state": "refreshed", "message": "token 已续期"}

        if not classify.session_dead_text(error):
            # 网络抖动/上游 5xx：不计数（否则一次断网就能连杀整池号）
            return {"uid": uid, "state": "failed", "message": error}
        failures = account.consecutive_failures + 1
        fields: dict[str, Any] = {"consecutive_failures": failures}
        disabled = failures >= WB_SESSION_INVALID_LIMIT
        if disabled:
            fields["status"] = STATUS_DISABLED
            fields["status_reason"] = "session invalid (keepalive)"
        self._store.update_runtime(uid, **fields)
        logger.warning(
            f"[workbuddy] uid={uid[:8]}… 保活失败（{failures}/{WB_SESSION_INVALID_LIMIT}）: {error}"
            + ("→ 已禁用" if disabled else "")
        )
        return {
            "uid": uid, "state": "disabled" if disabled else "failed",
            "message": f"session 失效 {failures}/{WB_SESSION_INVALID_LIMIT} 次"
            + ("，已禁用" if disabled else ""),
        }

    def keepalive_all(self) -> dict[str, Any]:
        """全池续期（22 点）：paused 也续（暂停不该放着过期），disabled 跳过。"""
        accounts = self._task_accounts()
        items: list[dict[str, Any]] = []
        refreshed = failed = disabled = skipped = 0
        items = self._run_pool_task(
            accounts, lambda account: self.keepalive(account.uid),
            label="保活", error_state="failed",
        )
        for item in items:
            state = str(item.get("state") or "failed")
            refreshed += 1 if state == "refreshed" else 0
            disabled += 1 if state == "disabled" else 0
            failed += 1 if state == "failed" else 0
            skipped += 1 if state == "skipped" else 0
        return {
            "total": len(accounts),
            "refreshed": refreshed,
            "failed": failed,
            "disabled": disabled,
            "skipped": skipped,
            "summary": (
                f"共 {len(accounts)} 号：续期 {refreshed}，失败 {failed}，"
                f"不适用 {skipped}" + (f"，禁用 {disabled}" if disabled else "")
            ),
            "items": items,
        }

    # ── 罚号与记账 ──

    def _clear_model_block(self, account: Account, model: str) -> None:
        """成功请求证明「这个号此刻服务这个模型」，清掉它的 11102 负缓存条目。

        只清 11102 条目：6004（模型级限流）有上游给的重置墙钟语义，一次成功不该抹掉
        它——否则限流期内每个成功请求都会把冷却清零，等于没有冷却。
        """
        if not model:
            return
        fresh = self._store.get_account(account.uid)
        cooldowns = dict((fresh or account).model_cooldowns or {})
        entry = cooldowns.get(model)
        if not isinstance(entry, dict) or entry.get("kind") != classify.MODEL_BLOCKED:
            return
        cooldowns.pop(model, None)
        self._store.update_runtime(account.uid, model_cooldowns=cooldowns)

    def _block_model(self, account: Account, model: str, payload: str) -> None:
        """写入 11102 负缓存：该 (账号, 模型) 在一段时间内不再被选中。

        基础 TTL 6h、重复命中指数退避（封顶 24h）——11102 是「这个后端没有这个模型」
        的稳定属性，TTL 短了只会让同一组合反复撞上游。
        """
        if not model:
            return
        fresh = self._store.get_account(account.uid) or account
        cooldowns = dict(fresh.model_cooldowns or {})
        previous = cooldowns.get(model) if isinstance(cooldowns.get(model), dict) else {}
        hits = int(previous.get("hits") or 0) + 1 if previous.get("kind") == classify.MODEL_BLOCKED else 1
        ttl_s = min(
            WB_NEGATIVE_CACHE_BASE_S * (2 ** min(hits - 1, WB_NEGATIVE_CACHE_SHIFT)),
            WB_NEGATIVE_CACHE_MAX_S,
        )
        cooldowns[model] = {
            "until_ms": int(time.time() * 1000) + ttl_s * 1000,
            "kind": classify.MODEL_BLOCKED,
            "hits": hits,
            "reason": _compact_payload(payload, limit=120),
        }
        self._store.update_runtime(account.uid, model_cooldowns=cooldowns)
        logger.warning(
            f"[workbuddy] uid={account.uid[:8]}… 模型 {model} 不可用（11102，第 {hits} 次），"
            f"避让 {ttl_s // 3600}h"
        )

    def _penalize_transport(self, account: Account) -> None:
        """传输层失败按 SERVER 口径喂熔断（不写冷却——下一轮换号已排除本号）。"""
        self._feed_breaker(account)

    def _note_stream_error(self, account: Account, model: str, payload: str) -> None:
        """流内 error 帧（6004 限流/内容拦截等真实形态）——罚号依据 D6。"""
        classified = classify.classify(200, payload)
        if classified.kind in (classify.NONE,):
            return
        self._penalize(account, classified, model, payload)

    def _note_usage_cost(self, account: Account, model: str, usage: dict[str, Any]) -> None:
        """末帧 usage → 成本台账（实测扣费观测，见 account_pool.note_model_cost）。

        上游把本次真实扣费放在 usage.credit（积分），`credit` 缺失 = 没观测到，
        不写台账（"测不到"不能被当成"测得 0"——那会把收费号误标成免费号）。
        credit=0 是有效观测（限免期/夜间免费），正是成本分层要找的号。
        """
        credit = _as_number(usage.get("credit"))
        if credit is None:
            return
        total = _usage_total_tokens(usage)
        if total <= 0:
            return  # 单价要按 token 数归一，没有 token 数就没有可比的价格
        self._pool.note_model_cost(account.uid, model, credit, total)

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
        if kind == classify.MODEL_BLOCKED:
            # 11102「该后端无此模型」：(账号, 模型) 负缓存避让。**不罚账号**——号是好的，
            # 只是这个模型在它的后端上不存在；换号也没用（每个号各自记各的）。
            self._block_model(account, model, text)
            self._store.update_runtime(account.uid, **fields)
            return
        if kind == classify.HARD_CREDIT:
            fields["cooldown_until_ms"] = classify.hard_cooldown_until_ms()
            fields["cooldown_kind"] = "hard"
        elif kind == classify.SOFT_RATE:
            current = self._store.get_account(account.uid)
            failures = max(current.consecutive_failures if current else 0, 0)
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
                # 账号级软限流要累加连败，下一轮的指数退避才真的 ×2^n（模型级
                # 6004 只挡该模型、账号本身健康，不动连败计数）。
                fields["consecutive_failures"] = failures + 1
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
        """OAuth 登录完成入库（新 token 时刻）：后台顺带拉积分给账号列填数。

        不拉模型目录：目录只在手动「获取模型」时刷新（见 refresh 的 docstring）。
        """
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
            # 人工「刷新」= 强制换票（不看是否临近过期），并同步等积分结果：
            # 返回的 public_dict 直接带最新余额，前端按钮立即可见
            if not self._try_refresh(account, sync_credits=True, force=True):
                raise RuntimeError("token 刷新失败（凭证可能已失效，请重新登录）")
        else:
            raise ValueError(f"未知动作: {action}")
        updated = self._store.get_account(uid)
        return updated.public_dict() if updated else {}

    def login_start(self, realm: str = "cn") -> dict[str, Any]:
        return self._login_manager.start(realm)

    def login_poll(self, state: str) -> dict[str, Any]:
        return self._login_manager.poll(state)

    def pool_snapshot(self) -> dict[str, Any]:
        return self._pool.snapshot()
