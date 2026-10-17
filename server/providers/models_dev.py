"""models.dev 兜底目录：补厂商 manifest 没声明的上下文窗口、最大输出与输入/输出模态。

移植 workbuddy2api-panel 的 internal/upstream/modelsdev.go（拉取 / 索引 / 采值规则），
字段策略照它的 context_catalog.go——**输出上限未知就省略，绝不编造**。

字段级查找链（上游动态值由各 provider 自己判断，本模块只管后两级）：

    上游 manifest 声明值（权威）
    > 本地缓存 server/data/modelsdev.json
    > models.dev 按需拉取（异步、不阻塞请求；失败静默，展示层保持「—」）

「上游优先」是默认口径（fill_limits 只填空值）；调用方也可以传 `prefer=True` 把
目录当权威（codex 页就是这样：manifest 与目录给的上下文常不是一个数，取目录值）。

多 provider 采值：同一裸 id 在 models.dev 里常被多家收录且互相分歧，取值优先级
官方 vendor 源 > 票数众数 > provider 名字典序。**分歧判定含模态**——limit 相同但
modalities 不同的条目算两个候选，不会把两家的模态投票混在一起。最后一级是确定性
tie-break——同一份文档两次拉取必须落同一个值，否则管理台的数字会自己抖动。

与参考实现的一处刻意偏差：**不采用「上下文未知 → 1M 兜底」**。那条兜底服务于客户端
按 context_length 提前截断的场景（高估优于低估）；网关对外报的是事实，未知就留空。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

# crequests 指向出站会话复用层（每线程一条 keep-alive 连接，逐请求清 Cookie）：
# 调用写法与 curl_cffi 顶层 API 同形，只是不再每次新建 Session。类型与异常
# (RequestsError/Response) 在该模块内原样再导出。
from net import outbound as crequests
from net.proxy import proxies_for

URL = "https://models.dev/api.json"
CACHE_PATH = DATA_DIR / "modelsdev.json"

# 单次拉取超时（连接 / 读取）。参考实现用 5s 总超时；我们这里是后台线程、不占请求
# 路径，读取放宽到 30s——文档 ~5.6MB，5s 在慢链路上大概率失败，白白丢兜底值。
TIMEOUT = (5.0, 30.0)
# 响应体上限：文档实测 ~5.6MB，留足余量挡住异常大响应；超限直接放弃（截断会让 JSON 解析失败）
MAX_BODY = 32 << 20
# 拉取节流（成功与失败同算）：文档是全量聚合体，短窗内不重复打 models.dev
FETCH_COOLDOWN_S = 300.0
# 数值上限校验：context / output 超过它视为脏数据丢弃
VALUE_MAX = 1_000_000_000

# 官方 vendor 源优先名单：聚合网关（nano-gpt 等）自报的 limit 常与官方源分歧
VENDOR_SOURCES = frozenset({
    "openai", "anthropic", "google", "deepseek", "moonshotai", "moonshotai-cn",
    "zai", "minimax", "xai", "meta",
})

_lock = threading.Lock()
_index: dict[str, dict[str, Any]] | None = None   # 裸 id → 上限与模态（None = 尚未拉到）
_cache: dict[str, dict[str, Any]] = {}            # 本地缓存（第 3 级）
_asked: set[str] = set()                          # 本进程问过的裸 id（缓存只写这些）
_cache_loaded = False
_last_fetch = 0.0
_fetching = False


# ── 查询（调用方入口）────────────────────────────────────────────


def fill_limits(
    model_id: str,
    values: dict[str, Any],
    *,
    context_key: str = "context_window",
    output_key: str = "max_output_tokens",
    list_keys: dict[str, str] | None = None,
    prefer: bool = False,
) -> list[str]:
    """就地写 values 里的上下文 / 最大输出 / 模态列表，返回实际写入的键名。

    prefer=False（默认）：**只填空值**（None / 0 / 非数字 / 空列表），厂商给了值就不动。
    prefer=True：**目录匹配到就以它为准**（覆盖厂商值），没匹配到就保留厂商值——
    调用方据此把「按目录来」当策略（如 codex 页的上下文与输入模态，manifest 与目录
    给的不是一个数，取目录值）。两条路都只认查得到的值：输出上限为 0 或缺省算
    「未知」，模态为空列表算「未知」，查不到/空值保持原样（展示层显示「—」）。

    键名可指定：三家 provider 的元数据字段名不同（codex/workbuddy 用统一字段名，
    opencode 沿用上游 models.dev 的 camelCase），翻译留在调用层，本模块只认语义。
    `list_keys` 是「索引字段 → values 键名」的映射，用于列表型字段（模态）；
    不传就只动两个数值字段。
    """
    limits = lookup(model_id)
    if not limits:
        return []
    filled: list[str] = []
    context = limits.get("context_window") or 0
    output = limits.get("max_output_tokens") or 0
    if context > 0 and (prefer or not _as_count(values.get(context_key))):
        values[context_key] = context
        filled.append(context_key)
    if output > 0 and (prefer or not _as_count(values.get(output_key))):
        values[output_key] = output
        filled.append(output_key)
    for source_key, target_key in (list_keys or {}).items():
        items = limits.get(source_key)
        if not isinstance(items, list) or not items:
            continue
        current = values.get(target_key)
        if not prefer and isinstance(current, list) and current:
            continue
        values[target_key] = list(items)
        filled.append(target_key)
    return filled


def lookup(model_id: str) -> dict[str, Any] | None:
    """裸 id → 数值上限（内存索引优先，回落本地缓存），未收录返回 None。

    只读内存，顺带触发一次后台拉取（若尚未拉过且在节流窗口外）；绝不发同步网络请求。
    """
    key = _bare(model_id)
    if not key:
        return None
    _ensure_async()
    with _lock:
        _load_cache_locked()
        _asked.add(key)
        if _index is not None and key in _index:
            return dict(_index[key])
        if key in _cache:
            return dict(_cache[key])
    return None


# ── 拉取调度 ─────────────────────────────────────────────────────


def _ensure_async() -> None:
    """确保索引可用（异步）：已有索引 / 正在拉 / 节流窗口内 → 直接返回。"""
    global _last_fetch, _fetching
    with _lock:
        _load_cache_locked()
        if _index is not None or _fetching:
            return
        now = time.monotonic()
        if now - _last_fetch < FETCH_COOLDOWN_S:
            return
        # 先把 last_fetch 置为「本次进行中」：in-flight 去重，并发调用不再重复起线程
        _last_fetch = now
        _fetching = True
    threading.Thread(target=_fetch, daemon=True).start()


def _fetch() -> None:
    """拉取并建索引（后台线程；任何失败只告警 + 冷却，不上抛）。"""
    global _index, _fetching
    try:
        response = crequests.get(
            URL,
            impersonate="chrome",
            proxies=proxies_for(URL),
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            logger.warning(f"[模型库] 拉取失败 HTTP {response.status_code}（兜底值不可用）")
            return
        raw = response.content
        if len(raw) > MAX_BODY:
            logger.warning(f"[模型库] 响应体 {len(raw)} 字节超上限，丢弃（兜底值不可用）")
            return
        index = _build_index(json.loads(raw))
        if not index:
            logger.warning("[模型库] 索引为空，按失败处理（兜底值不可用）")
            return
        with _lock:
            _index = index
            _write_cache_locked()
        logger.info(f"[模型库] models.dev 兜底目录就绪 models={len(index)}")
    except Exception as exc:
        logger.warning(f"[模型库] 拉取失败: {type(exc).__name__}: {exc}（兜底值不可用）")
    finally:
        with _lock:
            _fetching = False


def _build_index(doc: Any) -> dict[str, dict[str, Any]]:
    """models.dev 文档 → 裸 id 索引（采值规则见模块 docstring）。"""
    if not isinstance(doc, dict):
        return {}
    # 裸 id → 候选值（同值多 provider 只计票不重复存）→ 采值结果
    candidates: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}
    for provider, provider_body in doc.items():
        models = provider_body.get("models") if isinstance(provider_body, dict) else None
        if not isinstance(models, dict):
            continue
        for full_id, model in models.items():
            if not isinstance(model, dict):
                continue
            limit = model.get("limit")
            if not isinstance(limit, dict):
                continue
            context = _as_count(limit.get("context"))
            output = _as_count(limit.get("output"))
            # 脏值不进索引：上下文缺失 / 非正 / 超量级整条丢弃；输出为负或超量级丢弃；
            # 输出缺省或 0 保留（= 未知，展示层据此不编造）
            if not context or context > VALUE_MAX:
                continue
            if output is None:
                output = 0
            elif output < 0 or output > VALUE_MAX:
                continue
            modalities = model.get("modalities")
            modalities = modalities if isinstance(modalities, dict) else {}
            inputs = _str_list(modalities.get("input"))
            outputs = _str_list(modalities.get("output"))
            # 分歧判定带上模态：limit 相同、模态不同的条目算两个候选，
            # 免得把两家的模态投到一条上
            slot = candidates.setdefault(_bare(full_id), {})
            key = (context, output, tuple(inputs), tuple(outputs))
            entry = slot.get(key)
            if entry is None:
                slot[key] = {
                    "context_window": context,
                    "max_output_tokens": output,
                    "input_modalities": inputs,
                    "output_modalities": outputs,
                    "vendor": provider in VENDOR_SOURCES,
                    "votes": 1,
                    "provider": provider,
                }
                continue
            entry["votes"] += 1
            entry["vendor"] = entry["vendor"] or provider in VENDOR_SOURCES
            # 候选的 provider 身份取已见最小名（确定性 tie-break 用）
            entry["provider"] = min(entry["provider"], provider)
    out: dict[str, dict[str, Any]] = {}
    for model_id, slot in candidates.items():
        best: dict[str, Any] | None = None
        for entry in slot.values():
            if best is None or _better(entry, best):
                best = entry
        picked: dict[str, Any] = {
            "context_window": int(best["context_window"]),
            "max_output_tokens": int(best["max_output_tokens"]),
        }
        # 模态只在这家有值时落键（没收录的模型保持"两个数字"的老形状）
        for field in ("input_modalities", "output_modalities"):
            if best.get(field):
                picked[field] = list(best[field])
        out[model_id] = picked
    return out


def _better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """采值优先级：官方 vendor 源 > 票数众数 > provider 名字典序。"""
    if candidate["vendor"] != current["vendor"]:
        return candidate["vendor"]
    if candidate["votes"] != current["votes"]:
        return candidate["votes"] > current["votes"]
    return candidate["provider"] < current["provider"]


# ── 本地缓存（第 3 级）──────────────────────────────────────────


def _load_cache_locked() -> None:
    global _cache_loaded  # _cache 只做原地赋值（无需 global）
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning(f"[模型库] 缓存读取失败（忽略）: {type(exc).__name__}: {exc}")
        return
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return
    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            continue
        context = _as_count(entry.get("context_window")) or 0
        output = _as_count(entry.get("max_output_tokens")) or 0
        if not context:
            continue
        cached: dict[str, Any] = {"context_window": context, "max_output_tokens": output}
        for field in ("input_modalities", "output_modalities"):
            items = _str_list(entry.get(field))
            if items:
                cached[field] = items
        _cache[_bare(model_id)] = cached


def _write_cache_locked() -> None:
    """把本进程问过的模型写进缓存（与既有条目合并，不清空之前的结果）。

    只写问过的模型：文档是全量聚合体，整份落盘（数 MB）没必要——缓存要的是
    「这台网关服务过的模型」，几个到几十条。
    """
    global _cache
    if _index is None or not _asked:
        return
    merged = dict(_cache)
    for model_id in _asked:
        if model_id in _index:
            merged[model_id] = _index[model_id]
    payload = {
        "fetched_at_ms": int(time.time() * 1000),
        "source": URL,
        "models": dict(sorted(merged.items())),
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = CACHE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, CACHE_PATH)
    except Exception as exc:
        # 缓存是辅助产物：写不出来只告警，内存索引照用（与快照同姿态）
        logger.warning(f"[模型库] 缓存写入失败(不影响兜底): {type(exc).__name__}: {exc}")
        return
    _cache = merged


# ── 小工具 ──────────────────────────────────────────────────────


def _bare(model_id: Any) -> str:
    """裸 id：取最后一段路径并小写（models.dev 里同一模型有裸名与 `openai/gpt-5.5` 两种形态）。"""
    return str(model_id or "").split("/")[-1].strip().lower()


def _as_count(value: Any) -> int | None:
    """数值化 token 计数：接受 int / 数字字符串；其余（含 bool / NaN）返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):   # NaN / inf
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _str_list(value: Any) -> list[str]:
    """模态列表归一：转字符串、去空白、小写、保序去重；非列表或全空 → 空列表。

    上游同一件事写法不一（`"Image"` / `" image "`），归一后候选判定才能稳定比对。
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if text and text not in out:
            out.append(text)
    return out
