"""workbuddy provider 单元测试（黄金案例移植自 wb2api 语义）。

运行：cd server && uv run python -m pytest tests/test_workbuddy.py -q
（无 pytest 时：uv run python tests/test_workbuddy.py 直接执行内建 main）
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.workbuddy import checkin, classify, dsml, sanitize, shape
from providers.workbuddy import upstream as wb_upstream
from providers.workbuddy.pool import Pool
from providers.workbuddy.provider import WorkBuddyProvider, _parse_prefix
from providers.workbuddy.store import Account, WorkBuddyStore
from providers.workbuddy.streamwrap import SseRepairStream

# ── shape.py：怪癖归一（wb2api payload 案例移植）──


def test_shape_forces_stream_and_usage():
    body = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]}
    assert shape.shape_workbuddy_body(body) is True
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_shape_translates_max_completion_tokens():
    body = {"model": "m", "max_completion_tokens": 128, "messages": []}
    shape.shape_workbuddy_body(body)
    assert "max_completion_tokens" not in body
    assert body["max_tokens"] == 128
    # 显式 max_tokens 优先：别名只删不译
    body = {"model": "m", "max_tokens": 10, "max_completion_tokens": 128, "messages": []}
    shape.shape_workbuddy_body(body)
    assert body["max_tokens"] == 10
    # 非正数不翻译
    body = {"model": "m", "max_completion_tokens": 0, "messages": []}
    shape.shape_workbuddy_body(body)
    assert "max_tokens" not in body


def test_shape_clamps_gpt_min_tokens():
    body = {"model": "gpt-6-sol", "max_tokens": 8, "messages": []}
    shape.shape_workbuddy_body(body)
    assert body["max_tokens"] == 16
    body = {"model": "hy3", "max_tokens": 8, "messages": []}
    shape.shape_workbuddy_body(body)
    assert body["max_tokens"] == 8  # 非 GPT 系不动


def test_shape_normalizes_tool_choice():
    body = {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}}}
    shape.shape_workbuddy_body(body)
    assert body["tool_choice"] == "f"
    body = {"model": "m", "messages": [], "tool_choice": {"type": "auto"}}
    shape.shape_workbuddy_body(body)
    assert body["tool_choice"] == "auto"
    body = {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": {"type": "none"}}
    shape.shape_workbuddy_body(body)
    assert "tool_choice" not in body and "tools" not in body


def test_shape_normalizes_roles_and_images():
    body = {"model": "m", "messages": [
        {"role": "developer", "content": "sys"},
        {"role": "user", "content": [{"type": "image_url", "image_url": "data:image/png;base64,xx"}]},
    ]}
    shape.shape_workbuddy_body(body)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"][0]["image_url"] == {"url": "data:image/png;base64,xx"}


def test_shape_tool_patterns():
    body = {"model": "m", "messages": [], "tools": [
        {"type": "function", "function": {"name": "agent_run", "parameters": {
            "type": "object",
            "patternProperties": {"^agent\\_run\\_": {}},
            "properties": {"runId": {"type": "string", "pattern": "^agent\\_run\\_"}}}}}]}
    shape.shape_workbuddy_body(body)
    props = body["tools"][0]["function"]["parameters"]
    assert props["properties"]["runId"]["pattern"] == "^agent_run_"
    assert "^agent_run_" in props["patternProperties"]


def test_shape_tool_pairing_merge_repack_cleanup():
    # 形态一：背靠背 assistant.tool_calls 合并（11148 正面修复）
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [{"id": "c00", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "assistant", "tool_calls": [{"id": "c01", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c00", "content": "r0"},
        {"role": "tool", "tool_call_id": "c01", "content": "r1"},
    ]}
    shape.shape_workbuddy_body(body)
    assert len(body["messages"][0]["tool_calls"]) == 2
    assert sum(1 for m in body["messages"] if m["role"] == "assistant") == 1
    # 配对断裂：孤儿 tool 结果剔除（会话自愈）
    body = {"model": "m", "messages": [
        {"role": "user", "content": "u"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "user", "content": "u2"},
    ]}
    shape.shape_workbuddy_body(body)
    assert all(m["role"] != "tool" for m in body["messages"])
    # 插入消息重排：tool c00 | X | tool c01 → tool c00 | tool c01 | X
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "c00", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c01", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c00", "content": "r0"},
        {"role": "user", "content": "<image_resize_notice>"},
        {"role": "tool", "tool_call_id": "c01", "content": "r1"},
    ]}
    shape.shape_workbuddy_body(body)
    roles = [(m["role"], m.get("tool_call_id")) for m in body["messages"]]
    assert roles == [("assistant", None), ("tool", "c00"), ("tool", "c01"), ("user", None)]


def test_shape_thinking_injection():
    body = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]}
    shape.shape_workbuddy_body(body)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"  # 缺档补默认档
    # 显式 disabled 尊重并删 effort
    body = {"model": "deepseek-v4", "thinking": {"type": "disabled"},
            "reasoning_effort": "high", "messages": []}
    shape.shape_workbuddy_body(body)
    assert "reasoning_effort" not in body
    # 非 deepseek 零改动
    body = {"model": "glm-5.3", "messages": []}
    shape.shape_workbuddy_body(body)
    assert "thinking" not in body


def test_shape_effort_downgrade():
    body = {"model": "deepseek-v4", "messages": [],
            "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    shape.shape_workbuddy_body(body, {"deepseek-v4": ["low", "medium"]})
    assert body["reasoning_effort"] == "medium"  # ≤max 的最高支持档
    body = {"model": "deepseek-v4", "messages": [], "reasoning_effort": "low"}
    shape.shape_workbuddy_body(body, {"deepseek-v4": ["high", "xhigh"]})
    assert body["reasoning_effort"] == "high"  # 支持档全更高 → 取最低档


def test_shape_backfills_reasoning_content():
    body = {"model": "deepseek-v4", "thinking": {"type": "enabled"}, "messages": [
        {"role": "assistant", "content": "prev"},
        {"role": "user", "content": "next"},
    ]}
    shape.shape_workbuddy_body(body)
    assistant = body["messages"][0]
    assert isinstance(assistant["reasoning_content"], str)
    assert assistant["reasoning"] == " "  # 皆无时补单空格（上游 len>0 校验）


def test_shape_prompt_cache_key():
    body = {"model": "m", "messages": []}
    shape.inject_prompt_cache_key(body, "1234567890", "conv-1")
    assert body["prompt_cache_key"].startswith("wb2a-12345678-")
    # uid 是硬隔离因子
    body2 = {"model": "m", "messages": []}
    shape.inject_prompt_cache_key(body2, "9999999999", "conv-1")
    assert body2["prompt_cache_key"] != body["prompt_cache_key"]
    # 客户端已带 → 不覆盖
    body["prompt_cache_key"] = "custom"
    assert shape.inject_prompt_cache_key(body, "u", "c") is False


def test_shape_drops_tool_calls_with_blank_function_name():
    """空 function.name 的 tool_call 必须剔除（上游 11133 直接拒参，带结果也拒）。

    形态来源：流式响应在 name 分片到达前断掉，客户端把 name:"" 写进历史，下一轮原样
    转发 → 整条会话每轮 400，只能清历史。判定收窄到「function 存在且 name 是空白串」。
    """
    def _call(call_id, name):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}

    # 带配对结果的空 name 调用：光靠"按 id 对称裁剪"挡不住（它正好命中 keep），必须否决
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [_call("c0", ""), _call("c1", "read_file")]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    ]}
    shape.shape_workbuddy_body(body)
    calls = [tc["id"] for m in body["messages"] if m.get("role") == "assistant"
             for tc in (m.get("tool_calls") or [])]
    assert calls == ["c1"]  # 脏调用与它的结果一起被对称裁掉
    assert [m["tool_call_id"] for m in body["messages"] if m.get("role") == "tool"] == ["c1"]

    # 纯空白也算脏
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [_call("c0", "   ")]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
    ]}
    shape.shape_workbuddy_body(body)
    assert not any(m.get("tool_calls") for m in body["messages"])

    # 缺 function / 缺 name 字段的占位条目**不判脏**（上游不因此拒参，不扩大打击面）
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "c0", "type": "function"},
            {"id": "c1", "type": "function", "function": {"arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file"}},
        ]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]}
    shape.shape_workbuddy_body(body)
    assert [tc["id"] for m in body["messages"] if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])] == ["c0", "c1", "c2"]

    # 批内混合：只剔脏项，其余原样
    body = {"model": "m", "messages": [
        {"role": "assistant", "tool_calls": [_call("c0", "read_file"), _call("c1", ""), _call("c2", "grep")]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]}
    shape.shape_workbuddy_body(body)
    assert [tc["id"] for m in body["messages"] if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])] == ["c0", "c2"]
    result_ids = [m["tool_call_id"] for m in body["messages"] if m.get("role") == "tool"]
    assert result_ids == ["c0", "c2"]


def test_shape_prompt_cache_key_falls_back_to_session():
    """缺 conversation_id 时必须回退会话键：不回退的话同账号所有请求共享一个常量键。"""
    # 同一会话的两轮 → 同一个键（前缀缓存能命中）
    first = {"model": "m", "messages": []}
    second = {"model": "m", "messages": []}
    shape.inject_prompt_cache_key(first, "u_1", "ses_abc")
    shape.inject_prompt_cache_key(second, "u_1", "ses_abc")
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    # 不同会话 → 不同键（否则互相驱逐）
    other = {"model": "m", "messages": []}
    shape.inject_prompt_cache_key(other, "u_1", "ses_xyz")
    assert other["prompt_cache_key"] != first["prompt_cache_key"]
    # body 里的 conversation_id 优先于回退值
    body = {"model": "m", "messages": [], "conversation_id": "conv-9"}
    shape.inject_prompt_cache_key(body, "u_1", "ses_abc")
    session_only = {"model": "m", "messages": []}
    shape.inject_prompt_cache_key(session_only, "u_1", "conv-9")
    assert body["prompt_cache_key"] == session_only["prompt_cache_key"]


# ── dsml.py：标记修复（wb2api dsml 向量移植）──


def _block(*invokes: str) -> str:
    return f"<{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} calls>{''.join(invokes)}</{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} calls>"


def _invoke(name: str, *params: str) -> str:
    inner = "".join(params)
    return (f"<{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} invoke name=\"{name}\">"
            f"{inner}</{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} invoke>")


def _param(name: str, value: str, string: bool = True) -> str:
    attr = ' string="true"' if string else ""
    return (f"<{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} parameter name=\"{name}\"{attr}>"
            f"{value}</{dsml.MARKUP_PIPE}DSML{dsml.MARKUP_PIPE} parameter>")


def test_dsml_strict_and_weak():
    block = _block(_invoke("exec_command", _param("cmd", "ls -la")))
    # 严格判定：名字在名单内 → 还原
    repair = dsml.enabled_repair({"exec_command"})
    emit, calls = repair.feed(f"before{block}after")
    assert emit == "beforeafter"
    assert len(calls) == 1 and calls[0].name == "exec_command"
    assert json.loads(calls[0].arguments) == {"cmd": "ls -la"}
    assert repair.converted == 1 and repair.flush() == ""
    # 名单内但块内夹正文 → 拒绝原文回吐（不吞字节）
    bad = _block(f"junk{_invoke('exec_command', _param('cmd', 'ls'))}")
    repair = dsml.enabled_repair({"exec_command"})
    emit, calls = repair.feed(bad)
    assert emit == bad and not calls and repair.rejected == 1
    # 弱判定：名单为空 + 工具名形如标识符 → 还原
    repair = dsml.enabled_repair(set())
    assert repair is None  # v1 语义：无名单不启用
    # 名单外工具名 → 拒绝
    repair = dsml.enabled_repair({"other_tool"})
    emit, calls = repair.feed(block)
    assert emit == block and not calls


def test_dsml_split_across_chunks_and_flush():
    repair = dsml.enabled_repair({"exec_command"})
    block = _block(_invoke("exec_command", _param("cmd", "ls")))
    cut = len("before<｜｜DSML｜｜ ca")
    emit1, c1 = repair.feed(("before" + block)[:cut])
    emit2, c2 = repair.feed(("before" + block)[cut:])
    assert c1 == [] and len(c2) == 1
    assert emit1 + emit2 == "before"
    # 未闭合块 flush 原文回吐（连起始标记）
    repair = dsml.enabled_repair({"exec_command"})
    head = "text <｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name=\"exec"
    emit, _ = repair.feed(head)
    assert emit == "text "
    assert repair.flush() == head[len("text "):]


def test_dsml_oversize_block_rejected():
    repair = dsml.enabled_repair({"exec_command"})
    big = "x" * (dsml.MAX_BLOCK_BYTES + 1)
    emit, calls = repair.feed(dsml.MARKUP_OPEN + big)
    assert calls == []
    assert emit.startswith(dsml.MARKUP_OPEN) and big in emit  # 原文回吐不吞字节


def test_dsml_json_vs_string_params():
    block = _block(_invoke("t", _param("n", "42", string=False), _param("s", "a\"b\nc")))
    repair = dsml.enabled_repair({"t"})
    _, calls = repair.feed(block)
    assert json.loads(calls[0].arguments) == {"n": 42, "s": 'a"b\nc'}


def test_dsml_tool_name_allowlist():
    body = {
        "tools": [{"type": "function", "function": {"name": "bash"}}],
        "functions": [{"name": "legacy"}],
        "messages": [{"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "from_history"}}]}],
    }
    assert dsml.tool_name_allowlist(body) == {"bash", "legacy", "from_history"}


# ── streamwrap.py：SSE 包装 ───


def _chunk_events(chunks: list[bytes], repair=None, on_error=None) -> list[dict]:
    stream = SseRepairStream(iter(chunks), repair, on_error)
    raw = b"".join(stream)
    events = []
    for line in raw.decode().split("\n\n"):
        line = line.strip()
        if line.startswith("data: "):
            events.append(line[len("data: "):])
    return events


def _json_events(events: list[str]) -> list[dict]:
    """解析 data 事件（[DONE] 哨兵除外）。"""
    return [json.loads(e) for e in events if e != "[DONE]"]


def test_streamwrap_passthrough_and_error_observer():
    seen: list[str] = []
    frames = [
        b'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"error":{"code":"6004","message":"rate limited"}}\n\n',
        b"data: [DONE]\n\n",
        b'data: {"ignored":"after done"}\n\n',
    ]
    events = _chunk_events(frames, on_error=seen.append)
    assert len(events) == 3 and "error" in events[1]  # error 帧原样透出
    assert len(seen) == 1 and "6004" in seen[0]       # 观察者收到 payload
    assert all("ignored" not in e for e in events)    # [DONE] 后数据丢弃


def test_streamwrap_repairs_markup_into_tool_calls():
    repair = dsml.enabled_repair({"exec_command"})
    block = _block(_invoke("exec_command", _param("cmd", "ls")))
    frames = [
        f'data: {json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": f"run{block}"}, "finish_reason": None}]}, ensure_ascii=False)}\n\n'.encode(),
        b'data: [DONE]\n\n',
    ]
    events = _chunk_events(frames, repair=repair)
    parsed = _json_events(events)
    # 展开：正文帧（标记摘除）+ tool_call 帧 + finish 帧 + [DONE]
    assert parsed[0]["choices"][0]["delta"]["content"] == "run"
    assert parsed[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "exec_command"
    assert parsed[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[-1] == "[DONE]"


def test_streamwrap_synthetic_finish_when_upstream_omits():
    repair = dsml.enabled_repair({"t"})
    block = _block(_invoke("t", _param("x", "1")))
    frames = [
        f'data: {json.dumps({"choices": [{"index": 0, "delta": {"content": block}}]}, ensure_ascii=False)}\n\n'.encode(),
        # 上游不发 [DONE]，直接 EOF
    ]
    events = _chunk_events(frames, repair=repair)
    parsed = _json_events(events)
    assert any(p.get("choices", [{}])[0].get("finish_reason") == "tool_calls" for p in parsed)
    assert events[-1] == "[DONE]"  # 兜底补发


# ── classify.py：分类表（wb2api Classify 语义）──


def test_classify_table():
    cases: list[tuple[int, str, str]] = [
        (400, '{"code":"11102","msg":"service info not found"}', classify.MODEL_BLOCKED),
        (402, '{"code":14003,"msg":"insufficient credit"}', classify.HARD_CREDIT),
        (401, '{"code":"12153","msg":"Offline user session not found"}', classify.SESSION_DEAD),
        (403, '{"code":"11140","msg":"request illegal"}', classify.ACCOUNT_FAULT),
        (429, '{"code":"6004","msg":"将在 2026-10-12 00:00:00 重置"}', classify.SOFT_RATE),
        (429, '{"code":"14018","msg":"credits exhausted"}', classify.HARD_CREDIT),
        (429, '{"code":6004,"msg":"model usage limit"}', classify.SOFT_RATE),
        (403, "<html>blocked by WAF</html>", classify.WAF_BLOCK),
        (404, '{"msg":"not found"}', classify.NOT_FOUND),
        (500, "internal error", classify.SERVER),
        (400, '{"code":11115,"msg":"prompt is too long"}', classify.PROMPT_TOO_LONG),
        (400, '{"code":11101,"msg":"Unmarshal chat params failed"}', classify.BAD_PARAMS),
        (400, '{"code":11135,"msg":"invalid image"}', classify.IMAGE_INVALID),
        (400, '{"msg":"blocked by security policy"}', classify.CONTENT_BLOCKED),
        (400, '{"code":"11128","msg":"unknown role"}', classify.CLIENT),
    ]
    for status, body, expected in cases:
        assert classify.classify(status, body).kind == expected, (status, body)


def test_classify_6004_reset_parsing():
    # 重置时刻动态取未来时间：写死日期会随开发推进变成过去式，断言必然失败（曾踩）。
    future = datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=2)
    stamp = future.strftime("%Y-%m-%d %H:%M:%S")
    result = classify.classify(429, json.dumps(
        {"code": "6004", "msg": f"将在 {stamp} 重置"}, ensure_ascii=False
    ))
    assert result.model_rate_limit is True
    assert result.model_reset_at_ms > int(time.time() * 1000)
    result = classify.classify(429, '{"msg":"rate limit exceeded"}')
    assert result.model_rate_limit is False


def test_classify_retry_after_header():
    result = classify.apply_retry_after(classify.classify(429, "rate limit"),
                                        {"Retry-After": "30"})
    assert result.retry_after_s == 30.0
    # 超上限丢弃
    result = classify.apply_retry_after(classify.classify(429, "rate limit"),
                                        {"retry-after": "999999"})
    assert result.retry_after_s == 0.0


def test_classify_hard_cooldown_until():
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 11, 15, 0, 0, tzinfo=tz)
    until = classify.hard_cooldown_until_ms(now)
    assert until > int(now.timestamp() * 1000)
    at = datetime.fromtimestamp(until / 1000, tz)
    assert (at.hour, at.minute) == (4, 0) and at.day == 12  # 次日 04:00


# ── store.py：accounts 表 ───


def test_store_crud_and_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            account = Account(uid="u_1", label="主号", access_token="at", refresh_token="rt")
            store.upsert_account(account)
            loaded = store.get_account("u_1")
            assert loaded is not None and loaded.access_token == "at"
            # 运行态增量更新
            store.update_runtime("u_1", cooldown_until_ms=123, cooldown_kind="soft",
                                 model_cooldowns={"m": {"until_ms": 1, "kind": "6004"}})
            loaded = store.get_account("u_1")
            assert loaded.cooldown_until_ms == 123
            assert loaded.model_cooldowns["m"]["kind"] == "6004"
            # token 刷新
            store.update_tokens("u_1", "at2", "rt2", 999)
            assert store.get_account("u_1").access_token == "at2"
            # uid 白名单
            assert not store.get_account("../evil")
            assert store.delete_account("../evil") is False
            assert store.delete_account("u_1") is True
            assert store.get_account("u_1") is None
        finally:
            store.close()  # Windows 下必须先关连接，否则 TemporaryDirectory 清理报占用


# ── 别名前缀（cn:/glb:/oc:）与 realm 路由 ──


def _provider_with_catalog(store: WorkBuddyStore, cn: list[str], glb: list[str]) -> WorkBuddyProvider:
    provider = WorkBuddyProvider(store=store)
    provider._catalogs["cn"].models = {m: {"id": m} for m in cn}
    provider._catalogs["global"].models = {m: {"id": m} for m in glb}
    provider._catalogs["cn"].refreshed_at = provider._catalogs["global"].refreshed_at = "t"
    return provider


def test_parse_prefix_table():
    assert _parse_prefix("cn:deepseek-v4.1-flash") == ("cn", "deepseek-v4.1-flash")
    assert _parse_prefix("glb:deepseek-v4.1-flash") == ("global", "deepseek-v4.1-flash")
    # 裸名 / 旧式 provider/ 前缀 / 无关冒号名 → 不声明 realm
    assert _parse_prefix("deepseek-v4.1-flash") == (None, "deepseek-v4.1-flash")
    assert _parse_prefix("workbuddy/x") == (None, "workbuddy/x")
    assert _parse_prefix("a:b") == (None, "a:b")


def test_provider_route_strips_alias_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            provider = _provider_with_catalog(store, ["cn-only", "both"], ["glb-only", "both"])
            # cn:/glb: 各自剥前缀、定域；规范名回写别名，裸名发上游
            route = provider.route("cn:cn-only")
            assert (route.upstream_model, route.realm, route.model) == ("cn-only", "cn", "cn:cn-only")
            route = provider.route("glb:glb-only")
            assert (route.upstream_model, route.realm, route.model) == ("glb-only", "global", "glb:glb-only")
            # 同名跨域：前缀决定域
            assert provider.route("cn:both").realm == "cn"
            assert provider.route("glb:both").realm == "global"
            # 旧式 workbuddy/ 与裸名：按哪个册里有判定
            assert provider.route("workbuddy/glb-only").realm == "global"
            assert provider.route("glb-only").realm == "global"
            assert provider.route("cn-only").realm == "cn"
            # 未知模型（前缀正确但目录没有）→ ModelNotFound
            assert provider.supported("cn:nope") is False
            assert provider.supported("cn:cn-only") is True
        finally:
            store.close()


def test_provider_list_models_uses_alias_and_merges_realms():
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            provider = _provider_with_catalog(store, ["cn-only", "both"], ["glb-only", "both"])
            ids = [m.id for m in provider.list_models()]
            # 同名跨域只列一次（cn 优先），域内独有各列一次
            assert ids == ["cn:both", "cn:cn-only", "glb:glb-only"]
            both = next(m for m in provider.list_models() if m.id == "cn:both")
            assert both.metadata["aliases"] == ["cn:both", "glb:both"]
            assert both.metadata["realm"] == "cn"
        finally:
            store.close()


def test_pool_realm_filter():
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="cn_1", realm="cn", access_token="at"))
            store.upsert_account(Account(uid="glb_1", realm="global", access_token="at"))
            pool = Pool(store)
            for _ in range(20):
                assert pool.pick("m", "s", realm="cn").uid == "cn_1"
                assert pool.pick("m", "s", realm="global").uid == "glb_1"
            # 未声明域 = 旧行为（不限域，两号都可能被选到）
            picked = {pool.pick("m", f"s{i}").uid for i in range(40)}
            assert picked == {"cn_1", "glb_1"}
            # 该域无可用号 → None（provider 译 429），不会串域借号
            store.update_runtime("glb_1", status="disabled")
            assert pool.pick("m", "s", realm="global") is None
            assert pool.pick("m", "s", realm="cn").uid == "cn_1"
        finally:
            store.close()


def test_registry_alias_routing():
    from gateway.base import ALIAS_PREFIXES, Registry

    class _Fake:
        def __init__(self, name: str):
            self.name = name

        def route(self, model: str):
            from gateway.base import Route
            return Route(provider=self.name, model=model, upstream_model=model,
                         protocol="chat", free=False)

    reg = Registry()
    reg.register(_Fake("opencode"))
    reg.register(_Fake("workbuddy"))
    reg.register(_Fake("codex"))
    # 别名前缀表本身
    assert ALIAS_PREFIXES == {
        "oc": "opencode", "cn": "workbuddy", "glb": "workbuddy", "cx": "codex",
    }
    # route_target：命中前缀 → provider；未注册前缀 → (None, prefix)
    assert reg.route_target("cn:x") == (reg.get("workbuddy"), None)
    assert reg.route_target("cx:gpt-5-codex") == (reg.get("codex"), None)
    assert reg.route_target("nope:x")[0] is None and reg.route_target("nope:x")[1] == "nope"
    assert reg.route_target("nope/x")[0] is None and reg.route_target("nope/x")[1] == "nope"
    # 裸名 → 默认 provider（首个注册）
    assert reg.route_target("bare")[0] is reg.default
    # owner（/v1/models 的 owned_by）：与 route_target 同源，未注册前缀落默认
    assert reg.owner("oc:big-pickle") == "opencode"
    assert reg.owner("cn:x") == "workbuddy" and reg.owner("cx:gpt-5-codex") == "codex"
    assert reg.owner("nope/x") == "opencode" and reg.owner("bare") == "opencode"
    # 别名指向的 provider 未注册 → 报未知，不静默落默认 provider
    only_oc = Registry()
    only_oc.register(_Fake("opencode"))
    assert only_oc.route_target("cn:x") == (None, "cn")
    assert only_oc.route_target("oc:x")[0] is only_oc.get("opencode")


# ── 签到（checkin.py / provider.checkin）──


class _StubCall:
    """替换 checkin._call：按顺序吐出 (http_status, body)，并记录实际打过的路径。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.paths: list[str] = []

    def __call__(self, account, path, *, retries):
        self.paths.append(path)
        if not self._responses:
            raise AssertionError(f"多余的上游调用: {path}")
        return self._responses.pop(0)


