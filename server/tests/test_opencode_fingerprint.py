"""opencode 免费档客户端指纹回归测试（对照 opencode2api 2026-10-14 修复 +
官方客户端 2.0.29 源码逐项对齐）。

上游 2026-10-14 起对每个档位做客户端指纹校验，实测确认过四类 403 FreeTierError：
旧端点、旧头形态、旧工具名、合成工具定义。这些用例把"像官方客户端"的每一处都
钉在这里：头集合、session/父会话编码、chat 的 max tokens 字段名、responses 的
路由默认值、responses 的会话缓存键。任何一处回退都会在这里变红，而不是等线上
请求全被拒。

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
        client="cli_fp001",
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
    assert headers["user-agent"] == "opencode/latest/2.0.32/cli"
    # 无父会话时亲和头与子会话同值（derive 的信号同源）；带父会话时亲和钉父
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
    # anthropic 走 x-api-key，且 beta 集合与官方客户端逐字一致：
    # 官方恒请求 interleaved thinking，其余 beta 按请求内容条件追加（网关不构造
    # 那两类请求）；多带的 beta 官方从不发，是形态偏差。
    anthropic = upstream.build_upstream_headers(PROTOCOL_ANTHROPIC, "public", _ids())
    assert anthropic["x-api-key"] == "public" and "authorization" not in anthropic
    assert anthropic["anthropic-beta"] == "interleaved-thinking-2025-05-14"


def test_parent_session_headers_mirror_official_pair():
    base = _ids()
    bare = upstream.build_upstream_headers(PROTOCOL_CHAT, "public", base)
    assert "x-parent-session-id" not in bare
    assert "x-opencode-parent-session-id" not in bare
    parent = RequestIDs(
        session=base.session,
        request=base.request,
        project=base.project,
        parent_session=_OFFICIAL_SESSION,
    )
    # 官方客户端带父会话时两个头名都发（model-request.ts:283-290）
    headers = upstream.build_upstream_headers(PROTOCOL_CHAT, "public", parent)
    assert headers["x-parent-session-id"] == _OFFICIAL_SESSION
    assert headers["x-opencode-parent-session-id"] == _OFFICIAL_SESSION
    # 会话亲和绑规范化父会话（对照 opencode2api 2026-10-20 对真实 CLI 的抓包）：
    # 只有 x-opencode-session-id 留子会话，三个亲和/会话头都钉父会话。
    assert headers["x-opencode-session"] == _OFFICIAL_SESSION
    assert headers["x-session-affinity"] == _OFFICIAL_SESSION
    assert headers["X-Session-Id"] == _OFFICIAL_SESSION
    assert headers["x-opencode-session-id"] == base.session


def test_parent_session_is_reencoded_to_official_form():
    # 父会话与 session 同一条上游编码校验：私有形态重编码（确定性哈希）
    foreign = identity.derive_request_ids({"x-parent-session-id": "parent-uuid-1"}, {})
    assert identity.is_official_session(foreign.parent_session)
    assert foreign.parent_session == identity.canonical_session_id("parent-uuid-1")
    # 同一父信号跨请求稳定
    again = identity.derive_request_ids(
        {"x-opencode-parent-session-id": "parent-uuid-1"},
        {"messages": [{"role": "user", "content": "next turn"}]},
    )
    assert again.parent_session == foreign.parent_session
    # 官方形态的父会话原样透传（保住官方客户端自身的会话身份）
    official = identity.derive_request_ids(
        {"x-opencode-parent-session-id": _OFFICIAL_SESSION}, {}
    )
    assert official.parent_session == _OFFICIAL_SESSION


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
        # anthropic 请求带官方 CLI 恒发的 ?beta=true 查询参数
        assert captured["url"] == "https://opencode.ai/inference/v1/messages?beta=true"
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


def test_shape_renames_chat_max_tokens_to_official_field():
    """chat 的 max tokens 字段名对齐官方客户端：只有 max_completion_tokens。"""
    shaped, changed = upstream.shape_free_request_body(
        {"model": "big-pickle", "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
        PROTOCOL_CHAT,
    )
    assert changed
    assert "max_tokens" not in shaped
    assert shaped["max_completion_tokens"] == 4096
    # 已经是官方字段名的请求不动（幂等）
    again, changed_again = upstream.shape_free_request_body(shaped, PROTOCOL_CHAT)
    assert not changed_again and again["max_completion_tokens"] == 4096
    # 两个名字都给时以官方字段为准，不产生重复字段
    both, _ = upstream.shape_free_request_body(
        {"max_tokens": 1, "max_completion_tokens": 2, "messages": []}, PROTOCOL_CHAT
    )
    assert both["max_completion_tokens"] == 2 and "max_tokens" not in both
    # anthropic 的 max_tokens 本来就是官方字段，不改名
    anthropic, _ = upstream.shape_free_request_body(
        {"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
        PROTOCOL_ANTHROPIC,
    )
    assert anthropic["max_tokens"] == 4096 and "max_completion_tokens" not in anthropic


def test_shape_adds_official_responses_defaults():
    """responses 的路由默认值对齐官方：store=false + include encrypted reasoning。"""
    payload = {
        "model": "muse-spark-1.2-contributor-free",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    shaped, changed = upstream.shape_free_request_body(dict(payload), PROTOCOL_RESPONSES)
    assert changed
    assert shaped["store"] is False
    assert shaped["include"] == ["reasoning.encrypted_content"]
    # 幂等：补齐过的请求再塑形不改动
    _again, changed_again = upstream.shape_free_request_body(shaped, PROTOCOL_RESPONSES)
    assert not changed_again
    # 客户端显式给值时不动（官方同样以显式配置覆盖路由默认）
    explicit, _ = upstream.shape_free_request_body(
        {"store": True, "include": ["other"]}, PROTOCOL_RESPONSES
    )
    assert explicit["store"] is True and explicit["include"] == ["other"]
    # 有状态续接（previous_response_id）不塞 store=false——那是客户端的合法用法，
    # 强塞会把续接打断；include 照补（它不改变存储语义）
    stateful, _ = upstream.shape_free_request_body(
        {"previous_response_id": "resp_1", "input": []}, PROTOCOL_RESPONSES
    )
    assert "store" not in stateful
    assert stateful["include"] == ["reasoning.encrypted_content"]
    # chat 协议不补这两个字段（官方 chat 路线对 Zen 不发 store / include）
    chat, _ = upstream.shape_free_request_body(
        {"messages": [{"role": "user", "content": "hi"}]}, PROTOCOL_CHAT
    )
    assert "store" not in chat and "include" not in chat


def test_responses_send_injects_client_cache_key():
    """responses 出站补客户端级 prompt_cache_key（跨会话共享前缀缓存）；
    chat 不补；客户端自带键不覆盖。"""
    captured: dict[str, bytes] = {}
    original = upstream.crequests.post

    class _Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}

    def stub(url, **kwargs):
        captured["data"] = kwargs["data"]
        return _Response()

    upstream.crequests.post = stub
    try:
        ids = _ids()
        body = json.dumps({"model": "muse", "input": []}).encode()
        upstream.send_upstream(PROTOCOL_RESPONSES, body, ids, stream=True)
        assert json.loads(captured["data"])["prompt_cache_key"] == ids.client
        # chat 协议不注入（官方 opencode chat 路线没有 supportsPromptCacheKey）
        upstream.send_upstream(PROTOCOL_CHAT, body, ids, stream=True)
        assert "prompt_cache_key" not in json.loads(captured["data"])
        # 客户端自带的缓存键保持原样
        own = json.dumps({"prompt_cache_key": "mine"}).encode()
        upstream.send_upstream(PROTOCOL_RESPONSES, own, ids, stream=True)
        assert json.loads(captured["data"])["prompt_cache_key"] == "mine"
    finally:
        upstream.crequests.post = original


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
    assert headers["user-agent"] == "opencode/latest/2.0.32/cli"


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
