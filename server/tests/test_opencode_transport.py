"""opencode 出站传输回归测试（对照 2026-10-15 及 2026-10-17 线上事故）。

三件事在这里锁死，任何一处回退都会变红，而不是等线上又批量 504/截断：

1. **本地不设超时，且必须显式传 `timeout=None`**。curl_cffi 在 stream 模式下把
   tuple 翻译成 CONNECTTIMEOUT + LOW_SPEED；省略 timeout 会落回它自带的默认
   30/30，等于给免费档的长首字与推理期静默设了条 30s 死线。
2. **响应头之前的传输失败按预算重发 + 几何退避**。出口线路会间歇性在握手期掐断
   新连接（curl 35/56），且是成串出现的：2026-10-17 实测同一分钟内 0/12 全败、
   几分钟后 3/3 全通。单凭证不重试就是客户端可见的 502。
3. **判据只有一条：响应头一个字节都没到**（status 0）。错误码不进判据——出口线路
   的故障码枚举不完；头一旦到过就不重发（上游多半已开始处理，重发是二次执行）。

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


def _patch_attempts(attempts: int, backoff: float):
    class _AttemptsPatch:
        def __enter__(self) -> None:
            self._saved = (
                upstream.OPENCODE_TRANSPORT_ATTEMPTS,
                upstream.OPENCODE_TRANSPORT_BACKOFF_S,
                upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S,
            )
            upstream.OPENCODE_TRANSPORT_ATTEMPTS = attempts
            upstream.OPENCODE_TRANSPORT_BACKOFF_S = backoff
            # 夹具里把封顶抬高，让用例自己决定退避曲线（生产封顶另有专门用例）。
            upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S = max(backoff * 8, backoff)

        def __exit__(self, *exc: object) -> None:
            (
                upstream.OPENCODE_TRANSPORT_ATTEMPTS,
                upstream.OPENCODE_TRANSPORT_BACKOFF_S,
                upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S,
            ) = self._saved

    return _AttemptsPatch()


# ── 1. 超时：本地不设，且必须显式传 None ──────────────────────────


def test_outbound_timeout_is_disabled_explicitly():
    """本地不设超时；必须显式传 None，省略会落回 curl_cffi 默认的 30/30。"""

    captured: dict[str, Any] = {}

    class _Raw:
        status_code = 200
        headers: dict[str, str] = {}  # noqa: RUF012  stub 不实例化，不写共享默认值

    def fake_post(url: str, **kwargs: Any) -> _Raw:
        captured.update(kwargs)
        return _Raw()

    saved = upstream.crequests.post
    upstream.crequests.post = fake_post  # type: ignore[assignment]
    try:
        upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
    finally:
        upstream.crequests.post = saved
    assert captured.get("timeout", "missing") is None, (
        f"必须显式传 timeout=None，实得 {captured.get('timeout', 'missing')!r}"
    )


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
        with _Patched(server.url), _patch_attempts(2, 0.05):
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
        with _Patched(server.url), _patch_attempts(2, 0.05):
            result = upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
            assert result.status == 403, result.status
        assert server.connections == 1, f"403 不该重发，实得 {server.connections} 次连接"
    finally:
        server.close()


def test_retry_budget_exhausted_raises_original_error():
    """连接一直被掐断时不无限重试：打满预算后把 curl_cffi 的异常原样抛出。"""
    server = _StubServer(_reset)
    try:
        with _Patched(server.url), _patch_attempts(4, 0.01):
            try:
                upstream.send_upstream(PROTOCOL_CHAT, b"{}", _ids(), stream=True)
                raise AssertionError("连接一直失败时不该成功")
            except crequests.RequestsError:
                pass
        assert server.connections == 4, f"预算应为 4 次，实得 {server.connections} 次连接"
    finally:
        server.close()


def test_backoff_grows_geometrically_and_caps():
    """退避是几何增长的（成串掐断要拉开间隔），并封顶在 MAX。"""
    with _patch_attempts(4, 0.3):
        waits = [upstream._retry_backoff(i) for i in range(1, 4)]
    assert waits == [0.3, 0.6, 1.2], waits
    saved = upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S
    upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S = 1.0
    try:
        assert upstream._retry_backoff(4) == 1.0, "超过 MAX 应封顶"
        assert upstream._retry_backoff(9) == 1.0, "远超 MAX 也应封顶"
    finally:
        upstream.OPENCODE_TRANSPORT_BACKOFF_MAX_S = saved


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
        with _Patched(server.url), _patch_attempts(2, 0.05):
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
    """分类器语义：可重发 = **一个响应头字节都没解析出来**；错误码不参与判定。

    错误码白名单曾把新形态的出口故障漏掉（实测除了 35/56/92 还有 16/55 这类），
    所以判据收窄到唯一有语义的那条：status 0 才允许重发；拿不到响应对象时保守。
    """

    class _Rsp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _Exc(Exception):
        def __init__(self, code: int, status_code: int | None) -> None:
            super().__init__(f"curl {code}")
            self.code = code
            if status_code is not None:
                self.response = _Rsp(status_code)

    # 已知的与"没见过"的错误码一视同仁：只要响应头没到就可重发
    for code in (1, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92, 999):
        assert upstream._is_retryable_transport(_Exc(code, 0)), f"code={code} + 头没到 应可重发"
        assert not upstream._is_retryable_transport(_Exc(code, 200)), f"code={code} 头已到不该重发"
    # 体读到一半断（curl 18 + status 200）与 403 类判决同样不重发
    assert not upstream._is_retryable_transport(_Exc(18, 200)), "体读到一半断不该重发"
    # 拿不到响应对象 = 判据不成立：保守不重发
    assert not upstream._is_retryable_transport(_Exc(35, None)), "无响应对象要保守"


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