@contextmanager
def _stub_upstream(*responses):
    stub = _StubCall(*responses)
    original = checkin._call
    checkin._call = stub
    try:
        yield stub
    finally:
        checkin._call = original


def test_checkin_skip_rules():
    assert checkin.skip_reason(Account(uid="u", realm="cn", access_token="at")) == ""
    assert checkin.skip_reason(
        Account(uid="u", realm="cn", access_token="at", enterprise_id="ent")
    ) == "企业账号不支持签到"
    assert checkin.skip_reason(
        Account(uid="u", realm="global", access_token="at")
    ) == "global 域无签到体系"
    assert checkin.skip_reason(Account(uid="u", realm="cn")) == "缺少 access_token"
    # 不适用账号直接出 skipped，一个请求都不发
    with _stub_upstream() as stub:
        out = checkin.run_checkin(Account(uid="u", realm="global", access_token="at"))
    assert out.state == checkin.STATE_SKIPPED and out.ok and stub.paths == []


def test_checkin_status_parsing_enveloped_and_flat():
    # 新口径：{code,msg,data} 信封
    with _stub_upstream((200, {"code": 0, "msg": "ok", "data": {
        "active": True, "today_checked_in": True, "today_credit": 100,
        "streak_days": 7, "total_credits": 1200, "is_streak_day": True,
        "activity_name": "每日签到",
    }})):
        status = checkin.checkin_status(Account(uid="u", realm="cn", access_token="at"))
    assert status.active and status.today_checked_in
    assert status.today_credit == 100.0 and status.streak_days == 7
    assert status.total_credits == 1200.0 and status.is_streak_day
    assert "连续 7 天" in status.describe()

    # 旧口径：裸负载 + daily_credit 别名 + streak 对象；缺 active 不能判成活动关闭
    with _stub_upstream((200, {"daily_credit": "50", "streak": {"days": 3}})):
        status = checkin.checkin_status(Account(uid="u", realm="cn", access_token="at"))
    assert status.active is True and status.today_credit == 50.0
    assert status.streak_days == 3 and status.today_checked_in is False


