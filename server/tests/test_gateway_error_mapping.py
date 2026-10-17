"""网关错误映射测试：模型不可用与池不可用必须给出**不同的**状态码与 code。

这两者客户端该做的事正好相反：

  - `ModelUnavailable`（号都在、只是都不服务这个模型）→ 400 + error.type=`model_unavailable`：
    换模型才有用，退避重试同一个模型是白等（上游对我们没有限流，是我们自己避让）。
  - `UpstreamUnavailable`（池子真的没号：全冷却/全占满）→ 429 + error.type=`insufficient_capacity`：
    退避重试是对的，池子会恢复。

历史上两种成因都报"没有可用账号"（503/429），"模型不可用"被读成"服务端过载"
→ 客户端自动重试 5 次仍失败（参考项目 issue #102 的同款形态）。

运行：cd server && uv run python tests/test_gateway_error_mapping.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import base as gateway_base
from gateway import inference
from gateway.base import (
    ModelUnavailable,
    RequestIDs,
    Route,
    UpstreamUnavailable,
)


class _Provider:
    """最小 provider：路由固定、请求体原样、send 抛预设错误。"""

    name = "fakewb"
    upstream_url = "https://example.invalid"

    def __init__(self, error: Exception):
        self.error = error

    def list_models(self) -> list:
        return []

    def supported(self, model: str) -> bool:
        return True

    def route(self, model: str) -> Route:
        return Route(provider=self.name, model=model, upstream_model=model,
                     protocol="chat", free=False)

    def derive_ids(self, headers, body, client_ip="") -> RequestIDs:
        return RequestIDs(
            session="ses_t", request="msg_t", project="prj_t", client="cli_t"
        )

    def shape(self, body, protocol):
        return body, False

    def status(self) -> dict:
        return {}

    def send(self, *args, **kwargs):
        raise self.error


class _FakeHandler:
    command = "POST"
    close_connection = False

    def __init__(self):
        self.client_address = ("127.0.0.1", 12345)
        self.headers = {"Content-Length": "64", "User-Agent": "test"}
        self.wfile = io.BytesIO()
        self.status = 0
        self.sent: list[tuple[str, str]] = []

    def send_response(self, status, *args):
        self.status = status

    def send_header(self, key, value):
        self.sent.append((key, value))

    def end_headers(self):
        pass

    def payload(self) -> dict:
        raw = self.wfile.getvalue().decode("utf-8")
        return json.loads(raw) if raw else {}


class _StubStore:
    """落库替身：测试不碰 data/ 下的真实 SQLite。"""

    def __init__(self):
        self.records: list = []

    def log_request(self, record) -> None:
        self.records.append(record)


def _run(error: Exception) -> tuple[_FakeHandler, _StubStore]:
    """把一次 chat/completions 走完整条网关错误路径。"""
    provider = _Provider(error)
    gateway_base.registry.register(provider)  # 首个注册 = 默认 provider（裸模型名走它）
    stub = _StubStore()
    original_store = inference.store
    original_body = inference.read_request_body
    inference.store = lambda name: stub  # type: ignore[assignment]
    inference.read_request_body = lambda handler, max_bytes: json.dumps({
        "model": "glm-5.2", "messages": [], "stream": True,
    }).encode("utf-8")  # type: ignore[assignment]
    handler = _FakeHandler()
    try:
        inference.handle(handler, "POST", "/v1/chat/completions", {})
    finally:
        inference.store = original_store  # type: ignore[assignment]
        inference.read_request_body = original_body  # type: ignore[assignment]
    return handler, stub


def test_model_unavailable_maps_to_400():
    handler, stub = _run(ModelUnavailable(
        "model glm-5.2 is unavailable on every account (per-model cooldown), try another model",
        hint="model_blocked: 2 个账号对该模型处于冷却",
    ))
    assert handler.status == 400
    payload = handler.payload()
    # 我们的错误信封把 kind 放在 error.type（OpenAI 形协议；Anthropic 形是同名嵌套）
    assert payload["error"]["type"] == "model_unavailable"
    assert "try another model" in payload["error"]["message"]
    # 记录里留下原文（面板"原因"列直接可读）
    assert stub.records and "unavailable on every account" in stub.records[0].error_message


def test_pool_unavailable_still_maps_to_429():
    handler, _stub = _run(UpstreamUnavailable("workbuddy: no available cn account"))
    assert handler.status == 429
    assert handler.payload()["error"]["type"] == "insufficient_capacity"


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
