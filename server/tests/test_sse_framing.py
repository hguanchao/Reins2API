"""SSE 半包处理：分块喂流的等价性与边界（gateway/stream/sse.py）。

为什么单独立文件：sse.py 自己写着"半包处理是本移植的最高风险点"——上游的字节
怎么切块完全是网络说了算的，同一份流按不同切法喂进来必须派发**完全相同**的事件
序列，否则线上表现就是"偶尔少一段推理 / 偶尔多一个工具块"这种极难复现的 bug。

这里用两条独立的解码器实现做差分：
  · SseLineDecoder —— 转码路径用的逐行解码器；
  · SseFrameSplitter —— 透传路径用的整帧观察器（内部也是逐行解码器）。
同一份字节流按随机切法喂给两者，事件序列必须逐字相同；再钉住几条边界：
CRLF 帧、多行 data 合并、UTF-8 多字节被切开、没有空行终结的尾记录、注释心跳行，
以及"上游迟迟不发换行"时的缓冲上限（SseBufferOverflow）。

运行：cd server && uv run python tests/test_sse_framing.py
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.stream.errors import SseBufferOverflow, SseUnexpectedEof
from gateway.stream.sse import (
    MAX_RECORD_BYTES,
    SseFrameSplitter,
    SseLineDecoder,
)

# 一份覆盖各种形态的流：普通帧 / CRLF 帧 / 多行 data / 注释心跳 / 无名事件 /
# 带 event 名的事件 / 结尾没有空行的残记录。
STREAM = (
    b'data: {"i":0}\n\n'
    + b"event: ping\ndata: {}\n\n"
    + b": keepalive\n\n"
    + b"data: line1\ndata: line2\r\n\r\n"
    + b'data: {"i":1}\n\n'
    + b'event: message_start\ndata: {"type":"message_start"}\n\n'
    + "data: 中文载荷\n\n".encode()
    + b"data: tail-without-blank-line\n"
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, name: str, data: str) -> None:
        self.events.append((name, data))


def _split(stream: bytes, cuts: list[int]) -> list[bytes]:
    bounds = [0, *cuts, len(stream)]
    return [stream[a:b] for a, b in itertools.pairwise(bounds) if a != b]


def test_split_points_do_not_change_events() -> None:
    """随机切块 × 两条解码路径：事件序列必须与"一次喂完"完全一致。"""
    reference = _Recorder()
    decoder = SseLineDecoder(reference)
    decoder.push(STREAM)
    truncated = False
    try:
        decoder.end()
    except SseUnexpectedEof:
        truncated = True
    assert reference.events, "夹具本身没产出事件，测试无意义"
    # 尾记录没有空行终结 → 不派发，且 end() 必须报截断
    assert truncated, "没有空行终结的尾记录必须报 SseUnexpectedEof"
    assert ("", "tail-without-blank-line") not in reference.events, reference.events
    assert reference.events[-1] == ("", "中文载荷"), reference.events

    rng = random.Random(20261017)
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, len(STREAM)), rng.randint(1, 12)))
        chunks = _split(STREAM, cuts)

        lines = _Recorder()
        frame_lines = _Recorder()
        line_decoder = SseLineDecoder(lines)
        frame_splitter = SseFrameSplitter(frame_lines)
        for chunk in chunks:
            line_decoder.push(chunk)
            frame_splitter.push(chunk)
        try:
            line_decoder.end()
        except SseUnexpectedEof:
            pass
        assert lines.events == reference.events, (cuts, lines.events)
        # 透传观察器同样不派发残帧（只观察完整帧）
        assert frame_lines.events == reference.events, (cuts, frame_lines.events)


def test_crlf_and_multiline_data() -> None:
    rec = _Recorder()
    decoder = SseLineDecoder(rec)
    decoder.push(b"event: chunk\r\ndata: a\r\ndata: b\r\n\r\n")
    assert rec.events == [("chunk", "a\nb")], rec.events


def test_utf8_split_across_chunks() -> None:
    """按 \\n 切分永远不会劈开 UTF-8 字符（多字节序列不含 0x0A）。"""
    payload = "data: 中文与 emoji 🐈\n\n".encode()
    for cut in range(1, len(payload)):
        rec = _Recorder()
        decoder = SseLineDecoder(rec)
        decoder.push(payload[:cut])
        decoder.push(payload[cut:])
        assert rec.events == [("", "中文与 emoji 🐈")], (cut, rec.events)


def test_comment_and_event_only_records_are_not_dispatched() -> None:
    """只有 event:/id:/注释 而没有 data 的记录不派发（SSE 语义）。"""
    rec = _Recorder()
    decoder = SseLineDecoder(rec)
    decoder.push(b": ping\n\nevent: nothing\n\nid: 7\n\n")
    assert rec.events == [], rec.events


def test_unterminated_record_reports_truncation() -> None:
    rec = _Recorder()
    decoder = SseLineDecoder(rec)
    decoder.push(b'data: {"ok":1}')
    try:
        decoder.end()
        raise AssertionError("没有空行终结的记录必须报 SseUnexpectedEof")
    except SseUnexpectedEof:
        pass
    assert rec.events == [], rec.events  # 残记录不派发


def test_giant_record_is_capped() -> None:
    """上游迟迟不发换行：缓冲到上限必须报错，而不是无限攒到 OOM。"""
    rec = _Recorder()
    decoder = SseLineDecoder(rec)
    chunk = b"x" * (4 * 1024 * 1024)
    try:
        for _ in range(MAX_RECORD_BYTES // len(chunk) + 2):
            decoder.push(chunk)
        raise AssertionError("超长记录必须报 SseBufferOverflow")
    except SseBufferOverflow as exc:
        assert "MB" in str(exc), exc
    assert rec.events == []

    splitter = SseFrameSplitter(_Recorder())
    try:
        for _ in range(MAX_RECORD_BYTES // len(chunk) + 2):
            splitter.push(chunk)
        raise AssertionError("超长帧必须报 SseBufferOverflow")
    except SseBufferOverflow:
        pass


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