def test_checkin_run_flow():
    account = Account(uid="u", realm="cn", access_token="at")

    # 未签 → 领取成功 → 回读连签/累计
    with _stub_upstream(
        (200, {"active": True, "today_checked_in": False}),
        (200, {"credit": 100}),
        (200, {"today_checked_in": True, "streak_days": 8, "total_credits": 1300}),
    ) as stub:
        out = checkin.run_checkin(account)
    assert out.state == checkin.STATE_CLAIMED and out.credit == 100.0 and out.ok
    assert out.streak_days == 8 and out.total_credits == 1300.0
    assert stub.paths == [checkin.STATUS_PATH, checkin.CLAIM_PATH, checkin.STATUS_PATH]

    # 已签：状态直接判定，绝不打领取接口（幂等靠上游，省请求靠这里）
    with _stub_upstream(
        (200, {"active": True, "today_checked_in": True, "streak_days": 9})
    ) as stub:
        out = checkin.run_checkin(account)
    assert out.state == checkin.STATE_ALREADY and out.streak_days == 9
    assert stub.paths == [checkin.STATUS_PATH]

    # 竞态：状态说未签、领取却回 null 或 code 10001 → 按已签处理，不是失败
    for body in (None, {"code": 10001, "msg": "今天已签到，请明天再来"}):
        with _stub_upstream((200, {"active": True, "today_checked_in": False}), (400, body)) as stub:
            out = checkin.run_checkin(account)
        assert out.state == checkin.STATE_ALREADY and out.ok
        assert stub.paths == [checkin.STATUS_PATH, checkin.CLAIM_PATH]

    # 活动未开启
    with _stub_upstream((200, {"active": False, "activity_name": "签到节"})):
        out = checkin.run_checkin(account)
    assert out.state == checkin.STATE_INACTIVE and out.ok and "签到节" in out.message

    # 回读失败不能把"已领取"改写成失败
    with _stub_upstream(
        (200, {"active": True, "today_checked_in": False}),
        (200, {"credit": 30}),
        (500, {"msg": "internal error"}),
    ):
        out = checkin.run_checkin(account)
    assert out.state == checkin.STATE_CLAIMED and out.credit == 30.0

    # 硬失败必须抛 CheckinError，不能静默成"已签"
    for responses in (((500, {"msg": "internal error"}),), ((200, {"code": 1, "msg": "boom"}),)):
        with _stub_upstream(*responses):
            try:
                checkin.run_checkin(account)
                raise AssertionError("应抛出 CheckinError")
            except checkin.CheckinError:
                pass


