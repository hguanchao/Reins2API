"""HTTP 响应写出辅助（与具体路由无关，供 api/ 与 gateway/ 共用）。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

CORS_ORIGIN = "*"
_CORS_HEADERS = (
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
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", _CORS_HEADERS)
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
    handler.send_header("Access-Control-Allow-Headers", _CORS_HEADERS)
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
