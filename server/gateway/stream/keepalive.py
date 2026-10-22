# 下游 keepalive：流式转发期间下游久无字节时，发一条 SSE 注释帧证明连接还活着。
#
# 动机（对照 magpie 的 relayEvents/keepaliveGap）：思考型模型一个 token 都没出、
# 上游又在正常推流的间隙里静默，客户端（或中间的反代/企业防火墙）的读空闲计时
# 到点就会掐连接——模型的整段思考全部作废。keepalive 帧是 SSE 注释（": keepalive"），
# 三个协议的合规读端都跳过注释行，对客户端零副作用。
#
# 时钟口径：
#   * 下游静默 = 距 sink.last_write_at（最后一次向下游写出）的时长；
#   * 上游活着 = stats.up_last_at 有值且距它不超过 UPSTREAM_SILENCE_LIMIT_S。
#     上游自己也静默（真正断线/挂起）时**不**发——客户端的空闲超时该由它自己
#     判上游死活，网关替上游撒谎反而推迟故障发现。
#   * 上游从未吐过字节（up_last_at == 0）归首包前重发层管，这里不掺和。
#
# 写入竞态：keepalive 线程与转发线程同时 write sink。_HandlerSink 的 write 是
# 单个 wfile.write + flush，Python 的 socket 写在 GIL 下按调用原子完成，两条
# 线程交错写的最坏结果是"注释帧与数据帧交错的字节流"——SSE 解析按行扫描，
# 注释行混进任何位置都被忽略，语义不破。加锁反而引入"转发持锁阻塞在上游读"
# 的反直觉耦合，不值得。

from __future__ import annotations

import threading
import time
from typing import Any

# 下游静默多久开始发 keepalive：高于正常客户端的心跳预期、低于常见反代的 60s
# 读空闲默认值。
KEEPALIVE_AFTER_S = 30.0
# 检查周期（也是两帧之间的最小间隔）。
KEEPALIVE_EVERY_S = 15.0
# 上游自身静默超过这个时长就不再发（上游可能已死，别替它假装活着）。
UPSTREAM_SILENCE_LIMIT_S = 120.0

_KEEPALIVE_FRAME = b": keepalive\n\n"


class KeepalivePump:
    """后台线程：下游静默且上游仍活跃时，周期性写 SSE 注释帧。

    生命周期跟随一次流式转发：start() 在转发开始时调用，stop() 在转发结束
    （正常/异常/断连）的 finally 里调用。三个阈值参数化（生产用模块常量），
    测试用短时值驱动真实时钟。
    """

    def __init__(
        self,
        sink: Any,
        stats: Any,
        *,
        after_s: float = KEEPALIVE_AFTER_S,
        every_s: float = KEEPALIVE_EVERY_S,
        upstream_limit_s: float = UPSTREAM_SILENCE_LIMIT_S,
    ) -> None:
        self._sink = sink
        self._stats = stats
        self._after_s = after_s
        self._upstream_limit_s = upstream_limit_s
        self._stop = threading.Event()
        self._every_s = every_s
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="sse-keepalive", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._every_s):
            if self._stats.up_last_at <= 0.0:
                continue  # 上游还没吐过字节：首包前重发层的辖地。
            if time.monotonic() - self._stats.up_last_at > self._upstream_limit_s:
                return  # 上游自身静默过久：停手，让客户端的超时去判死活。
            if time.monotonic() - self._sink.last_write_at < self._after_s:
                continue  # 下游不静默：无事可做。
            try:
                self._sink.write(_KEEPALIVE_FRAME)
            except Exception:
                # 下游已断：静默退出，转发循环马上也会拿到同一个写错误。
                return