def test_checkin_provider_ledger_short_circuit():
    """台账命中即短路（不发请求）；force 绕过台账去打上游，且失败收进 state=error。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            today = checkin.beijing_today()
            store.upsert_account(Account(
                uid="u_1", realm="cn", access_token="at",
                last_checkin_day=today, checkin_streak=5, checkin_total_credits=600,
            ))
            provider = WorkBuddyProvider(store=store)

            def _explode(*args, **kwargs):
                raise checkin.CheckinError("stub: 不应发起请求")

            original = checkin._call
            checkin._call = _explode
            try:
                cached = provider.checkin("u_1")
                forced = provider.checkin("u_1", force=True)
            finally:
                checkin._call = original

            assert cached["state"] == checkin.STATE_ALREADY and cached["local"] is True
            assert cached["streak_days"] == 5 and cached["total_credits"] == 600.0
            assert forced["state"] == checkin.STATE_ERROR and "stub" in forced["message"]
        finally:
            store.close()


def test_checkin_all_only_active_accounts():
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_a", realm="cn", access_token="at"))
            store.upsert_account(Account(uid="u_p", realm="cn", access_token="at",
                                         status="paused"))
            store.upsert_account(Account(uid="u_g", realm="global", access_token="at"))
            provider = WorkBuddyProvider(store=store)
            # 领到积分后 provider 会起线程拉余额：测试里禁掉，别真打上游
            provider._refresh_credits_async = lambda uid: None

            # u_a 走完整领取；u_g 不适用；u_p 根本不进这一轮
            with _stub_upstream(
                (200, {"active": True, "today_checked_in": False}),
                (200, {"credit": 20}),
                (200, {"streak_days": 1, "total_credits": 20}),
            ):
                # 余额刷新与连登管家由 tests/test_workbuddy_tasks.py 单独覆盖：
                # 这里只验「谁进这一轮」，别让用例挂着真实上游调用。
                summary = provider.checkin_all(with_balance=False, with_streak=False)

            assert summary["total"] == 2  # active 的 u_a + global 的 u_g
            assert summary["claimed"] == 1 and summary["skipped"] == 1
            assert summary["credits"] == 20.0
            assert {item["uid"] for item in summary["items"]} == {"u_a", "u_g"}
            # 领到的号落台账，且顺带刷新积分
            assert store.get_account("u_a").last_checkin_day == checkin.beijing_today()
            assert store.get_account("u_p").last_checkin_day == ""
        finally:
            store.close()


def test_store_migrates_legacy_accounts_table():
    """老库（无签到列）打开后自动补列：CREATE TABLE IF NOT EXISTS 不会给旧表加列。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wb.db"
        legacy = sqlite3.connect(str(path))
        legacy.execute(
            "CREATE TABLE accounts (uid TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', "
            "realm TEXT NOT NULL DEFAULT 'cn', access_token TEXT NOT NULL DEFAULT '', "
            "refresh_token TEXT NOT NULL DEFAULT '', enterprise_id TEXT NOT NULL DEFAULT '', "
            "device_token TEXT NOT NULL DEFAULT '', token_expires_ms INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL DEFAULT 'active', status_reason TEXT NOT NULL DEFAULT '', "
            "cooldown_until_ms INTEGER NOT NULL DEFAULT 0, cooldown_kind TEXT NOT NULL DEFAULT '', "
            "breaker_count INTEGER NOT NULL DEFAULT 0, "
            "breaker_until_ms INTEGER NOT NULL DEFAULT 0, "
            "model_cooldowns TEXT NOT NULL DEFAULT '{}', "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0, credits_total REAL, credits_remain REAL, "
            "credits_updated_ms INTEGER NOT NULL DEFAULT 0, used_seq INTEGER NOT NULL DEFAULT 0, "
            "idle_boost REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', "
            "created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO accounts (uid, label, access_token, created_ms, updated_ms) "
            "VALUES ('u_old', '老号', 'at', 1, 1)"
        )
        legacy.commit()
        legacy.close()

        store = WorkBuddyStore(path)
        try:
            account = store.get_account("u_old")
            assert account is not None and account.label == "老号"
            assert account.last_checkin_day == "" and account.checkin_streak == 0
            assert account.checkin_total_credits is None

            store.update_runtime("u_old", last_checkin_day="2026-10-12", checkin_streak=3,
                                 checkin_total_credits=600, checkin_updated_ms=42)
            again = store.get_account("u_old")
            assert again.checked_in_on("2026-10-12") and not again.checked_in_on("2026-10-13")
            assert again.checkin_streak == 3 and again.checkin_total_credits == 600.0
            assert again.checkin_updated_ms == 42

            # 补列后整体 upsert 仍要对得上 _ACCOUNT_COLUMNS 的顺序与个数
            again.label = "改名"
            store.upsert_account(again)
            reloaded = store.get_account("u_old")
            assert reloaded.label == "改名" and reloaded.checkin_streak == 3
            assert reloaded.checkin_total_credits == 600.0
        finally:
            store.close()


