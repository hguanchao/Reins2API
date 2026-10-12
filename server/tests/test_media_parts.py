"""多模态输入与能力适配的单元测试（2026-10-12 那轮：视频/PDF/服务端工具/结构化输出/能力声明）。

覆盖四块：
1. 桥接层新增的 video / audio 块类型（chat ↔ anthropic 双向）；
2. 非 function 工具（web_search 等）从「整单 400」改为忽略；
3. 出站附件归一 providers/parts.py —— 按**传入的上游档案**决定放行/改写/降级；
4. 能力块 providers/capabilities.py —— 合并优先级与「不知道就不输出」。

运行：cd server && uv run python -m pytest tests/test_media_parts.py -q
（无 pytest 时：uv run python tests/test_media_parts.py 直接执行内建 main）
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.bridge.content import (
    decode_anthropic_blocks_checked,
    decode_openai_blocks_checked,
    encode_anthropic_blocks,
    encode_chat_blocks,
)
from gateway.bridge.format import chat_response_format
from gateway.protocols import anthropic as anthropic_protocol
from gateway.protocols import chat as chat_protocol
from gateway.protocols import responses as responses_protocol
from providers import parts, profiles
from providers.capabilities import UpstreamProfile, as_flag, capability_block
from providers.workbuddy import shape

# 两种上游档案（测试里显式给，不依赖具体 provider）：
#   STRICT = 不收 file（workbuddy 实测形态）；FULL = chat 全量 part 透传
STRICT = frozenset({"text", "image_url", "video_url", "input_audio"})
FULL = frozenset({"text", "image_url", "video_url", "input_audio", "file"})


# ── 1. 视频 / 音频块 ──


def test_chat_video_and_audio_decode_encode():
    """chat 侧 video_url / input_audio ↔ 内部 video / audio 块。"""
    decoded = decode_openai_blocks_checked([
        {"type": "text", "text": "看这个"},
        {"type": "video_url", "video_url": {"url": "https://x/a.mp4"}},
        {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "wav"}},
    ])
    assert [b.kind for b in decoded] == ["text", "video", "audio"]
    assert decoded[1].url == "https://x/a.mp4"
    assert decoded[2].data == "QUJD"
    assert decoded[2].media_type == "audio/wav"  # format → media_type

    encoded = encode_chat_blocks(decoded)
    assert encoded[1] == {"type": "video_url", "video_url": {"url": "https://x/a.mp4"}}
    assert encoded[2] == {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "wav"}}


def test_anthropic_video_block_roundtrip():
    """ZCode 在 anthropic 格式下发的私有 video 块（base64/url 两式）原样往返。"""
    decoded = decode_anthropic_blocks_checked([
        {"type": "video", "source": {"type": "url", "url": "https://x/b.mp4"}},
        {"type": "video", "source": {"type": "base64", "media_type": "video/mp4", "data": "QUJD"}},
    ])
    assert [b.kind for b in decoded] == ["video", "video"]
    assert decoded[0].url == "https://x/b.mp4"
    assert decoded[1].media_type == "video/mp4"

    encoded = encode_anthropic_blocks(decoded)
    assert encoded[0] == {"type": "video", "source": {"type": "url", "url": "https://x/b.mp4"}}
    assert encoded[1]["source"]["data"] == "QUJD"


def test_anthropic_document_with_video_media_type():
    """有的客户端把视频塞进 document：按 media_type 归到 video 块。"""
    decoded = decode_anthropic_blocks_checked([
        {"type": "document", "title": "v.mp4",
         "source": {"type": "base64", "media_type": "video/mp4", "data": "QUJD"}},
    ])
    assert decoded[0].kind == "video"
    assert decoded[0].media_type == "video/mp4"
    assert decoded[0].filename == "v.mp4"


def test_anthropic_audio_degrades_to_note():
    """Anthropic 协议装不下音频：降级成可见文字占位，不让整条请求失败。"""
    decoded = decode_openai_blocks_checked([
        {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}},
    ])
    encoded = encode_anthropic_blocks(decoded)
    assert encoded[0]["type"] == "text"
    assert "音频附件" in encoded[0]["text"]
    assert "已省略" in encoded[0]["text"]


# ── 2. 非 function 工具：忽略而不是 400 ──


def test_chat_skips_server_tools():
    request = chat_protocol.decode_request({
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "web_search"},
            {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}},
        ],
    })
    assert [t.name for t in request.tools] == ["f"]


def test_responses_skips_server_tools():
    request = responses_protocol.decode_request({
        "model": "m", "input": "hi",
        "tools": [
            {"type": "web_search_preview"},
            {"type": "function", "name": "f", "parameters": {"type": "object"}},
        ],
    })
    assert [t.name for t in request.tools] == ["f"]


def test_anthropic_skips_server_tools_by_input_schema():
    """Anthropic 自定义工具必带 input_schema；不带的（web_search_20250305…）是服务端工具。"""
    request = anthropic_protocol.decode_request({
        "model": "m", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
            {"name": "f", "description": "d", "input_schema": {"type": "object"}},
        ],
    })
    assert [t.name for t in request.tools] == ["f"]


# ── 3. 结构化输出 ──


def test_anthropic_output_config_format_becomes_chat_response_format():
    """Anthropic 的 output_config.format（摊平）→ chat 的 response_format（嵌套），
    name 缺失时补默认名——chat 上游要求 json_schema.name 必填。"""
    request = anthropic_protocol.decode_request({
        "model": "m", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
    })
    assert request.response_format == {
        "type": "json_schema", "schema": {"type": "object"},
    }
    assert chat_response_format(request.response_format) == {
        "type": "json_schema",
        "json_schema": {"schema": {"type": "object"}, "name": "structured_output"},
    }


def test_chat_response_format_keeps_client_name():
    assert chat_response_format({
        "type": "json_schema",
        "json_schema": {"name": "mine", "strict": True, "schema": {"type": "object"}},
    }) == {
        "type": "json_schema",
        "json_schema": {"name": "mine", "strict": True, "schema": {"type": "object"}},
    }


# ── 4. 出站附件归一（按档案，不认识厂商）──


def test_parts_normalizes_file_image_to_image_url():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "什么颜色"},
        {"type": "file", "file": {"filename": "g.png", "file_data": "data:image/png;base64,QUJD"}},
    ]}]}
    assert parts.normalize_chat_message_parts(body, accepted_parts=STRICT) is True
    assert body["messages"][0]["content"][1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"},
    }


def test_parts_inlines_text_file():
    payload = base64.b64encode("口令 PINEAPPLE".encode()).decode()
    body = {"messages": [{"role": "user", "content": [
        {"type": "file", "file": {"filename": "n.txt", "file_data": f"data:text/plain;base64,{payload}"}},
    ]}]}
    assert parts.normalize_chat_message_parts(body, accepted_parts=STRICT) is True
    assert body["messages"][0]["content"] == [{"type": "text", "text": "口令 PINEAPPLE"}]


def test_parts_drops_pdf_with_note_when_file_not_accepted():
    """档案不收 file：PDF 丢掉并留一句省略说明。"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "总结这份文档"},
        {"type": "file", "file": {"filename": "t.pdf", "file_data": "data:application/pdf;base64,QUJD"}},
    ]}]}
    assert parts.normalize_chat_message_parts(body, accepted_parts=STRICT) is True
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "总结这份文档"}
    assert "application/pdf" in content[1]["text"]


