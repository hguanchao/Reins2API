"""结构化输出兜底测试：无原生 format 字段的协议改说系统提示 + 回答剥围栏。

对照 magpie 的 Format.instruction / inSystem / unfence：跨协议转换会静默丢掉
结构化输出声明（Anthropic 编码器不写 output_config.format），丢掉等于客户端
要的 JSON 无声失效——兜底是把要求说进系统提示，回答套了 ```json 围栏时再剥掉。

运行：cd server && uv run python tests/test_format_fallback.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.bridge.convert import convert_response, prepare_request
from gateway.bridge.format import format_instruction, unfence_json

CHAT_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "s", "schema": {"type": "object", "properties": {"a": {"type": "number"}}}},
}


# ── format_instruction ──


def test_instruction_with_schema():
    text = format_instruction({"type": "json_schema", "schema": {"type": "object"}})
    assert text is not None and "JSON schema" in text and '"type": "object"' in text


def test_instruction_json_object():
    text = format_instruction({"type": "json_object"})
    assert text == "Respond with a single JSON object and nothing else."


def test_instruction_ignores_text_and_garbage():
    assert format_instruction({"type": "text"}) is None
    assert format_instruction("no") is None
    assert format_instruction(None) is None


# ── unfence_json ──


def test_unfence_strips_json_fence():
    assert unfence_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert unfence_json('```\n{"a": 1}\n```') == '{"a": 1}'
    assert unfence_json('```json\n[1, 2]\n```') == "[1, 2]"


def test_unfence_keeps_non_json_and_partial():
    # 围栏内不是合法 JSON：原样保留。
    assert unfence_json("```json\n{bad\n```") == "```json\n{bad\n```"
    # 围栏外还有别的话：原样保留。
    assert unfence_json('Sure!\n```json\n{"a":1}\n```') == 'Sure!\n```json\n{"a":1}\n```'
    # 非 json 标签：原样保留。
    assert unfence_json("```python\nprint(1)\n```") == "```python\nprint(1)\n```"
    # 普通文本永不动。
    assert unfence_json("plain text") == "plain text"


# ── 请求侧：跨协议到 Anthropic 时说进系统提示 ──


def test_chat_to_anthropic_format_told_in_system():
    # OpenAI Chat 的 system 是消息角色（顶层没有 system 字段）。
    body = {
        "model": "deepseek-v4",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
        "response_format": CHAT_FORMAT,
    }
    output = prepare_request("chat", "anthropic", body, "https://upstream")
    text = json.dumps(output["system"], ensure_ascii=False)
    assert "JSON schema" in text and "properties" in text and "be brief" in text
    # schema 原文进了提示（带上属性定义），且 output_config.format 没有被写出来。
    assert output.get("output_config") is None


def test_responses_to_anthropic_format_told_in_system():
    body = {
        "model": "deepseek-v4",
        "instructions": "be brief",
        "input": "hi",
        "text": {"format": {"type": "json_object"}},
    }
    output = prepare_request("responses", "anthropic", body, "https://upstream")
    text = system if isinstance(system := output["system"], str) else json.dumps(system, ensure_ascii=False)
    assert "single JSON object" in text and "be brief" in text


def test_chat_to_anthropic_without_format_untouched():
    body = {
        "model": "deepseek-v4",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
    }
    output = prepare_request("chat", "anthropic", body, "https://upstream")
    assert "be brief" in json.dumps(output["system"], ensure_ascii=False)
    assert "Respond with" not in json.dumps(output["system"], ensure_ascii=False)


def test_chat_to_responses_format_stays_native():
    # 目标协议有原生字段：不碰系统提示，format 原样落地。
    body = {
        "model": "deepseek-v4",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": CHAT_FORMAT,
    }
    output = prepare_request("chat", "responses", body, "https://upstream")
    assert output["text"]["format"]["type"] == "json_schema"
    assert "Respond with" not in json.dumps(output, ensure_ascii=False)


def test_same_protocol_passthrough_never_touched():
    body = {
        "model": "deepseek-v4",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": CHAT_FORMAT,
    }
    output = prepare_request("chat", "chat", body, "https://upstream")
    assert output["response_format"] == CHAT_FORMAT


# ── 响应侧：围栏剥除 ──


ANTHROPIC_FENCED_RESPONSE = {
    "id": "msg_x",
    "type": "message",
    "role": "assistant",
    "model": "deepseek-v4",
    "content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def test_response_to_chat_unfences_when_format_asked():
    # wants_json 由调用方（推理层）从客户端请求算好传入。
    converted = convert_response("anthropic", "chat", ANTHROPIC_FENCED_RESPONSE,
                                 wants_json=True)
    assert converted["choices"][0]["message"]["content"] == '{"a": 1}'


def test_response_to_chat_keeps_text_without_format():
    # 客户端没要 JSON：围栏是模型的本意，不动。
    plain = dict(ANTHROPIC_FENCED_RESPONSE)
    converted = convert_response("anthropic", "chat", plain)
    assert converted["choices"][0]["message"]["content"] == '```json\n{"a": 1}\n```'


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