# ── 帧规范化（streamwrap._normalize_frame）与软限流同号重试 ──


def test_normalize_frame_drops_upstream_noise():
    """上游那套"空占位 + 私有键"必须剥干净——ZCode 会因为它们把思考链切成一词一块。

    期望逐条对齐 wb2api sse.go normalizeFrame 的测试用例。
    """
    from providers.workbuddy.streamwrap import _normalize_frame

    # 空 content/refusal + finish_reason 空串 → 空 delta + null
    assert _normalize_frame({
        "id": "x",
        "choices": [{"index": 0, "delta": {"content": "", "refusal": ""}, "finish_reason": ""}],
    }) == {
        "id": "x",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "usage": None,
    }
    # 非空 tool_calls 保留；空列表丢弃；缺 id 时补标准 id
    kept = _normalize_frame(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "c1"}]}}]}
    )
    assert kept["choices"][0]["delta"]["tool_calls"] == [{"id": "c1"}]
    assert kept["id"] == "chatcmpl-wb2api"
    dropped = _normalize_frame(
        {"choices": [{"index": 0, "delta": {"tool_calls": [], "content": "hi"}}]}
    )
    assert dropped["choices"][0]["delta"] == {"content": "hi"}
    # 空占位 function_call（name/arguments 全空）丢弃
    placeholder = _normalize_frame(
        {"choices": [{"index": 0, "delta": {"function_call": {"name": "", "arguments": ""}}}]}
    )
    assert placeholder["choices"][0]["delta"] == {}
    # 顶层非标键丢弃；缺 usage 补 null；finish_reason 非空保留
    noisy = _normalize_frame({
        "id": "x", "object": "chat.completion.chunk", "created": 1, "junk": "noise",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    assert noisy == {
        "id": "x", "object": "chat.completion.chunk", "created": 1,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": None,
    }
    # 真实上游帧（实测抓包）：reasoning_content 与 model/created 必须原样活下来，
    # extra_fields / 空 content / 空 tool_calls / 空 finish_reason 全部消失。
    real = _normalize_frame({
        "id": "937c4f0ad29e45c78b8485e3cf8668ee",
        "model": "deepseek-v4.1-flash",
        "object": "chat.completion.chunk",
        "created": 1791792866,
        "choices": [{
            "index": 0,
            "delta": {
                "content": "", "reasoning_content": "The", "function_call": None,
                "refusal": "", "tool_calls": [], "extra_fields": None,
            },
            "logprobs": None,
            "finish_reason": "",
        }],
        "usage": None,
    })
    assert real == {
        "id": "937c4f0ad29e45c78b8485e3cf8668ee",
        "model": "deepseek-v4.1-flash",
        "object": "chat.completion.chunk",
        "created": 1791792866,
        "choices": [{"index": 0, "delta": {"reasoning_content": "The"}, "finish_reason": None}],
        "usage": None,
    }


def test_streamwrap_normalizes_frames_end_to_end():
    """整条流：噪声帧进、标准帧出，且 [DONE] 恰好一个。"""
    frames = [
        (
            b'data: {"id":"x1","object":"chat.completion.chunk","created":1,"model":"m",'
            b'"choices":[{"index":0,"delta":{"role":"assistant","content":"","reasoning_content":"",'
            b'"refusal":"","tool_calls":[],"function_call":{"name":"","arguments":""}},'
            b'"finish_reason":""}],"extra_field":"junk"}\n\n'
        ),
        (
            b'data: {"id":"x1","choices":[{"index":0,"delta":{"content":"hello"},'
            b'"finish_reason":""}]}\n\n'
        ),
        (
            b'data: {"id":"x1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"total_tokens":7}}\n\n'
        ),
        b"data: [DONE]\n\n",
    ]
    events = _chunk_events(frames)
    payloads = _json_events(events)
    assert len(payloads) == 3 and events.count("[DONE]") == 1
    assert payloads[0]["choices"][0] == {
        "index": 0, "delta": {"role": "assistant"}, "finish_reason": None,
    }
    assert "extra_field" not in payloads[0]
    assert payloads[1]["choices"][0]["delta"] == {"content": "hello"}
    assert payloads[2]["choices"][0]["finish_reason"] == "stop"
    assert payloads[2]["usage"] == {"total_tokens": 7}


def test_soft_rate_single_account_retries_same_account():
    """单号池撞 429 软限流：清掉刚罚的冷却、歇一下**同号**重试，而不是把 429 透传。

    线上实证：同一个号隔 4 秒再打就过了，而日志里 duration 只有一次上游往返
    ——说明当时根本没重试（换号轮换在单号池里被 exclude 挡住，第二轮 pick 返回 None）。
    """
    import providers.workbuddy.upstream as wb_upstream
    from core.settings import WB_SOFT_RETRY_SLEEP_S
    from gateway.base import RequestIDs

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(
                uid="u_1", realm="cn", access_token="at",
                token_expires_ms=int(time.time() * 1000) + 3_600_000,
            ))
            provider = _provider_with_catalog(store, ["cn-only"], [])
            provider._refresh_credits_async = lambda uid: None

            class _Resp:
                def __init__(self, status: int, body: bytes):
                    self.status_code = status
                    self.headers: dict[str, str] = {}
                    self._body = body

                def iter_content(self, chunk_size: int = 8192):
                    yield self._body

                def close(self) -> None:
                    pass

            rate = json.dumps({
                "code": 14003, "msg": "too many requests",
                "retry_policy": {"retry": True},
            }).encode()
            ok = json.dumps({
                "id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}],
            }).encode()

            calls: list[str] = []

            def fake_chat(account, body, **kwargs):
                calls.append(account.uid)
                return _Resp(429, rate) if len(calls) == 1 else _Resp(200, ok)

            original_chat = wb_upstream.chat_request
            original_sleep = time.sleep
            slept: list[float] = []
            wb_upstream.chat_request = fake_chat
            time.sleep = lambda s: slept.append(s)  # 别真等 2 秒
            try:
                response = provider.send(
                    provider.route("cn:cn-only"),
                    "chat",
                    json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                    RequestIDs(session="s", request="r", project="p"),
                    stream=True,
                )
            finally:
                wb_upstream.chat_request = original_chat
                time.sleep = original_sleep

            assert calls == ["u_1", "u_1"]  # 同号重试，不是换号
            assert response.status == 200
            assert slept and slept[0] == WB_SOFT_RETRY_SLEEP_S
            # 重试前把刚写下的软冷却清掉了，否则会被自己的冷却挡住
            assert store.get_account("u_1").cooldown_until_ms == 0
        finally:
            store.close()


# ── sanitize.py：出站脱敏（上游逐字审核指纹）──


def test_sanitize_rewrites_claude_code_identity_sentence():
    """线上实证的触发句：请求体里带着它就整单 400（code 11128 unapproved channel）。

    匹配串不带结尾标点——CLI 版句号收尾、桌面版（Agent SDK）逗号接后继句，
    两种形态都要覆盖，且原有标点原样保留。
    """
    cli = "You are Claude Code, Anthropic's official CLI for Claude. Be concise."
    cleaned = sanitize.sanitize_text(cli)
    assert "official CLI tool for Claude." in cleaned
    assert "official CLI for Claude" not in cleaned

    desktop = ("You are Claude Code, Anthropic's official CLI for Claude, "
               "running within the Claude Agent SDK.")
    cleaned = sanitize.sanitize_text(desktop)
    assert "official CLI tool for Claude," in cleaned
    assert cleaned.endswith("Claude Agent SDK.")


def test_sanitize_keeps_clean_text_untouched():
    """无指纹 → 原样返回；含 11128 的普通正文也不动（不移植 11-128 改写，见模块 docstring）。"""
    for text in (
        "Anthropic's official CLI for Claude",       # 缺 "You are Claude Code, " 前缀
        "the file holds the number 11128 inside",     # 裸数字今天不触发
        "Main branch you will usually use this for",  # 缺括号/后缀，不误伤零散文本
    ):
        assert sanitize.sanitize_text(text) == text


def test_sanitize_strips_billing_header_forms():
    """键值形态整段删除（含值）；裸键名（引用/示例文本）只做最小缩写。

    真实字面量从模块取（sanitize._FEATURES 里的那条特征），避免测试与实现各写一份。
    """
    token = sanitize._FEATURES[4]
    # 键值形态（带 : 或 = 分隔符）：连同值整段删除
    assert sanitize.sanitize_text(f"{token}: abc; tail") == "tail"
    assert sanitize.sanitize_text(f"{token}=1;") == ""
    # 裸键名不带分隔符：最小缩写（去掉 x- 前缀），且绝不误删整行
    assert sanitize.sanitize_text(f"see {token} for details") == f"see {token[2:]} for details"

def test_sanitize_covers_reasoning_and_tool_arguments():
    """reasoning / reasoning_content / tool_calls.arguments / 多模态 text part 全是盲区。

    工具调用轮 content 常为 null——只洗 content 的实现会跳过整条消息，
    写进工具参数的指纹原样漏出；reasoning 则是 shape 镜像回填的旁路。
    """
    identity = "You are Claude Code, Anthropic's official CLI for Claude"
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": f"quoted {identity} here",
                "reasoning": "To give feedback, users should report the issue at "
                             "https://github.com/anthropics/claude-code/issues",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "write", "arguments": json.dumps({"text": identity})},
                }],
            },
            {"role": "tool", "tool_call_id": "c1",
             "content": [{"type": "text", "text": identity}]},
        ]
    }
    assert sanitize.sanitize_body(body) is True
    dumped = json.dumps(body, ensure_ascii=False)
    assert "official CLI for Claude" not in dumped
    assert "provide feedback" in dumped                       # 只换了 give→provide
    assert "github.com/anthropics/claude-code/issues" in dumped
    assert sanitize.sanitize_body(body) is False              # 幂等：再洗无改动


