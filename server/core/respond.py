"""HTTP 响应写出辅助（与具体路由无关，供 api/ 与 gateway/ 共用）。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

CORS_ORIGIN = "*"
# 管理面与推理端点共用的 CORS 头集合：单一来源，OPTIONS 预检与普通响应都取这里，
# 避免两处各写一份后漂移（此前 api/server.py 与 core/respond.py 各持一份）。
CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
CORS_HEADERS = (
    "Content-Type, Authorization, X-Api-Key, HTTP-Referer, X-Title, "
    "OpenAI-Beta, anthropic-version, anthropic-beta, x-api-key, x-request-id"
)


def send_json(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Any,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
    handler.send_header("Access-Control-Allow-Methods", CORS_METHODS)
    handler.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
    handler.send_header("Access-Control-Expose-Headers", "*")
    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
        handler.wfile.flush()


def send_bytes(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
    handler.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
        handler.wfile.flush()


def error_json(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    """管理 API 的统一错误信封。"""
    send_json(handler, status, {"ok": False, "error": message})
