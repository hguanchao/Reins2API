"""上游流"首包前"重发：只有在下游一个字节都没收到时才允许重发。

判据是"字节有没有流向下游"，不是错误码、也不是时间：
  * 第一次尝试里上游一个块都没产出（传输类异常掐断，或 2xx 之后空流 EOF）→ 下游
    什么都没看到，重发零副作用；
  * 给下游之后就绝不重发——chat SSE 没有断点续传，重发只能让模型从头推理，
    客户端看到的就是"思考断掉又重新开始"。这类中断只能如实报错（由错误上报层翻成下游错误帧）。

resend 由推理层提供（它持有 body/ids/route 与 provider），本模块只管尝试节奏与
统计：掐流日志要能说明"停在哪、静默多久、重发过几次"。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as crequests

from gateway.jsonutil import close_quietly


@dataclass
class StreamStats:
    """一次下游流的上游侧实况（attempts 含首次；up_* 只累计真正收到并转发的块）。"""

    attempts: int = 0
    retries: int = 0
    up_chunks: int = 0
    up_bytes: int = 0
    up_last_at: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def silence_ms(self) -> int:
        """距上游最后一个块的毫秒数；一个块都没收到时从开始算。"""
        base = self.up_last_at or self.started_at
        return max(0, int((time.monotonic() - base) * 1000))


def retrying_chunks(
    first: Any,
    resend: Callable[[], Any],
    *,
    attempts: int,
    backoff_s: float,
    stats: StreamStats,
    on_retry: Callable[[int, Exception | None], None] | None = None,
) -> Iterator[bytes]:
    """把上游块流包成"首包前可重发"的字节迭代器。

    first 是已经拿到的响应（头已到、状态已判）；resend() 每次都返回一份新响应。
    只吞两类失败并重发：传输类异常（curl_cffi RequestsError 家族），以及 2xx 之后
    一个块都没有就 EOF 的空流。其余异常（含 resend 抛出的上游判决）原样上抛，由
    流式层翻成下游错误帧。

    attempts 是**上游次数上限**（含首次），所以 attempts=2 表示最多重发一次。
    """
    response = first
    total = max(1, attempts)
    try:
        for attempt in range(1, total + 1):
            stats.attempts = attempt
            produced = False
            failure: Exception | None = None
            try:
                for chunk in response.iter_chunks():
                    if not chunk:
                        continue
                    produced = True
                    stats.up_chunks += 1
                    stats.up_bytes += len(chunk)
                    stats.up_last_at = time.monotonic()
                    yield chunk
            except Exception as exc:  # 去留由下面的判据决定
                failure = exc
            if produced:
                # 已经吐过字节：正常收尾，或者把中断原样交给流式层去报错误帧。
                if failure is not None:
                    raise failure
                return
            # 到这里 = 这一次尝试一个字节都没产出。
            if failure is not None and not _is_transport_failure(failure):
                raise failure
            if attempt >= total:
                if failure is not None:
                    raise failure
                return  # 2xx 空流且预算用尽：交给流式层报"没等到终止事件"
            close_quietly(response)
            stats.retries += 1
            if on_retry is not None:
                on_retry(attempt, failure)
            time.sleep(max(0.0, backoff_s))
            response = resend()
    finally:
        # 消费者提前收工（客户端断连、已收到终止事件）与异常路径都在这里释放上游连接。
        close_quietly(response)


def _is_transport_failure(exc: BaseException) -> bool:
    """curl_cffi 的传输类异常（握手被掐 / 连接重置 / 超时…）都在这棵树下。"""
    return isinstance(exc, crequests.RequestsError)