def test_sanitize_body_noop_shapes():
    """坏形状不炸：无 messages / messages 非表 / 非 dict —— 一律 False 不改。"""
    assert sanitize.sanitize_body({}) is False
    assert sanitize.sanitize_body({"messages": "nope"}) is False
    assert sanitize.sanitize_body({"messages": [None, 42, "x"]}) is False


def test_send_sanitizes_body_before_upstream():
    """端到端：带指纹的请求体打到上游前已被改写（本会话自己踩过的粘性 400 回归）。"""
    import providers.workbuddy.upstream as wb_upstream
    from gateway.base import RequestIDs

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(
                uid="u_1", realm="cn", access_token="at",
                token_expires_ms=int(time.time() * 1000) + 3_600_000,
            ))
            provider = _provider_with_catalog(store, ["cn-only"], [])
            provider._refresh_credits_async = lambda uid: None

            class _Resp:
                status_code = 200

                def __init__(self, body: bytes):
                    self.headers: dict[str, str] = {}
                    self._body = body

                def iter_content(self, chunk_size: int = 8192):
                    yield self._body

                def close(self) -> None:
                    pass

            ok = json.dumps({
                "id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}],
            }).encode()
            seen: list[bytes] = []

            def fake_chat(account, body, **kwargs):
                seen.append(body)
                return _Resp(ok)

            original_chat = wb_upstream.chat_request
            wb_upstream.chat_request = fake_chat
            try:
                response = provider.send(
                    provider.route("cn:cn-only"),
                    "chat",
                    json.dumps({"messages": [
                        {"role": "system",
                         "content": "You are Claude Code, Anthropic's official CLI for Claude."},
                        {"role": "user", "content": "hi"},
                    ]}).encode(),
                    RequestIDs(session="s", request="r", project="p"),
                    stream=True,
                )
            finally:
                wb_upstream.chat_request = original_chat

            assert response.status == 200
            assert seen and b"official CLI for Claude" not in seen[0]
            assert b"official CLI tool for Claude." in seen[0]
        finally:
            store.close()