def test_parts_passes_file_through_when_accepted():
    """档案收 file（未验证的上游 / 原生支持 document 的协议）：原样透传，零改动。"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "file", "file": {"filename": "t.pdf", "file_data": "data:application/pdf;base64,QUJD"}},
    ]}]}
    assert parts.normalize_chat_message_parts(body, accepted_parts=FULL) is False
    assert body["messages"][0]["content"][0]["type"] == "file"


def test_parts_drops_any_unaccepted_part_type():
    """不止 file：档案不收 video_url 时同样降级为省略说明（机制是通用的）。"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "https://x/a.mp4"}},
    ]}]}
    assert parts.normalize_chat_message_parts(
        body, accepted_parts=frozenset({"text", "image_url"})
    ) is True
    assert body["messages"][0]["content"][0]["type"] == "text"
    assert "video_url" in body["messages"][0]["content"][0]["text"]


def test_parts_leaves_supported_parts_alone():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        {"type": "video_url", "video_url": {"url": "https://x/a.mp4"}},
    ]}]}
    assert parts.normalize_chat_message_parts(body, accepted_parts=STRICT) is False
    assert len(body["messages"][0]["content"]) == 3


def test_workbuddy_shape_applies_its_profile():
    """接进 workbuddy 出站管线：用的是 WORKBUDDY 档案（不收 file → PDF 被省略）。"""
    body = {
        "model": "step-5-preview",
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_data": "data:application/pdf;base64,QUJD"}},
        ]}],
    }
    assert shape.shape_workbuddy_body(body) is True
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "已省略" in content[0]["text"]


