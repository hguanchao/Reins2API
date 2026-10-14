"""出站 chat 请求怪癖归一管线（移植 wb2api payload.go + tool_pairing.go +
thinking.go + cache_key.go，单 pass，顺序即 Go 版顺序不可换）。

上游是 OpenAI chat 方言但有若干硬怪癖（不归一就 400 或语义错误）——本模块是
「让请求通过」的兼容层；网关桥对 responses/anthropic 客户端已产出部分正确字段，
此管线对其幂等，对 chat 直传客户端是完整兜底。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from providers import parts, profiles

# 本网关注入的缓存键形态 `wb2a-<uid8>-<32hex>`；用它区分「客户端自带键」与
# 「上一轮我们自己写的键」——换号重试时必须按新 uid 重算，不能沿用上个账号的键。
_GATEWAY_KEY_RE = re.compile(r"^wb2a-.+-[0-9a-f]{32}$")

# effort 档位从低到高（payload.go effortRank）。
_EFFORT_RANK = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
# GPT 系上游接受的 max_tokens 下限（<16 一律 400 11133，15 拒 16 过）。
_GPT_MIN_MAX_TOKENS = 16
# DeepSeek 官方客户端默认档兜底（configure thinking 无来源时 fallback 'high'）。
_DEFAULT_DEEPSEEK_EFFORT = "high"


def shape_workbuddy_body(
    body: dict[str, Any],
    supported_efforts: dict[str, list[str]] | None = None,
    default_efforts: dict[str, str] | None = None,
) -> bool:
    """对出站 body 做 in-place 单 pass 归一，返回是否发生改动。

    顺序不可换（wb2api payload.go）：thinking 注入先于 effort 降级——补入的
    默认档也要走降级管线；tool 配对三步先合并再重排再清理。
    """
    changed = False
    if body.get("stream") is not True:
        body["stream"] = True
        changed = True
    if _translate_max_completion_tokens(body):
        changed = True
    if _clamp_gpt_min_max_tokens(body):
        changed = True
    if not isinstance(body.get("stream_options"), dict):
        body["stream_options"] = {"include_usage": True}
        changed = True
    if _normalize_tool_choice(body):
        changed = True
    if _normalize_tool_patterns(body):
        changed = True
    if _normalize_roles(body):
        changed = True
    if _normalize_image_url(body):
        changed = True
    if parts.normalize_chat_message_parts(body, accepted_parts=profiles.WORKBUDDY.accepted_parts):
        changed = True
    messages = body.get("messages")
    if isinstance(messages, list):
        # 顺序不能换：先合并背靠背 tool_calls（11148 正面修复），repack 才看得到完整一批。
        if _merge_adjacent_tool_calls(messages):
            changed = True
        if _repack_tool_result_blocks(messages):
            changed = True
        if _cleanup_orphan_tool_calls(messages):
            changed = True
    model = str(body.get("model") or "")
    if _inject_thinking(body, (default_efforts or {}).get(model, "")):
        changed = True
    if _normalize_reasoning_effort(body, supported_efforts or {}):
        changed = True
    if _backfill_reasoning_content(body):
        changed = True
    return changed


# ── 字段翻译与归一（payload.go）──


def _translate_max_completion_tokens(body: dict[str, Any]) -> bool:
    """max_completion_tokens → max_tokens（上游只认后者，别名透传回落 32000 截断）。
    显式 max_tokens 优先（别名只删）；非正数值不翻译（0/null 语义是未设置）。"""
    if "max_completion_tokens" not in body:
        return False
    alias = body.pop("max_completion_tokens")
    if "max_tokens" in body:
        return True  # 别名只删不译
    if isinstance(alias, bool):
        return False
    if isinstance(alias, int) and alias > 0:
        body["max_tokens"] = alias
        return True
    if isinstance(alias, float) and alias > 0 and alias == int(alias):
        body["max_tokens"] = int(alias)
        return True
    return False


def _clamp_gpt_min_max_tokens(body: dict[str, Any]) -> bool:
    """GPT 系模型 max_tokens < 16 抬到下限（Claude Code 切模型发极小探针会 400）。"""
    model = str(body.get("model") or "")
    if "gpt-" not in model.lower():
        return False
    value = body.get("max_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value >= _GPT_MIN_MAX_TOKENS:
        return False
    body["max_tokens"] = _GPT_MIN_MAX_TOKENS
    return True


def _normalize_tool_choice(body: dict[str, Any]) -> bool:
    """tool_choice 字符串化（上游该字段是 string，对象形式 400 11101）。

    "none" → 删 tool_choice + tools/functions；auto/required → 字符串；
    {"type":"function",...} → 函数名；其他对象/非标量 → 删 tool_choice。
    """
    if "tool_choice" not in body:
        return False
    choice = body["tool_choice"]

    def suppress() -> None:
        body.pop("tools", None)
        body.pop("functions", None)

    if isinstance(choice, str):
        if choice.strip().lower() == "none":
            body.pop("tool_choice")
            suppress()
            return True
        return False
    if isinstance(choice, dict):
        kind = str(choice.get("type") or "").strip().lower()
        if kind == "none":
            body.pop("tool_choice")
            suppress()
            return True
        if kind in ("auto", "required"):
            body["tool_choice"] = kind
            return True
        if kind == "function":
            function = choice.get("function")
            name = function.get("name") if isinstance(function, dict) else ""
            name = str(name or choice.get("name") or "").strip()
            body["tool_choice"] = name or "auto"
            return True
        body.pop("tool_choice")
        return True
    body.pop("tool_choice")
    return True


def _normalize_tool_patterns(node: Any) -> bool:
    r"""tools 子树里 pattern 值与 patternProperties 键的 `\\_` → `_`（上游校验器
    比主流正则引擎更严，`\_` 整体拒收 400 11129；归一无损）。"""
    changed = False
    if isinstance(node, dict):
        pattern = node.get("pattern")
        if isinstance(pattern, str) and "\\_" in pattern:
            node["pattern"] = pattern.replace("\\_", "_")
            changed = True
        props = node.get("patternProperties")
        if isinstance(props, dict):
            rebuilt = {k.replace("\\_", "_"): v for k, v in props.items()}
            if rebuilt != props:
                node["patternProperties"] = rebuilt
                changed = True
        for value in node.values():
            changed = _normalize_tool_patterns(value) or changed
    elif isinstance(node, list):
        for value in node:
            changed = _normalize_tool_patterns(value) or changed
    return changed


def _normalize_roles(body: dict[str, Any]) -> bool:
    """developer role → system（上游 role 白名单，400 11128；只改 role 值不动消息）。"""
    changed = False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if isinstance(role, str) and role.strip().lower() == "developer":
            msg["role"] = "system"
            changed = True
    return changed


def _normalize_image_url(body: dict[str, Any]) -> bool:
    """image_url 字符串 → 对象形态（上游只认对象，字符串 400 11101）。"""
    changed = False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url:
                part["image_url"] = {"url": image_url}
                changed = True
    return changed


# ── tool 配对三步（tool_pairing.go；顺序：merge → repack → cleanup）──


def _empty_content(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _merge_reasoning_content(dst: dict[str, Any], src: dict[str, Any]) -> None:
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or rc == "":
        return
    prev = dst.get("reasoning_content")
    dst["reasoning_content"] = f"{prev}\n{rc}" if isinstance(prev, str) and prev else rc


def _merge_adjacent_tool_calls(messages: list[Any]) -> bool:
    """背靠背 assistant.tool_calls 合成一条 / 纯正文 assistant 折进前一条
    （上游 deepseek 系对「声明调用的 assistant 后紧跟另一条 assistant」判 11148）。"""
    if len(messages) < 2:
        return False
    out: list[Any] = []
    changed = False
    for item in messages:
        msg = item if isinstance(item, dict) else None
        role = str(msg.get("role") or "") if msg else ""
        if msg is not None and role == "assistant" and out:
            calls = msg.get("tool_calls")
            prev = out[-1] if isinstance(out[-1], dict) else None
            prev_role = str(prev.get("role") or "") if prev else ""
            prev_calls = prev.get("tool_calls") if prev else None
            # 形态一：本条带 tool_calls 且无正文 → 并入上一条同形态消息。
            if (isinstance(calls, list) and calls and _empty_content(msg.get("content"))
                    and prev is not None and prev_role == "assistant"
                    and isinstance(prev_calls, list) and prev_calls):
                prev["tool_calls"] = [*prev_calls, *calls]
                _merge_reasoning_content(prev, msg)
                changed = True
                continue
            # 形态二（反向）：本条是纯正文 assistant，上一条是带 tool_calls 无正文
            # 的 assistant → 正文折进上一条（正文与调用声明拆两条的客户端回放形态）。
            if ("tool_calls" not in msg
                    and isinstance(msg.get("content"), str) and msg["content"]
                    and prev is not None and prev_role == "assistant"
                    and isinstance(prev_calls, list) and prev_calls
                    and _empty_content(prev.get("content"))):
                prev["content"] = msg["content"]
                _merge_reasoning_content(prev, msg)
                changed = True
                continue
        out.append(item)
    if not changed:
        return False
    messages[:] = out  # Go 版靠返回值赋值回写；in-place 语义等价
    return True


def _repack_tool_result_blocks(messages: list[Any]) -> bool:
    """把插在 assistant.tool_calls 与其 tool 结果之间的非 tool 消息挪到整组之后
    （Codex image_resize_notice 插中间 → 11148）。只调顺序不改内容。"""
    if len(messages) < 3:
        return False
    out: list[Any] = []
    changed = False
    i = 0
    while i < len(messages):
        msg = messages[i] if isinstance(messages[i], dict) else None
        if msg is None or msg.get("role") != "assistant":
            out.append(messages[i])
            i += 1
            continue
        tcs = msg.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            out.append(messages[i])
            i += 1
            continue
        want = {tc.get("id") for tc in tcs if isinstance(tc, dict) and tc.get("id")}
        out.append(messages[i])
        i += 1
        results: list[Any] = []
        between: list[Any] = []
        saw_non_tool = False
        while i < len(messages):
            nxt = messages[i] if isinstance(messages[i], dict) else None
            if nxt is None:
                break
            role = str(nxt.get("role") or "")
            if role == "tool":
                if nxt.get("tool_call_id") not in want:
                    break
                results.append(messages[i])
                if saw_non_tool:
                    changed = True
                i += 1
                continue
            if not results:
                break  # assistant 后没有结果：交给 cleanupOrphanToolCalls
            if role == "assistant" and isinstance(nxt.get("tool_calls"), list) and nxt["tool_calls"]:
                break  # 下一组组头绝不当插入物吞掉，交还外层循环
            between.append(messages[i])
            saw_non_tool = True
            i += 1
        out.extend(results)
        out.extend(between)
    if not changed:
        return False
    messages[:] = out
    return True


def _cleanup_orphan_tool_calls(messages: list[Any]) -> bool:
    """剔除无法配对的 tool_call 与 tool 结果（工具执行失败的坏历史会让上游对
    之后每条消息都 400，整条会话报废——这是让会话自愈的安全网）。"""
    if not messages:
        return False
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "tool" and isinstance(item.get("tool_call_id"), str) and item["tool_call_id"]:
            result_ids.add(item["tool_call_id"])
        elif role == "assistant" and isinstance(item.get("tool_calls"), list):
            for tc in item["tool_calls"]:
                if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc["id"]:
                    call_ids.add(tc["id"])
    if not call_ids and not result_ids:
        return False
    keep = {cid for cid in call_ids if cid in result_ids}
    changed = False
    # ① assistant.tool_calls 按 keep 对称裁剪（只留有结果的调用；过滤后为空删键）。
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        tcs = item.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            continue
        kept = [tc for tc in tcs if isinstance(tc, dict) and tc.get("id") in keep]
        if len(kept) == len(tcs):
            continue
        changed = True
        if kept:
            item["tool_calls"] = kept
        else:
            item.pop("tool_calls", None)
    # ② 孤儿 tool 结果整条删除。
    kept_messages: list[Any] = []
    for item in messages:
        if (isinstance(item, dict) and item.get("role") == "tool"
                and item.get("tool_call_id") not in keep):
            changed = True
            continue
        kept_messages.append(item)
    if not changed:
        return False
    messages[:] = kept_messages
    return True


# ── DeepSeek 思维链（thinking.go）──


def _is_deepseek_model(model: str) -> bool:
    return model.strip().lower().startswith("deepseek")


def _inject_thinking(body: dict[str, Any], default_effort: str) -> bool:
    """DeepSeek 系注入 thinking:{type:enabled} + 缺档补默认档（官方客户端行为）。

    显式 thinking.type 非空 = 客户端显式控制：disabled 尊重并删 effort；
    enabled 缺 effort 补默认档。无 thinking / type 空 → 注入 enabled + 补档。
    """
    model = str(body.get("model") or "")
    if not _is_deepseek_model(model):
        return False
    thinking = body.get("thinking")
    kind = ""
    if isinstance(thinking, dict):
        kind = str(thinking.get("type") or "").strip()
    if kind:
        if kind.lower() == "disabled":
            removed = "reasoning_effort" in body or "reasoningEffort" in body
            body.pop("reasoning_effort", None)
            body.pop("reasoningEffort", None)
            return removed
        return _ensure_deepseek_effort(body, default_effort)
    if isinstance(thinking, dict):
        thinking["type"] = "enabled"
    else:
        body["thinking"] = {"type": "enabled"}
    return _ensure_deepseek_effort(body, default_effort) or True


def _ensure_deepseek_effort(body: dict[str, Any], default_effort: str) -> bool:
    if "reasoning_effort" in body or "reasoningEffort" in body:
        return False
    body["reasoning_effort"] = default_effort or _DEFAULT_DEEPSEEK_EFFORT
    return True


def _normalize_reasoning_effort(body: dict[str, Any], supported: dict[str, list[str]]) -> bool:
    """按模型 supportedEfforts 降级 reasoning_effort（snake/camel 双字段）：
    请求档位支持 → 原样；不支持 → ≤请求档的最高支持档；支持档全更高 → 最低档。"""
    if not supported:
        return False
    model = str(body.get("model") or "")
    efforts = supported.get(model)
    if not efforts:
        return False
    key = "reasoning_effort" if "reasoning_effort" in body else (
        "reasoningEffort" if "reasoningEffort" in body else "")
    if not key:
        return False
    requested = body[key]
    if not isinstance(requested, str) or not requested.strip():
        return False
    req = requested.strip().lower()
    req_idx = _EFFORT_RANK.get(req)
    if req_idx is None:
        return False
    best, best_idx = "", -1
    for candidate in efforts:
        idx = _EFFORT_RANK.get(str(candidate).strip().lower())
        if idx is not None and idx <= req_idx and idx > best_idx:
            best, best_idx = str(candidate), idx
    if best:
        if best.lower() != req:
            body[key] = best
            return True
        return False
    lowest, lowest_idx = "", 1 << 30
    for candidate in efforts:
        idx = _EFFORT_RANK.get(str(candidate).strip().lower())
        if idx is not None and idx < lowest_idx:
            lowest, lowest_idx = str(candidate), idx
    if lowest:
        body[key] = lowest
        return True
    return False


def _backfill_reasoning_content(body: dict[str, Any]) -> bool:
    """DeepSeek 多轮一致性：assistant 消息保证 reasoning_content / reasoning 都是
    非空 string（requiresReasoningContentOnAssistantMessages；部分租户对
    len(reasoning)>0 校验：缺失/null/空串 400，空白串 200——皆无时补单空格）。"""
    model = str(body.get("model") or "")
    if not _is_deepseek_model(model):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    thinking = body.get("thinking")
    thinking_enabled = (isinstance(thinking, dict)
                        and str(thinking.get("type") or "").strip().lower() == "enabled")
    has_trace = False
    for item in messages:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("reasoning"), str) and item["reasoning"]:
            has_trace = True
            break
        if "reasoning_content" in item:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return False
    changed = False
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        rc = item.get("reasoning_content")
        if not isinstance(rc, str):
            reasoning = item.get("reasoning")
            rc = reasoning if isinstance(reasoning, str) else ""
            item["reasoning_content"] = rc
            changed = True
        reasoning = item.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            continue
        item["reasoning"] = rc if rc else " "
        changed = True
    return changed


def ensure_console_system(body: dict[str, Any]) -> bool:
    """global 域兜底 system 注入（wb2api ensureConsoleSystem，PR #45）：
    首条消息非 system 时在最前补一条中性 system，防 console 域上游 code 11-128。"""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if isinstance(first, dict) and str(first.get("role") or "").strip().lower() == "system":
        return False
    body["messages"] = [
        {"role": "system", "content": "You are a helpful assistant."},
        *messages,
    ]
    return True


# ── prompt_cache_key（cache_key.go，P0 费用优化：命中上游前缀缓存费用降 ~17×）──


def inject_prompt_cache_key(body: dict[str, Any], uid: str, conversation_id: str) -> bool:
    """注入按账号隔离的稳定缓存键 `wb2a-<uid8>-<convHex>`。

    优先级：客户端自带的 prompt_cache_key → 原值保留；body 带 conversation_id →
    用它做会话哈希源；否则用入站 conversation_id。uid 是硬隔离因子——跨账号
    复用同键会让上游命中错账号的前缀缓存、泄露对方对话。

    本函数在轮换循环内被逐次调用：换号重试时 body 里还留着上一个账号的键，
    必须**按本次 uid 重算覆盖**（否则 B 号带着 A 号的键打上游，正好踩中上面那条
    隔离失效）。只尊重「客户端自带」的键——网关键有固定形态，用它来区分。
    """
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing and not _GATEWAY_KEY_RE.match(existing):
        return False  # 客户端自带键：尊重原值
    conv = conversation_id
    for key in ("conversation_id", "conversationId"):
        value = body.get(key)
        if isinstance(value, str) and value:
            conv = value
            break
    uid8 = (uid or "-")[:8]
    digest = hashlib.sha256(f"{uid}|{conv}".encode()).hexdigest()[:32]
    new_key = f"wb2a-{uid8}-{digest}"
    if existing == new_key:
        return False
    body["prompt_cache_key"] = new_key
    return True