def test_send_injects_account_scoped_cache_key():
    """prompt_cache_key 按账号隔离：注入发生在选号之后（uid 已知），而非 shape 阶段。

    回归点：shape() 阶段 uid 为空会让键退化成全局常量（wb2a--<常量>），
    跨账号复用同一前缀缓存键，与 cache_key.go 的"uid 是硬隔离因子"相悖。
    """
    import providers.workbuddy.upstream as wb_upstream
    from gateway.base import RequestIDs

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(
                uid="acct0001", realm="cn", access_token="at",
                token_expires_ms=int(time.time() * 1000) + 3_600_000,
            ))
            provider = _provider_with_catalog(store, ["cn-only"], [])
            provider._refresh_credits_async = lambda uid: None

            class _Resp:
                status_code = 200

                def __init__(self) -> None:
                    self.headers: dict[str, str] = {}

                def iter_content(self, chunk_size: int = 8192):
                    yield json.dumps({"id": "x", "choices": []}).encode()

                def close(self) -> None:
                    pass

            seen: list[bytes] = []

            def fake_chat(account, body, **kwargs):
                seen.append(body)
                return _Resp()

            original = wb_upstream.chat_request
            wb_upstream.chat_request = fake_chat
            try:
                provider.send(
                    provider.route("cn:cn-only"), "chat",
                    json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                    RequestIDs(session="s", request="r", project="p"), stream=True,
                )
            finally:
                wb_upstream.chat_request = original

            assert seen
            key = json.loads(seen[0])["prompt_cache_key"]
            assert key.startswith("wb2a-acct0001-"), key
        finally:
            store.close()



# ── 回归：换号/刷新/罚号的一致性 ──


def test_send_rederives_cache_key_on_rotation():
    """换号重试时 prompt_cache_key 必须按新账号重算。

    回归点：键在轮换循环内注入，换号后 body 里还留着上一个账号的键；旧实现
    「见键即跳过注入」，B 号会带着 A 号的键打上游——正好踩中「uid 是硬隔离因子、
    跨账号复用同键会串前缀缓存」的禁令。
    """
    import providers.workbuddy.upstream as wb_upstream
    from gateway.base import RequestIDs

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            for uid in ("u_alpha", "u_beta"):
                store.upsert_account(Account(
                    uid=uid, realm="cn", access_token="at",
                    token_expires_ms=int(time.time() * 1000) + 3_600_000,
                ))
            provider = _provider_with_catalog(store, ["cn-only"], [])
            provider._refresh_credits_async = lambda uid: None

            class _Resp:
                def __init__(self, status: int, body: bytes):
                    self.status_code = status
                    self.headers: dict[str, str] = {}
                    self._body = body

                def iter_content(self, chunk_size: int = 8192):
                    yield self._body

                def close(self) -> None:
                    pass

            ok = json.dumps({"id": "x", "choices": []}).encode()
            seen: list[tuple[str, str]] = []

            def fake_chat(account, body, **kwargs):
                seen.append((account.uid, json.loads(body).get("prompt_cache_key") or ""))
                if len(seen) == 1:
                    return _Resp(500, b'{"error":{"message":"boom"}}')
                return _Resp(200, ok)

            original_chat = wb_upstream.chat_request
            original_sleep = time.sleep
            wb_upstream.chat_request = fake_chat
            time.sleep = lambda s: None
            try:
                provider.send(
                    provider.route("cn:cn-only"), "chat",
                    json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                    RequestIDs(session="s", request="r", project="p"), stream=True,
                )
            finally:
                wb_upstream.chat_request = original_chat
                time.sleep = original_sleep

            assert len(seen) == 2
            assert seen[0][0] != seen[1][0]  # 确实换号了
            for uid, key in seen:
                assert key.startswith(f"wb2a-{uid[:8]}-"), (uid, key)
        finally:
            store.close()


def test_soft_rate_penalty_increments_failures():
    """账号级软限流要累加 consecutive_failures，否则指数退避永远停在基数。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_1", realm="cn", access_token="at"))
            provider = _provider_with_catalog(store, ["m"], [])
            classified = classify.classify(429, "too many requests")
            provider._penalize(store.get_account("u_1"), classified, "m", "too many requests")
            first = store.get_account("u_1")
            assert first.consecutive_failures == 1
            assert first.cooldown_kind == "soft"
            provider._penalize(store.get_account("u_1"), classified, "m", "too many requests")
            assert store.get_account("u_1").consecutive_failures == 2
        finally:
            store.close()


def test_try_refresh_skips_fresh_token_unless_forced():
    """非 force 的 _try_refresh 在 token 仍有效时不打上游；force 才强制换票。

    这是按账号刷新锁「拿锁后重读、已刷新就跳过」的语义：并发下后到者不会
    再轮换一次 RT，而人工「刷新」按钮仍要能强制换一张新票。
    """
    import providers.workbuddy.upstream as wb_upstream

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(
                uid="u_1", realm="cn", access_token="at", refresh_token="rt",
                token_expires_ms=int(time.time() * 1000) + 3_600_000,
            ))
            provider = _provider_with_catalog(store, ["m"], [])
            provider._refresh_credits_async = lambda uid: None
            calls: list[str] = []

            def fake_refresh(account):
                calls.append(account.uid)
                return {"accessToken": "at2", "refreshToken": "rt2", "expiresIn": 3600}

            original = wb_upstream.refresh_request
            wb_upstream.refresh_request = fake_refresh
            try:
                assert provider._try_refresh(store.get_account("u_1")) is True
                assert calls == []  # 仍有效：不打上游
                assert provider._try_refresh(store.get_account("u_1"), force=True) is True
                assert calls == ["u_1"]  # force：真打一次
            finally:
                wb_upstream.refresh_request = original
        finally:
            store.close()


# ── 积分台账：到账竞态的重试阶梯 / 观测过期补拉 / 失败可见 ──
#
# 背景（2026-10-18 实测）：上游的欢迎积分包比账号入库晚 1~2 秒才创建，而入库那一刻
# 的查询只花 ~0.2s——必然拿到空包；空包按「未知」不落库，且余额只在换 token 时才拉，
# 于是新号积分列一直空到下一次保活。这一组用例钉住三条：阶梯重试能补上、巡检按
# 「上次尝试」过期补拉、失败原因要落库给面板看。


@contextmanager
def _stub_credits(*results):
    """脚本化 fetch_credits_detail：元素是 CreditsDetail 或 Exception 实例。

    序列用尽后重复最后一项（便于写"一直空/一直失败"的用例）。yield 出调用记录
    （每次查询的 uid 列表），断言"打了谁、打了几次"。
    """
    calls: list[str] = []

    def _fake(account, **_kwargs):
        calls.append(account.uid)
        item = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    original = wb_upstream.fetch_credits_detail
    wb_upstream.fetch_credits_detail = _fake
    try:
        yield calls
    finally:
        wb_upstream.fetch_credits_detail = original


def test_credits_empty_payload_keeps_known_value_and_records_attempt():
    """空包：不清已知余额（未知≠0），但这次尝试要记账（巡检与面板都靠它）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(
                uid="u_1", realm="cn", access_token="at",
                credits_remain=500.0, credits_total=800.0,
            ))
            provider = WorkBuddyProvider(store=store)
            with _stub_credits(wb_upstream.CreditsDetail()):  # 无资源包
                assert provider._refresh_credits("u_1") is None
            account = store.get_account("u_1")
            assert (account.credits_remain, account.credits_total) == (500.0, 800.0)
            assert account.credits_checked_ms > 0  # 尝试过
            assert account.credits_updated_ms == 0  # 从未有过可靠观测
            assert account.credits_error == ""  # 空包不是失败
        finally:
            store.close()


