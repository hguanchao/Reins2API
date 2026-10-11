"""协议桥/流层的黄金特征测试（重构安全网）。

把 3×3 方向的请求转换、响应转换、流式转码、折叠的结果快照成 JSON，重构前后
逐字节比对。生成/更新黄金文件：

    cd server && uv run python tests/test_protocol_bridge.py --regen

普通校验（无 pytest 环境直接跑）：

    cd server && uv run python tests/test_protocol_bridge.py
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = Path(__file__).resolve().parent / "golden" / "protocol_bridge.json"

PROTOCOLS = ("chat", "responses", "anthropic")
MODEL = "deepseek-v4"
UPSTREAM_URL = "https://copilot.tencent.com"

# ── 固定夹具（每个协议一份请求/响应/上游流）──

CHAT_REQUEST = {
    "model": MODEL, "stream": True, "temperature": 0.5, "top_p": 0.9,
    "max_tokens": 128, "stop": ["x"], "reasoning_effort": "high",
    "messages": [
        {"role": "system", "content": "sys"},
        {"role": "developer", "content": "dev"},
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        ]},
        {"role": "assistant", "content": "ans", "reasoning_content": "r1",
         "reasoning_signature": "sig",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "user", "content": "again"},
    ],
    "tools": [{"type": "function", "function": {
        "name": "f", "description": "d",
        "parameters": {"type": "object", "properties": {}}}}],
    "tool_choice": {"type": "function", "function": {"name": "f"}},
    "response_format": {"type": "json_schema", "json_schema": {
        "name": "s", "schema": {"type": "object"}, "strict": True}},
    "parallel_tool_calls": True, "frequency_penalty": 0.1, "presence_penalty": 0.2,
    "seed": 7, "prompt_cache_key": "pk", "safety_identifier": "si",
    "service_tier": "auto", "store": True,
}

RESPONSES_REQUEST = {
    "model": MODEL, "stream": True, "temperature": 0.5, "top_p": 0.9,
    "max_output_tokens": 128, "stop": ["x"], "reasoning": {"effort": "high"},
    "instructions": "sys",
    "input": [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "image_url": "http://x/y.png"},
        ]},
        {"type": "reasoning", "id": "rs1",
         "summary": [{"type": "summary_text", "text": "r1"}], "encrypted_content": "enc"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "result"},
    ],
    "tools": [{"type": "function", "name": "f", "description": "d",
               "parameters": {"type": "object", "properties": {}}, "strict": True}],
    "tool_choice": {"type": "function", "name": "f"},
    "text": {"format": {"type": "json_schema", "name": "s", "schema": {"type": "object"}}},
    "parallel_tool_calls": True, "prompt_cache_key": "pk", "safety_identifier": "si",
    "service_tier": "auto", "store": True,
}

ANTHROPIC_REQUEST = {
    "model": MODEL, "stream": True, "temperature": 0.5, "top_p": 0.9,
    "max_tokens": 128, "stop_sequences": ["x"],
    "thinking": {"type": "enabled", "budget_tokens": 8192},
    "system": [{"type": "text", "text": "sys"}],
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"type": "url", "url": "http://x/y.png"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "r1", "signature": "sig"},
            {"type": "text", "text": "ans"},
            {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "result", "is_error": False},
        ]},
    ],
    "tools": [{"name": "f", "description": "d",
               "input_schema": {"type": "object", "properties": {}}}],
    "tool_choice": {"type": "tool", "name": "f"},
}

CHAT_RESPONSE = {
    "id": "chatcmpl_abc", "object": "chat.completion", "created": 1, "model": MODEL,
    "choices": [{"index": 0, "message": {
        "role": "assistant", "content": "hello", "reasoning_content": "r1",
        "reasoning_signature": "sig",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "f", "arguments": '{"a":1}'}}]},
        "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
              "prompt_tokens_details": {"cached_tokens": 2},
              "completion_tokens_details": {"reasoning_tokens": 3}},
}

RESPONSES_RESPONSE = {
    "id": "resp_abc", "object": "response", "created_at": 1, "status": "completed",
    "model": MODEL,
    "output": [
        {"type": "reasoning", "id": "rs1",
         "summary": [{"type": "summary_text", "text": "r1"}], "encrypted_content": "enc"},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "hello"}]},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
              "input_tokens_details": {"cached_tokens": 2},
              "output_tokens_details": {"reasoning_tokens": 3}},
}

ANTHROPIC_RESPONSE = {
    "id": "msg_abc", "type": "message", "role": "assistant", "model": MODEL,
    "content": [
        {"type": "thinking", "thinking": "r1", "signature": "sig"},
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 8, "output_tokens": 5,
              "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
}

CHAT_STREAM = (
    'data: {"id":"chatcmpl_abc","model":"' + MODEL + '","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"reasoning_content":"r1"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"content":"hel"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{\\"a\\""}}]}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":1}"}}]}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    'data: [DONE]\n\n'
)

RESPONSES_STREAM = (
    'event: response.created\n'
    'data: {"type":"response.created","response":{"id":"resp_abc","model":"' + MODEL + '"}}\n\n'
    'event: response.reasoning_summary_text.delta\n'
    'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs1","output_index":0,"delta":"r1"}\n\n'
    'event: response.output_text.delta\n'
    'data: {"type":"response.output_text.delta","item_id":"msg1","output_index":1,"delta":"hello"}\n\n'
    'event: response.output_item.added\n'
    'data: {"type":"response.output_item.added","output_index":2,"item":{"id":"fc1","type":"function_call","call_id":"c1","name":"f","arguments":""}}\n\n'
    'event: response.function_call_arguments.delta\n'
    'data: {"type":"response.function_call_arguments.delta","item_id":"fc1","output_index":2,"delta":"{\\"a\\":1}"}\n\n'
    'event: response.completed\n'
    'data: {"type":"response.completed","response":{"id":"resp_abc","status":"completed","output":[{"type":"function_call","call_id":"c1","name":"f"}],"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}\n\n'
)

ANTHROPIC_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_abc","model":"' + MODEL + '","usage":{"input_tokens":8,"output_tokens":0}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"r1"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"hello"}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"c1","name":"f","input":{}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":1}"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

REQUESTS = {"chat": CHAT_REQUEST, "responses": RESPONSES_REQUEST, "anthropic": ANTHROPIC_REQUEST}
RESPONSES = {"chat": CHAT_RESPONSE, "responses": RESPONSES_RESPONSE, "anthropic": ANTHROPIC_RESPONSE}
STREAMS = {"chat": CHAT_STREAM, "responses": RESPONSES_STREAM, "anthropic": ANTHROPIC_STREAM}


class _Sink:
    """收集下游写出的字节（transcode/forward 用）。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8")


