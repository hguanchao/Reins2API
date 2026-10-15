"""下游流发帧的错误路径回归测试。

为什么单独立这一条：错误路径平时不走，坏掉也不会有人发现——上游流中断时它本该给
客户端写一帧错误帧再收尾，而基类曾把方法名写成不存在的 `_emit_error`，于是整条路径
抛 AttributeError，客户端拿不到任何错误帧、流断在半句话上（看着像"模型自己结束了"）。
这个用例让三个协议各自跑一遍错误路径，钉住"发了错误帧 + 正常收尾抛出"。

运行：cd server && .venv/Scripts/python.exe tests/test_stream_emitter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.protocols import (
    AnthropicStreamEmitter,
    ChatStreamEmitter,
    ResponsesStreamEmitter,
)
from gateway.stream.errors import UpstreamStreamFailure
from gateway.types import BridgeStreamEvent

EMITTERS = (ChatStreamEmitter, ResponsesStreamEmitter, AnthropicStreamEmitter)


class _Recorder:
    """最小 StreamSink：把写出的字节按顺序攒起来供断言。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk.decode("utf-8", "replace"))

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def test_error_event_emits_frame_then_finishes() -> None:
    """三个协议的错误路径：都要写出错误帧，并以 UpstreamStreamFailure 正常收尾。"""
    for emitter_type in EMITTERS:
        sink = _Recorder()
        emitter = emitter_type(sink, "test-model")
        try:
            emitter.emit(
                BridgeStreamEvent(kind="error", error="upstream SSE stream failed: boom",
                                  error_type="upstream_error")
            )
        except UpstreamStreamFailure:
            pass  # emitter 写完错误帧后的正常收尾信号
        else:
            raise AssertionError(f"{emitter_type.__name__}: 应以 UpstreamStreamFailure 收尾")
        text = sink.text
        assert "boom" in text, f"{emitter_type.__name__}: 错误帧里缺错误文案: {text[:200]!r}"
        assert emitter.done is True, f"{emitter_type.__name__}: done 未置位"


def test_error_event_without_message_uses_default() -> None:
    """上游只断了流、没给文案时用兜底文案，不能因为 None 再炸一次。"""
    sink = _Recorder()
    emitter = ChatStreamEmitter(sink, "test-model")
    try:
        emitter.emit(BridgeStreamEvent(kind="error"))
    except UpstreamStreamFailure:
        pass
    else:
        raise AssertionError("应以 UpstreamStreamFailure 收尾")
    assert "upstream stream failed" in sink.text


def test_transcode_failure_helper_writes_error_frame() -> None:
    """真实的触发点：transcode 把上游流中断翻译成错误事件时，这帧要真的发得出去。

    这个入口返回 None 表示"错误事件已成功写给客户端"——它曾经返回 AttributeError，
    被 inference 记成"下游流以错误结束"。
    """
    from gateway.stream.errors import SseUnexpectedEof
    from gateway.stream.failure import emit_unexpected_stream_error

    for emitter_type in EMITTERS:
        sink = _Recorder()
        emitter = emitter_type(sink, "test-model")
        outcome = emit_unexpected_stream_error(emitter, SseUnexpectedEof("stream ended"))
        assert outcome is None, f"{emitter_type.__name__}: 错误帧没写出去 -> {outcome!r}"
        assert "terminal event" in sink.text, f"{emitter_type.__name__}: {sink.text[:200]!r}"


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