def test_credits_failure_recorded_then_cleared_by_success():
    """失败原因落库（面板要能讲清"为什么空"），下次成功即清空。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_1", realm="global", access_token="at"))
            provider = WorkBuddyProvider(store=store)
            with _stub_credits(ValueError("user-resource status 500: boom")):
                assert provider._refresh_credits("u_1") is None
            failed = store.get_account("u_1")
            assert "status 500" in failed.credits_error
            assert failed.credits_checked_ms > 0 and failed.credits_updated_ms == 0
            assert failed.credits_remain is None
            # 面板拿到的就是这两个字段（public_dict 直出，不另做裁剪）
            view = failed.public_dict()
            assert view["credits_error"] == failed.credits_error
            assert view["credits_checked_ms"] == failed.credits_checked_ms

            with _stub_credits(wb_upstream.CreditsDetail(remain=350.0, total=350.0)):
                detail = provider._refresh_credits("u_1")
            assert detail is not None and detail.remain == 350.0
            recovered = store.get_account("u_1")
            assert (recovered.credits_remain, recovered.credits_total) == (350.0, 350.0)
            assert recovered.credits_error == ""
            assert recovered.credits_updated_ms > 0
        finally:
            store.close()


def test_credits_settle_ladder_stops_at_first_observation():
    """阶梯：入库那次空包 → 重试到有观测即停（不再多打一次上游）。"""
    import providers.workbuddy.provider as provider_module

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_1", realm="global", access_token="at"))
            provider = WorkBuddyProvider(store=store)
            backoff = provider_module.WB_CREDITS_SETTLE_BACKOFF_S
            provider_module.WB_CREDITS_SETTLE_BACKOFF_S = 0.0  # 用例里不等真实秒数
            try:
                with _stub_credits(
                    wb_upstream.CreditsDetail(),  # 入库那次：欢迎包还没建好
                    wb_upstream.CreditsDetail(),  # 第一次重试：仍为空
                    wb_upstream.CreditsDetail(remain=350.0, total=350.0),  # 到账
                ) as calls:
                    detail = provider._refresh_credits_settle("u_1")
            finally:
                provider_module.WB_CREDITS_SETTLE_BACKOFF_S = backoff
            assert detail is not None and detail.remain == 350.0
            assert len(calls) == 3  # 第 3 次拿到观测即停，没有第 4 次
            assert store.get_account("u_1").credits_remain == 350.0
        finally:
            store.close()


def test_credits_settle_ladder_gives_up_at_attempt_cap():
    """阶梯是有限的：一直失败就停在次数上限，并把最后一次的失败原因留在库里。"""
    import providers.workbuddy.provider as provider_module

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_1", realm="cn", access_token="at"))
            provider = WorkBuddyProvider(store=store)
            backoff = provider_module.WB_CREDITS_SETTLE_BACKOFF_S
            provider_module.WB_CREDITS_SETTLE_BACKOFF_S = 0.0
            try:
                with _stub_credits(RuntimeError("upstream down")) as calls:
                    assert provider._refresh_credits_settle("u_1") is None
            finally:
                provider_module.WB_CREDITS_SETTLE_BACKOFF_S = backoff
            assert len(calls) == provider_module.WB_CREDITS_SETTLE_ATTEMPTS
            account = store.get_account("u_1")
            assert "upstream down" in account.credits_error
            assert account.credits_remain is None
        finally:
            store.close()


def test_credits_sweep_gates_by_attempt_age_and_status():
    """巡检选谁：只挑「上次尝试早于 now-TTL」的活跃/暂停号；没查过的也要补。"""
    from core.settings import WB_CREDITS_TTL_S

    now = int(time.time() * 1000)
    stale_ms = now - (WB_CREDITS_TTL_S + 60) * 1000
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            store.upsert_account(Account(uid="u_fresh", realm="cn", access_token="at",
                                         credits_checked_ms=now))
            store.upsert_account(Account(uid="u_old", realm="cn", access_token="at",
                                         credits_checked_ms=stale_ms))
            store.upsert_account(Account(uid="u_paused", realm="cn", access_token="at",
                                         status="paused", credits_checked_ms=stale_ms))
            store.upsert_account(Account(uid="u_never", realm="cn", access_token="at"))
            store.upsert_account(Account(uid="u_off", realm="cn", access_token="at",
                                         status="disabled"))
            provider = WorkBuddyProvider(store=store)
            with _stub_credits(wb_upstream.CreditsDetail(remain=10.0, total=10.0)) as calls:
                swept = provider.refresh_stale_credits()
            assert sorted(calls) == ["u_never", "u_old", "u_paused"]  # fresh 不补、disabled 不进
            assert swept == 3
            assert store.get_account("u_fresh").credits_checked_ms == now  # 没被动过
            assert store.get_account("u_never").credits_remain == 10.0
            assert store.get_account("u_off").credits_checked_ms == 0  # 禁用号不碰
        finally:
            store.close()


def test_credits_sweep_caps_per_round_and_records_failures():
    """每轮有上限（积压也不齐射）；失败次数 0 但尝试时刻要前进，免得下轮立刻重撞。"""
    import providers.workbuddy.provider as provider_module

    with tempfile.TemporaryDirectory() as tmp:
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            for uid in ("u_a", "u_b", "u_c"):
                store.upsert_account(Account(uid=uid, realm="cn", access_token="at"))
            provider = WorkBuddyProvider(store=store)
            cap = provider_module.WB_CREDITS_SWEEP_MAX
            provider_module.WB_CREDITS_SWEEP_MAX = 1
            try:
                with _stub_credits(ValueError("500")) as calls:
                    assert provider.refresh_stale_credits() == 0  # 没有新观测
            finally:
                provider_module.WB_CREDITS_SWEEP_MAX = cap
            assert len(calls) == 1  # 上限拦住，没把三个一起打
            swept_uid = calls[0]
            assert store.get_account(swept_uid).credits_checked_ms > 0
            others = [u for u in ("u_a", "u_b", "u_c") if u != swept_uid]
            for uid in others:
                assert store.get_account(uid).credits_checked_ms == 0  # 留到下一轮
        finally:
            store.close()


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
