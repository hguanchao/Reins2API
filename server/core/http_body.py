"""HTTP body 读取：按 Content-Length 读满，并给客户端等待时间设上限。"""

from http.server import BaseHTTPRequestHandler

CLIENT_BODY_READ_TIMEOUT = 15.0


class IncompleteRequestBodyError(ConnectionError):
    """客户端在发送完 Content-Length 字节前断开。"""


class RequestBodyTooLarge(ValueError):
    """声明的请求体超过本端点的上限。"""


def read_request_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int,
) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > max_bytes:
        handler.close_connection = True
        raise RequestBodyTooLarge(f"请求体过大（>{max_bytes} bytes）")

    connection = handler.connection
    previous_timeout = connection.gettimeout()
    connection.settimeout(CLIENT_BODY_READ_TIMEOUT)
    try:
        body = handler.rfile.read(length)
    finally:
        connection.settimeout(previous_timeout)
    if len(body) != length:
        raise IncompleteRequestBodyError("请求体未完整接收")
    return body
