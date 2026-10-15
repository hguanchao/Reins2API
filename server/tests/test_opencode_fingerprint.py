"""opencode 免费档客户端指纹回归测试（对照 opencode2api 2026-10-14 修复）。

上游 2026-10-14 起对每个档位做客户端指纹校验，实测确认过四类 403 FreeTierError：
旧端点、旧头形态、旧工具名、合成工具定义。这些用例锁住四件事，任何一处回退都会
在这里变红，而不是等线上请求全被拒。

运行：cd server && .venv/Scripts/python.exe tests/test_opencode_fingerprint.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.base import RequestIDs
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES
from providers import identity
from providers.opencode import OpenCodeProvider, tools, upstream

# 官方 CLI 铸的 session（descending 编码，计数器 1，低 12 位 ffe）
_OFFICIAL_SESSION = (
    "ses_" + format(~(1_760_000_000_000 * 0x1000 + 1) & 0xFFFFFFFFFFFF, "012x") + "A" * 14
)
# 形态对但编码不是官方的（原始哈希 / 升序 ID 一类）
_FOREIGN_SESSION = "ses_000000000000" + "A" * 14


def _ids() -> RequestIDs:
    return RequestIDs(
        session=identity.canonical_session_id("t"),
        request="msg_deadbeef",
        project=identity.opencode_project_id("global"),
    )


# ── identity：session 编码 ──


def test_canonical_session_matches_official_encoding():
    session = identity.canonical_session_id("some-conversation")
    assert identity.SESSION_PATTERN.match(session), session
    assert identity._DESCENDING_TIME_PART.match(session[4:16]), session
    assert identity.is_official_session(session)


def test_canonical_session_is_deterministic():
    # 确定性而非随机+缓存：进程重启后同一会话仍拿到同一个 session，prompt cache 亲和不断
    assert identity.canonical_session_id("abc") == identity.canonical_session_id("abc")
    assert identity.canonical_session_id("abc") != identity.canonical_session_id("abd")


def test_foreign_canonical_session_is_not_passed_through():
    # 形态对、编码不是官方的 session 必须重编码；直接透传会被上游 403
    assert not identity.is_official_session(_FOREIGN_SESSION)
    assert identity.is_official_session(identity.canonical_session_id(_FOREIGN_SESSION))


def test_official_session_passes_through():
    assert identity.is_official_session(_OFFICIAL_SESSION)


def test_conversation_signal_ignores_volatile_message_metadata():
    # 首条 user 轮里的 cwd/时间戳每轮都变（Windows 上每个请求的默认 cwd 都不同），
    # 不能把它们的差异算成"新会话"，否则 prompt cache 亲和每轮都断
    first = {"messages": [{"role": "user", "content": "hello there"}]}
    second = {
        "messages": [
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "hello there"},
        ],
        "metadata": {"cwd": "C:\\Users\\other", "timestamp": 123},
    }
    assert (
        identity.derive_request_ids({}, first).session
        == identity.derive_request_ids({}, second).session
    )
    # 内容块形式（真实客户端发的 parts 数组）取文本部分当种子
    blocks = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hello there"}]}]}
    assert (
        identity.derive_request_ids({}, blocks).session
        == identity.derive_request_ids({}, first).session
    )
    # 不同开头仍要分开
    other = {"messages": [{"role": "user", "content": "different opener"}]}
    assert (
        identity.derive_request_ids({}, other).session
        != identity.derive_request_ids({}, first).session
    )


def test_body_conversation_id_is_the_signal():
    # 显式 conversation_id 优先于消息内容：同一 conversation_id 下的不同发言是同一会话
    ids = {"conversation_id": "conv-1"}
    first = identity.derive_request_ids({}, {**ids, "input": "first turn"})
    second = identity.derive_request_ids({}, {**ids, "input": "second turn"})
    assert first.session == second.session
    other = identity.derive_request_ids({}, {"conversation_id": "conv-2", "input": "first turn"})
    assert other.session != first.session


def test_derive_uses_hard_rule_headers_and_reencodes_sessions():
    derived = identity.derive_request_ids(
        {"x-opencode-session-id": _FOREIGN_SESSION},
        {"messages": [{"role": "user", "content": "hi"}]},
    )
    # 客户端给的 session 编码不对 -> 不采用，按会话信号重编码
    assert derived.session != _FOREIGN_SESSION
    assert identity.is_official_session(derived.session)
    # 同一对话信号跨请求稳定
    again = identity.derive_request_ids({}, {"messages": [{"role": "user", "content": "hi"}]})
    assert again.session == derived.session


def test_project_id_is_bare_40_hex():
    # `prj_` 前缀现在是明确 403；线上形态是裸 40 位小写 hex
    project = identity.opencode_project_id("global")
    assert len(project) == 40 and project == project.lower()
    assert all(ch in "0123456789abcdef" for ch in project)
    assert not project.startswith("prj_")


# ── upstream：请求头 ──


def test_headers_mirror_client_and_drop_rejected_fields():
    headers = upstream.build_upstream_headers(PROTOCOL_CHAT, "public", _ids())
    # 实测必需项
    assert headers["user-agent"] == "opencode/latest/2.0.29/cli"
    assert headers["x-opencode-session-id"] == headers["x-opencode-session"]
    assert len(headers["x-opencode-project"]) == 40
    # 实测被拒项：网关自己的关联头不能出网
    assert "x-opencode-request" not in headers
    # 整个头集合里不得再出现旧形态
    joined = " ".join(f"{k}:{v}" for k, v in headers.items())
    assert "1.18.31" not in joined
    assert "prj_" not in joined


def test_headers_keep_legacy_gate_requirements():
    headers = upstream.build_upstream_headers(PROTOCOL_CHAT, "public", _ids())
    # 429 遗留要求：x-opencode-session 与 -id 首值必须一致且都是 descending 编码
    assert identity.is_official_session(headers["x-opencode-session-id"])
    assert headers["authorization"] == "Bearer public"
    # anthropic 走 x-api-key，且必须带 interleaved thinking beta
    anthropic = upstream.build_upstream_headers(PROTOCOL_ANTHROPIC, "public", _ids())
    assert anthropic["x-api-key"] == "public" and "authorization" not in anthropic
    assert "interleaved-thinking" in anthropic["anthropic-beta"]


def test_parent_session_header_only_when_present():
    base = _ids()
    assert "x-parent-session-id" not in upstream.build_upstream_headers(
        PROTOCOL_CHAT, "public", base
    )
    parent = RequestIDs(
        session=base.session,
        request=base.request,
        project=base.project,
        parent_session="ses_parent",
    )
    assert (
        upstream.build_upstream_headers(PROTOCOL_CHAT, "public", parent)["x-parent-session-id"]
        == "ses_parent"
    )


# ── upstream：端点 ──


def test_inference_traffic_uses_inference_host():
    captured: dict[str, object] = {}
    original = upstream.crequests.post

    class _Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}

    def stub(url, **kwargs):
        captured["url"] = url
        return _Response()

    upstream.crequests.post = stub
    try:
        upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
        assert captured["url"] == "https://opencode.ai/inference/v1/chat/completions"
        upstream.send_upstream(PROTOCOL_ANTHROPIC, b"{}", _ids(), stream=True)
        assert captured["url"] == "https://opencode.ai/inference/v1/messages"
    finally:
        upstream.crequests.post = original
    # 发现与模型清单仍留在 /zen（两端点清单一致）
    assert upstream.ZEN_BASE == "https://opencode.ai/zen"


# ── tools：真定义 ──


def test_genuine_definitions_are_loaded():
    known = tools.known_tools()
    assert len(known) >= 12
    for name in tools.CORE_TOOLS:
        assert name in known, f"核心工具缺官方定义: {name}"
        assert known[name].get("description"), name
        assert isinstance(known[name].get("parameters"), dict), name
    # 2.x 前的旧名不再注入：上游认识 bash 且会校验它的定义，而网关手上没有
    assert "bash" not in tools.CORE_TOOLS


def test_render_tool_matches_protocol_shapes():
    chat = tools.render_tool(PROTOCOL_CHAT, "shell")
    assert chat["type"] == "function" and chat["function"]["name"] == "shell"
    assert chat["function"]["parameters"] == tools.known_tools()["shell"]["parameters"]
    anthropic = tools.render_tool(PROTOCOL_ANTHROPIC, "shell")
    assert anthropic["input_schema"] == tools.known_tools()["shell"]["parameters"]
    responses = tools.render_tool(PROTOCOL_RESPONSES, "shell")
    assert responses["type"] == "function" and "function" not in responses
    assert responses["parameters"] == tools.known_tools()["shell"]["parameters"]


# ── upstream：请求体塑形 ──


def test_shape_injects_genuine_core_tools():
    shaped, changed = upstream.shape_free_request_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]}, PROTOCOL_CHAT
    )
    assert changed
    assert shaped["stream"] is True
    assert shaped["stream_options"] == {"include_usage": True}
    names = [tool["function"]["name"] for tool in shaped["tools"]]
    assert names == list(tools.CORE_TOOLS)
    # 注入的必须是官方定义，不是桩
    for tool in shaped["tools"]:
        assert (
            tool["function"]["parameters"]
            == tools.known_tools()[tool["function"]["name"]]["parameters"]
        )


def test_shape_replaces_recognised_tools_and_keeps_unknown_ones():
    payload = {
        "model": "big-pickle",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "stub",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "stub",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "my_custom_tool",
                    "description": "keep me",
                    "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
                },
            },
        ],
    }
    shaped, changed = upstream.shape_free_request_body(payload, PROTOCOL_CHAT)
    assert changed
    by_name = {tool["function"]["name"]: tool for tool in shaped["tools"]}
    # 认识的名字换成官方定义
    assert (
        by_name["shell"]["function"]["description"] == tools.known_tools()["shell"]["description"]
    )
    assert by_name["shell"]["function"]["parameters"] == tools.known_tools()["shell"]["parameters"]
    # 不认识的名字原样保留，下游自定义工具照常可用
    assert by_name["my_custom_tool"]["function"] == payload["tools"][2]["function"]
    # 客户端声明的旧名 bash 保留原样，但缺失的核心工具仍补齐
    assert by_name["bash"]["function"]["description"] == "stub"
    for name in tools.CORE_TOOLS:
        assert name in by_name
    assert shaped["tools"][0]["function"]["description"] != "stub"


def test_shape_is_idempotent_on_genuine_payload():
    payload = {
        "model": "big-pickle",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools.render_toolset(PROTOCOL_CHAT, tools.CORE_TOOLS),
    }
    before = json.dumps(payload, sort_keys=True)
    shaped, changed = upstream.shape_free_request_body(payload, PROTOCOL_CHAT)
    assert not changed
    assert json.dumps(shaped, sort_keys=True) == before


def test_shape_anthropic_uses_input_schema():
    shaped, _ = upstream.shape_free_request_body(
        {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}, PROTOCOL_ANTHROPIC
    )
    for tool in shaped["tools"]:
        assert "input_schema" in tool and "function" not in tool
        assert tool["input_schema"] == tools.known_tools()[tool["name"]]["parameters"]


def test_provider_class_wires_the_fixed_modules():
    """不构造实例、不碰网络：直接调 provider 类的包装方法，确认它接的是修好的实现。

    包装方法（derive_ids/shape 都是转发）对象同一性不成立，所以按行为验：修复落在
    模块里但没接进 provider 的话，上面的模块级用例会全绿而线上照旧 403。
    """
    assert OpenCodeProvider.upstream_url == "https://opencode.ai/inference"

    body = {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]}
    shallow = OpenCodeProvider.__new__(OpenCodeProvider)  # 免去目录加载：这两个方法不碰状态
    ids = OpenCodeProvider.derive_ids(shallow, {}, body)
    assert identity.is_official_session(ids.session)
    assert len(ids.project) == 40 and not ids.project.startswith("prj_")

    shaped, changed = OpenCodeProvider.shape(shallow, dict(body), PROTOCOL_CHAT)
    assert changed and shaped["stream"] is True
    names = [tool["function"]["name"] for tool in shaped["tools"]]
    assert names == list(tools.CORE_TOOLS)
    # 上游拒 x-opencode-request：包装层发出去的头里也不能有
    headers = upstream.build_upstream_headers(PROTOCOL_CHAT, "public", ids)
    assert "x-opencode-request" not in headers
    assert headers["user-agent"] == "opencode/latest/2.0.29/cli"


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