# ── 5. 能力块：合并优先级与「不知道就不输出」──


def test_capability_block_priority_and_omission():
    """declared（目录声明）> profile.defaults > 省略；None = 不知道，不输出。"""
    profile = UpstreamProfile(
        name="t",
        accepted_parts=frozenset({"text", "image_url"}),
        defaults={"output_modalities": ["text"], "supports_web_search": False},
    )
    block = capability_block(profile, "chat", {
        "supports_web_search": True,          # 目录声明压过档案默认
        "supports_reasoning": None,           # 不知道 → 不输出
        "context_window": 128000,
    })
    assert block["supports_web_search"] is True
    assert "supports_reasoning" not in block
    assert block["output_modalities"] == ["text"]
    assert block["context_window"] == 128000


def test_capability_block_filters_undeliverable_modalities():
    """传输层装不下的模态，厂商声明过也去掉；三个派生位跟着模态走。"""
    profile = UpstreamProfile(name="t", accepted_parts=frozenset({"text", "image_url"}))
    block = capability_block(profile, "chat", {
        "input_modalities": ["text", "image", "video", "pdf", "audio"],
    })
    assert block["input_modalities"] == ["text", "image"]
    assert block["supports_vision"] is True
    assert block["supports_video_input"] is False
    assert block["supports_pdf_input"] is False


def test_capability_block_pdf_survives_non_chat_protocol():
    """anthropic 上游的 document 是 PDF 的原生载体，不经过 chat 的 part 白名单。"""
    profile = UpstreamProfile(name="t", accepted_parts=frozenset({"text", "image_url"}))
    block = capability_block(profile, "anthropic", {"input_modalities": ["text", "pdf"]})
    assert block["input_modalities"] == ["text", "pdf"]
    assert block["supports_pdf_input"] is True


def test_capability_block_without_any_modality_source():
    """档案与目录都没说模态 → 不输出模态，也不派生三个 supports_* 位。"""
    profile = UpstreamProfile(name="t", accepted_parts=frozenset({"text"}))
    block = capability_block(profile, "chat", {})
    assert block == {}


def test_as_flag_keeps_unknown_as_none():
    assert as_flag(True) is True
    assert as_flag(False) is False
    assert as_flag(None) is None
    assert as_flag("true") is None  # 非布尔不猜


def test_profiles_declare_transport_parts():
    """档案要点：名称非空、传输层至少认文本与图片；其余类型的取舍与依据见
    tests/upstream_evidence.md（记录放测试目录，生产代码不放探针明细）。"""
    for profile in (profiles.WORKBUDDY, profiles.OPENCODE):
        assert profile.name != ""
        assert {"text", "image_url"} <= set(profile.accepted_parts)


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
