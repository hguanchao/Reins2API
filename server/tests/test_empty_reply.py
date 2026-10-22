"""空回复判定测试：零内容 + 零 token 计量 + 非截断停止 → 拒绝放行。

对照 magpie 的三条件设计（emptyFails / repliesCounted / uncounted / answersNothing）：
"正常结束却什么都没说"的流交出去会让严格客户端静默结束回合且不重试（真实事故：
某代理给 Codex 的回合只带 stop、不带任何 token 与消息），所以三条件全占时按
上游故障处理：折叠路抛 EmptyReply → 502，转码路补客户端协议错误帧。

反向用例锁定"不算空"的边界：有文本/有思考/有工具/有 token 计量/length 停止
（被截断说明有东西没放出来，如实透传）/content_filter 停止。

运行：cd server && uv run python tests/test_empty_reply.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.stream import collapse_stream, forward_stream, transcode_stream
from gateway.stream.errors import EmptyReply

MODEL = "deepseek-v4"


class _Sink:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8")


def _chunks(payload: str) -> list[bytes]:
    return [payload.encode("utf-8")]


def _chat_stream(finish_reason: str, with_usage: bool = False) -> str:
    tail = (
        f'data: {{"choices":[{{"index":0,"delta":{{}},"finish_reason":"{finish_reason}"}}]}}\n\n'
    )
    if with_usage:
        tail += 'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    return (
        'data: {"id":"chatcmpl_x","model":"' + MODEL + '","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{}}]}\n\n'
        + tail
        + "data: [DONE]\n\n"
    )


def _anthropic_stream(stop_reason: str | None, with_usage: bool = True) -> str:
    usage = '"usage":{"input_tokens":0,"output_tokens":0}' if with_usage else ""
    parts = [
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_x","model":"' + MODEL + '"' + (
            f',"usage":{{"input_tokens":0,"output_tokens":0}}' if with_usage else ""
        ) + '}}\n\n',
    ]
    if stop_reason is not None:
        parts.append(
            'event: message_delta\n'
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}"}}{"," + usage if usage else ""}}}\n\n'
        )
    parts.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return "".join(parts)


# ── 判定该拒绝的形态 ──


def test_collapse_rejects_chat_empty_stop():
    try:
        collapse_stream(_chunks(_chat_stream("stop")), "chat", MODEL)
    except EmptyReply as error:
        assert "empty reply" in str(error)
    else:
        raise AssertionError("chat 空回复（stop + 零计量）应被拒绝")


def test_collapse_rejects_anthropic_empty_end_turn():
    try:
        collapse_stream(_chunks(_anthropic_stream("end_turn")), "anthropic", MODEL)
    except EmptyReply:
        pass
    else:
        raise AssertionError("anthropic 空回复（end_turn + 零计量）应被拒绝")


def test_transcode_rejects_empty_cross_protocol():
    sink = _Sink()
    outcome = transcode_stream(sink, _chunks(_chat_stream("stop")), "chat", "anthropic", MODEL)
    # 错误帧以客户端协议（anthropic）写出，而不是让回合静默结束。
    assert isinstance(outcome.error, EmptyReply)
    assert "event: error" in sink.text
    assert "empty reply" in sink.text


def test_forward_flags_empty_same_protocol():
    sink = _Sink()
    outcome = forward_stream(sink, _chunks(_chat_stream("stop")), "chat", MODEL)
    # 透传字节无法撤销：错误只能留在 outcome 里给推理层记日志。
    assert isinstance(outcome.error, EmptyReply)


# ── 判定该放行的形态 ──


def test_collapse_passes_chat_with_usage():
    # 有 token 计量：计了费说明上游真的处理过内容，不算空。
    response = collapse_stream(_chunks(_chat_stream("stop", with_usage=True)), "chat", MODEL)
    assert response["choices"][0]["finish_reason"] == "stop"


def test_collapse_passes_anthropic_with_text():
    stream = _anthropic_stream("end_turn").replace(
        'event: message_delta',
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta',
    )
    response = collapse_stream(_chunks(stream), "anthropic", MODEL)
    assert response["content"][0]["text"] == "hi"


def test_collapse_passes_length_stop_empty():
    # length 停止的空回复：被截断说明有东西没放出来，如实透传，不额外报错。
    response = collapse_stream(_chunks(_chat_stream("length")), "chat", MODEL)
    assert response["choices"][0]["finish_reason"] == "length"


def test_collapse_passes_content_filter_stop_empty():
    response = collapse_stream(_chunks(_chat_stream("content_filter")), "chat", MODEL)
    assert response["choices"][0]["finish_reason"] == "content_filter"


def test_transcode_tool_stream_passes():
    # 工具流（黄金夹具）不是空回复：既有内容也有计量。
    from tests.test_protocol_bridge import CHAT_STREAM

    sink = _Sink()
    outcome = transcode_stream(sink, _chunks(CHAT_STREAM), "chat", "responses", MODEL)
    assert outcome.error is None


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
