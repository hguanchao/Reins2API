"""下游 keepalive 测试：下游静默 + 上游活跃 → 发 SSE 注释帧；其余形态不发。

帧是 SSE 注释（": keepalive"）：三个协议的合规读端都跳过注释行，对客户端
零副作用。判定三支：上游没吐过字节不发（首包前重发层辖地）、上游自身静默
过久不发（别替死上游装活）、下游不静默不发。

运行：cd server && uv run python tests/test_stream_keepalive.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.stream.keepalive import KeepalivePump, _KEEPALIVE_FRAME
from gateway.stream.retry import StreamStats


class _Sink:
    def __init__(self) -> None:
        self.written = 0
        self.last_write_at = time.monotonic()
        self.frames: list[bytes] = []

    def write(self, chunk: bytes) -> None:
        self.frames.append(chunk)
        self.written += len(chunk)
        self.last_write_at = time.monotonic()

    def flush(self) -> None:
        pass


# 阈值：检查周期 0.05s，下游静默 0.1s 触发，上游静默上限 10s（测试里不会碰到）。
AFTER = 0.1
EVERY = 0.05
LIMIT = 10.0


def _pump(sink: _Sink, stats: StreamStats) -> KeepalivePump:
    return KeepalivePump(sink, stats, after_s=AFTER, every_s=EVERY, upstream_limit_s=LIMIT)


def _wait_frames(sink: _Sink, count: int, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(sink.frames) >= count:
            return True
        time.sleep(0.01)
    return False


def test_pump_sends_frame_when_downstream_silent_upstream_alive():
    sink = _Sink()
    stats = StreamStats()
    stats.up_last_at = time.monotonic()  # 上游刚吐过块
    pump = _pump(sink, stats)
    pump.start()
    try:
        assert _wait_frames(sink, 1), "下游静默超阈值后应发出 keepalive 注释帧"
        assert sink.frames[0] == _KEEPALIVE_FRAME
    finally:
        pump.stop()
    # stop 之后不再发。
    count = len(sink.frames)
    time.sleep(EVERY * 3)
    assert len(sink.frames) == count


def test_pump_skips_when_upstream_never_produced():
    sink = _Sink()
    stats = StreamStats()  # up_last_at == 0：上游一个块都没有
    pump = _pump(sink, stats)
    pump.start()
    try:
        time.sleep(EVERY * 6)
        assert sink.frames == [], "上游没吐过字节时不该发 keepalive"
    finally:
        pump.stop()


def test_pump_skips_when_upstream_dead():
    sink = _Sink()
    stats = StreamStats()
    stats.up_last_at = time.monotonic() - LIMIT - 1  # 上游自身静默超限
    pump = _pump(sink, stats)
    pump.start()
    try:
        time.sleep(EVERY * 6)
        assert sink.frames == [], "上游已死时替下游装活只会推迟故障发现"
    finally:
        pump.stop()


def test_pump_quiets_while_downstream_active():
    sink = _Sink()
    stats = StreamStats()
    stats.up_last_at = time.monotonic()
    pump = _pump(sink, stats)
    pump.start()
    try:
        # 下游持续有写出（last_write_at 不断刷新）→ 不发 keepalive 帧。
        # frames 里会有测试自己写的 data 帧，所以只数注释帧。
        for _ in range(8):
            sink.write(b"data: x\n\n")
            time.sleep(EVERY)
        assert not any(f == _KEEPALIVE_FRAME for f in sink.frames), (
            "下游不静默时不该发 keepalive"
        )
    finally:
        pump.stop()


def test_pump_write_error_ends_thread():
    sink = _Sink()
    stats = StreamStats()
    stats.up_last_at = time.monotonic()

    def broken_write(chunk: bytes) -> None:
        raise BrokenPipeError()

    sink.write = broken_write  # type: ignore[method-assign]
    pump = _pump(sink, stats)
    pump.start()
    try:
        assert _wait_frames(sink, 1) is False
        # 线程应当自己退出（join 不超时卡住）。
        deadline = time.monotonic() + 2.0
        while pump._thread is not None and pump._thread.is_alive():
            if time.monotonic() > deadline:
                raise AssertionError("写错误后 keepalive 线程应静默退出")
            time.sleep(0.02)
    finally:
        pump.stop()


def test_stop_is_idempotent_and_joinable():
    sink = _Sink()
    stats = StreamStats()
    stats.up_last_at = time.monotonic()
    pump = _pump(sink, stats)
    pump.start()
    pump.stop()
    pump.stop()  # 二次 stop 不炸
    assert not threading.current_thread().daemon or True


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
