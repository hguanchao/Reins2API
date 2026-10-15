"""opencode 出站传输回归测试（对照 2026-10-15 线上事故）。

两件事在这里锁死，任何一处回退都会变红，而不是等线上又批量 504/截断：

1. **超时必须显式传**。curl_cffi 的 stream 模式把 tuple 翻译成
   CONNECTTIMEOUT=connect + LOW_SPEED(1 B/s)=connect+idle；不传 timeout 时它用自带
   默认 30/30，等于给免费档的长首字与推理期静默设了条 30s 死线（实测免费档首字
   2.4s~33.5s，正好横跨这条线 → 504 与"流被中途掐断"）。
2. **连接期失败重试一次**。出口线路会间歇性在握手期掐断新连接（curl 35，实测 8 次
   里 3 次），单凭证不重试就是客户端可见的 502。

全部离线：本地 TCP 桩服务 + 真实的 curl_cffi 行为，不打上游、不耗额度。

运行：cd server && .venv/Scripts/python.exe tests/test_opencode_transport.py
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curl_cffi import requests as crequests

from core import settings
from gateway.base import RequestIDs
from gateway.types import PROTOCOL_CHAT
from providers.opencode import upstream

_OK_BODY = b'{"id":"x","choices":[]}'
_FORBIDDEN_BODY = b'{"error":{"message":"Forbidden"}}'


# ── 本地 TCP 桩：按连接序号决定怎么处理 ──────────────────────────


class _StubServer:
    """极简 TCP 服务：handler(sock, index) 自行决定关连接 / 回包 / 静默。

    index 从 1 开始计（连接序号），配合"第一次掐断、第二次正常"这类脚本。
    """

    def __init__(self, handler: Callable[[socket.socket, int], None]) -> None:
        self._handler = handler
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(
                target=self._handler, args=(conn, self.connections), daemon=True
            ).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _drain_request(conn: socket.socket) -> None:
    """读到请求头结束（桩服务只关心"请求确实发出来了"）。"""
    conn.settimeout(2.0)
    try:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
    except OSError:
        return


def _respond(conn: socket.socket, status: str, body: bytes) -> None:
    _drain_request(conn)
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(head + body)
    except OSError:
        pass
    finally:
        conn.close()


def _reset(conn: socket.socket, _index: int) -> None:
    """立刻关连接（TCP 层 RST/关闭，不给任何响应）。"""
    conn.close()


def _hold_silent(conn: socket.socket, _index: int, seconds: float) -> None:
    """收下请求后静默——模拟上游"已连接但不吐第一个字节"。"""
    _drain_request(conn)
    time.sleep(seconds)
    conn.close()


def _header_then_reset(conn: socket.socket, _index: int) -> None:
    """先回响应头，再以 RST 掐断（不给任何 body）——上游"已开始处理才断线"。"""
    _drain_request(conn)
    try:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        time.sleep(0.05)
        import struct

        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    conn.close()


# ── 测试夹具 ─────────────────────────────────────────────────────


def _ids() -> RequestIDs:
    return RequestIDs(session="ses_test", request="msg_test", project="prj_test")


class _Patched:
    """把上游地址指到桩服务、并强制直连（本地回环不该走系统代理）。"""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> None:
        self._saved = {
            "INFERENCE_BASE": upstream.INFERENCE_BASE,
            "proxies_for": upstream.proxies_for,
        }
        upstream.INFERENCE_BASE = self._base_url
        upstream.proxies_for = lambda url: None  # type: ignore[assignment]

    def __exit__(self, *exc: object) -> None:
        upstream.INFERENCE_BASE = self._saved["INFERENCE_BASE"]
        upstream.proxies_for = self._saved["proxies_for"]


def _patch_timeout(connect: float, idle: float):
    """临时替换发送超时常量（用小值验证机制，不必等 5 分钟）。"""

    class _TimeoutPatch:
        def __enter__(self) -> None:
            self._saved = (
                upstream.OPENCODE_TIMEOUT_CONNECT,
                upstream.OPENCODE_TIMEOUT_IDLE,
            )
            upstream.OPENCODE_TIMEOUT_CONNECT = connect
            upstream.OPENCODE_TIMEOUT_IDLE = idle

        def __exit__(self, *exc: object) -> None:
            upstream.OPENCODE_TIMEOUT_CONNECT, upstream.OPENCODE_TIMEOUT_IDLE = self._saved

    return _TimeoutPatch()


def _patch_attempts(attempts: int, backoff: float):
    class _AttemptsPatch:
        def __enter__(self) -> None:
            self._saved = (
                upstream.OPENCODE_TRANSPORT_ATTEMPTS,
                upstream.OPENCODE_TRANSPORT_BACKOFF_S,
            )
            upstream.OPENCODE_TRANSPORT_ATTEMPTS = attempts
            upstream.OPENCODE_TRANSPORT_BACKOFF_S = backoff

        def __exit__(self, *exc: object) -> None:
            (
                upstream.OPENCODE_TRANSPORT_ATTEMPTS,
                upstream.OPENCODE_TRANSPORT_BACKOFF_S,
            ) = self._saved

    return _AttemptsPatch()


# ── 1. 超时：静默容忍取自 settings，不是 curl_cffi 的 30 ──────────


def test_stream_silence_window_comes_from_settings():
    """流式静默容忍由配置决定，不是 curl_cffi 自带的 30s。

    用小值(1s+2s)验证机制：桩服务收下请求后一直不吐字节，send_upstream 必须在
    connect+idle 的量级上掐掉（实测掐流点是其和的 1.2~1.7 倍），而不是 curl_cffi
    的默认 30s。回退成"不传 timeout"时这条会等到 30s，超出这里的上界。
    """
    server = _StubServer(lambda conn, idx: _hold_silent(conn, idx, 10.0))
    try:
        with _Patched(server.url), _patch_timeout(1.0, 2.0), _patch_attempts(1, 0.0):
            started = time.monotonic()
            try:
                upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
                raise AssertionError("桩服务静默时不该成功")
            except crequests.RequestsError as exc:
                elapsed = time.monotonic() - started
                code = getattr(exc, "code", None)
                message = str(exc)
        assert code == 28, f"期望 curl 28（静默掐流），实得 {code}: {message}"
        assert 3.0 <= elapsed <= 9.0, f"掐流点应≈connect+idle=3s，实测 {elapsed:.1f}s"
    finally:
        server.close()


def test_shipped_silence_window_is_minutes_not_seconds():
    """线上常量的静默容忍必须远离免费档首字区间（实测上限 33.5s）。

    换算与 curl_cffi 的 stream 分支一致：LOW_SPEED_TIME = connect + idle，
    实际掐流点还会再晚一点。取下限做断言：静默容忍至少 105s 才够（30s 那条线
    原本就是被 33.5s 的首字打穿的）。
    """
    window = settings.OPENCODE_TIMEOUT_CONNECT + settings.OPENCODE_TIMEOUT_IDLE
    assert window >= 105, f"静默容忍 {window}s 太紧，会被免费档正常长首字/推理静默打到"
    # 连接超时仍要有上限：出口挂死时线程不能无限占着
    assert 0 < settings.OPENCODE_TIMEOUT_CONNECT <= 30


# ── 2. 重试：连接期失败重发一次，语义错误绝不重发 ─────────────────


def test_connection_reset_is_retried_once():
    """第一次连接被掐断（请求头都没回），第二次正常 200 —— 重发一次并成功。"""

    def handler(conn: socket.socket, index: int) -> None:
        if index == 1:
            _reset(conn, index)
        else:
            _respond(conn, "200 OK", _OK_BODY)

    server = _StubServer(handler)
    try:
        with _Patched(server.url), _patch_timeout(5.0, 5.0), _patch_attempts(2, 0.05):
            result = upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
            assert result.status == 200, result.status
            assert result.read_body() == _OK_BODY
        assert server.connections == 2, f"应恰好重发一次，实得 {server.connections} 次连接"
    finally:
        server.close()


def test_upstream_http_error_is_not_retried():
    """非 2xx 是上游的判决（403 之类），不重发——推理层负责透传。"""
    server = _StubServer(lambda conn, idx: _respond(conn, "403 Forbidden", _FORBIDDEN_BODY))
    try:
        with _Patched(server.url), _patch_timeout(5.0, 5.0), _patch_attempts(2, 0.05):
            result = upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
            assert result.status == 403, result.status
        assert server.connections == 1, f"403 不该重发，实得 {server.connections} 次连接"
    finally:
        server.close()


def test_retry_budget_exhausted_raises_original_error():
    """连接一直被掐断时不无限重试：打满预算后把 curl_cffi 的异常原样抛出。"""
    server = _StubServer(_reset)
    try:
        with _Patched(server.url), _patch_timeout(5.0, 5.0), _patch_attempts(2, 0.05):
            try:
                upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
                raise AssertionError("连接一直失败时不该成功")
            except crequests.RequestsError:
                pass
        assert server.connections == 2, f"预算应为 2 次，实得 {server.connections} 次连接"
    finally:
        server.close()


# ── 3. 失败码分类：事故里的 curl 35 必须落在"可重发"里 ────────────


def test_tls_handshake_abort_really_reports_curl_35():
    """用真实 https 握手验证事故形态：对端在 TLS 握手期关连接 → curl 35。

    这条盯的是"分类依据与线上错误码一致"——线上日志里的
    `SSL_connect: Connection closed abruptly (SSL_ERROR_SYSCALL)` 就是这个码，
    且它必须落在"可重发"里。
    """
    server = _StubServer(_reset)  # 收下 TCP 就关，TLS 握手必然中断
    try:
        try:
            crequests.post(
                f"https://127.0.0.1:{server.port}/x",
                data=b"{}",
                stream=True,
                timeout=(5.0, 5.0),
                proxies=None,  # 本地回环，别走系统代理
                impersonate="chrome",
            )
            raise AssertionError("握手被掐断时不该成功")
        except crequests.RequestsError as exc:
            assert getattr(exc, "code", None) == 35, f"实得 code={exc.code}: {exc}"
            assert upstream._is_retryable_transport(exc), "curl 35 + 无响应头 必须可重发"
    finally:
        server.close()


def test_headers_then_reset_is_not_retried():
    """响应头已到（哪怕一个 chunk 都没吐）就不重发：上游多半已开始处理。

    实测形态 = curl 56 + status_code 200；重发等于让上游二次执行。
    """
    server = _StubServer(_header_then_reset)
    try:
        with _Patched(server.url), _patch_timeout(5.0, 5.0), _patch_attempts(2, 0.05):
            broke = False
            try:
                result = upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
                # 错误也可能晚一步、在取流时才浮出来（取决于 RST 与 peek 的竞速）
                try:
                    b"".join(result.iter_chunks())
                except Exception:
                    broke = True
            except crequests.RequestsError:
                broke = True
            assert broke, "头已到、被打断的流不该被当成成功"
        assert server.connections == 1, f"头已到不该重发，实得 {server.connections} 次连接"
    finally:
        server.close()


def test_classifier_requires_no_response_started():
    """分类器语义：可重发 = 传输类错误码 **且** 一个响应头字节都没解析出来。"""

    class _Rsp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _Exc(Exception):
        def __init__(self, code: int, status_code: int | None) -> None:
            super().__init__(f"curl {code}")
            self.code = code
            if status_code is not None:
                self.response = _Rsp(status_code)

    for code in (6, 7, 28, 35, 52, 56, 92):
        assert upstream._is_retryable_transport(_Exc(code, 0)), f"code={code} 应可重发"
        assert not upstream._is_retryable_transport(_Exc(code, 200)), f"code={code} 头已到不该重发"
        assert not upstream._is_retryable_transport(_Exc(code, None)), "无响应对象要保守"
    # 体读到一半断（curl 18 PARTIAL_FILE）与未知码：不重发
    for code in (18, 999):
        assert not upstream._is_retryable_transport(_Exc(code, 200)), code
        assert not upstream._is_retryable_transport(_Exc(code, 0)), code


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
