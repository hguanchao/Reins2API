"""出站会话复用：每线程一个 curl_cffi Session，连接跨请求保活。

Why：顶层 `crequests.post(...)` 每调一次铸一个全新 Session——TCP+TLS+HTTP/2
每笔请求全套重来，实测 opencode.ai 新建 0.81s vs 复用 0.31s，三个 provider 的
每一笔出站（含流式）都在白付这笔握手。网关的 handler 线程随 keep-alive 连接
存活（api/server.py `protocol_version = "HTTP/1.1"`），把 Session 挂在
threading.local 上，同一线程终生复用同一条连接；流式泵（inference._pump）
也在同一个 handler 线程内跑完，**不存在跨线程共享 easy 句柄**——curl_cffi 的
句柄是 thread-local 的（0.16 默认），实测多线程共享一个 Session 反而拿不到
连接复用（0.7~1.4s）。

Cookie 保真：顶层 API 每次请求都是空 Cookie 环境起手；这里的会话是长命的，
若不清 Cookie，同一线程上前后两个不同账号/客户端的请求会互相串上游 Cookie
（会话混淆、缓存定向脏写）。所以每次调用完立刻清 jar——外部语义与从前逐请求
一致，复用的只有连接本身。

后台线程（出口探测、token 巡检、签到任务）各有自己的线程，Session 随线程
生灭，短 RPC 拿不到复用也不付额外代价。

调用方式与 curl_cffi 顶层 API 同形（post/get/request 直传全部参数），
模块别名惯例沿用 `crequests`（见各 provider upstream 的 import 注释）；
同 `crequests.RequestsError` / `crequests.Response` 原样再导出，异常类型
仍是同一个类，任何模块的 except 都能照常捕获。
"""

from __future__ import annotations

import threading

from curl_cffi import requests as _crequests

# 类型/异常再导出：调用方继续写 crequests.RequestsError / crequests.Response。
# 必须是同一个类对象——except 的 isinstance 匹配不认别名。
RequestsError = _crequests.RequestsError
Response = _crequests.Response

_local = threading.local()


def session() -> _crequests.Session:
    """当前线程的出站 Session，首个调用时懒创建，随线程存活。"""
    s = getattr(_local, "session", None)
    if s is None:
        s = _crequests.Session()
        _local.session = s
    return s


def _call(method: str, url: str, **kwargs):
    # 方法名归一：调用方有传大写 "POST"/"GET" 的（workbuddy/login._do_json），
    # curl_cffi 顶层 API 自己会归一，这里换成会话直调后得自己兜住。
    method = method.lower()
    s = session()
    try:
        return getattr(s, method)(url, **kwargs)
    finally:
        # 逐请求清 Cookie（见模块注）：保连接、不保状态。清 jar 不影响已经
        # 开始的流式响应——那只是本会话 Cookie 存储，不是连接上的字节流。
        s.cookies.clear()


def get(url: str, **kwargs) -> Response:
    return _call("get", url, **kwargs)


def post(url: str, **kwargs) -> Response:
    return _call("post", url, **kwargs)


def request(method: str, url: str, **kwargs) -> Response:
    return _call(method, url, **kwargs)