def _chunks(payload: str, split_at: int | None = None) -> list[bytes]:
    raw = payload.encode("utf-8")
    if split_at is None or split_at <= 0 or split_at >= len(raw):
        return [raw]
    return [raw[:split_at], raw[split_at:]]


def build_snapshot() -> dict:
    """跑全部用例，返回可 JSON 序列化的快照。"""
    from gateway.bridge.convert import convert_response, prepare_request
    from gateway.stream import collapse_stream, forward_stream, transcode_stream

    snapshot: dict = {"prepare_request": {}, "convert_response": {}, "transcode": {}, "collapse": {}, "forward": {}}

    for src, dst in itertools.product(PROTOCOLS, PROTOCOLS):
        key = f"{src}->{dst}"
        body = json.loads(json.dumps(REQUESTS[src]))
        snapshot["prepare_request"][key] = prepare_request(src, dst, body, UPSTREAM_URL)

        resp = json.loads(json.dumps(RESPONSES[src]))
        snapshot["convert_response"][key] = convert_response(src, dst, resp)

        sink = _Sink()
        outcome = transcode_stream(sink, _chunks(STREAMS[src]), src, dst, MODEL)
        snapshot["transcode"][key] = {
            "sse": sink.text,
            "usage": _usage_dict(outcome.usage),
            "error": None if outcome.error is None else type(outcome.error).__name__,
        }

        fsink = _Sink()
        foutcome = forward_stream(fsink, _chunks(STREAMS[src]), src, MODEL)
        snapshot["forward"][key] = {
            "sse": fsink.text,
            "usage": _usage_dict(foutcome.usage),
            "error": None if foutcome.error is None else type(foutcome.error).__name__,
        }

    for proto in PROTOCOLS:
        snapshot["collapse"][proto] = collapse_stream(_chunks(STREAMS[proto]), proto, MODEL)

    # 半包边界：同一 chat 流按 37 字节切开，结果必须与整块一致。
    sink = _Sink()
    transcode_stream(sink, _chunks(CHAT_STREAM, 37), "chat", "anthropic", MODEL)
    snapshot["transcode"]["chat->anthropic#split37"] = {"sse": sink.text}

    return snapshot


def _usage_dict(usage) -> dict:
    return {
        "input": usage.input, "output": usage.output, "total": usage.total,
        "cached": usage.cached, "cache_creation": usage.cache_creation,
        "reasoning": usage.reasoning,
    }


def _freeze() -> None:
    """冻结随机 id 与时间，让快照可复现。"""
    import secrets
    import time

    counter = itertools.count(1)
    secrets.token_hex = lambda n: format(next(counter), f"0{n * 2}x")[-n * 2:]
    time.time = lambda: 1700000000.0


def _snapshot_text() -> str:
    _freeze()
    return json.dumps(build_snapshot(), ensure_ascii=False, indent=2, sort_keys=True)


def test_snapshot_matches_golden():
    assert GOLDEN.exists(), f"缺少黄金文件 {GOLDEN}，先跑 --regen"
    actual = _snapshot_text()
    expected = GOLDEN.read_text(encoding="utf-8")
    if actual != expected:
        # 定位第一处差异，便于排障。
        a_lines, e_lines = actual.splitlines(), expected.splitlines()
        for i in range(max(len(a_lines), len(e_lines))):
            a = a_lines[i] if i < len(a_lines) else "<missing>"
            e = e_lines[i] if i < len(e_lines) else "<missing>"
            if a != e:
                raise AssertionError(f"快照第 {i + 1} 行不一致:\n  实际: {a}\n  黄金: {e}")
        raise AssertionError("快照不一致（仅长度差异）")


def _regen() -> int:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(_snapshot_text(), encoding="utf-8")
    print(f"已写入黄金文件 {GOLDEN}")
    return 0


if __name__ == "__main__":
    if "--regen" in sys.argv:
        raise SystemExit(_regen())
    try:
        test_snapshot_matches_golden()
        print("PASS test_snapshot_matches_golden")
    except AssertionError as exc:
        print(f"FAIL test_snapshot_matches_golden: {exc}")
        raise SystemExit(1)
    raise SystemExit(0)
