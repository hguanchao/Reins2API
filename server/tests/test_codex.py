"""codex provider 单元测试（OAuth / 塑形 / 头族 / 分类 / 池 / 端到端 send）。

运行：cd server && uv run python tests/test_codex.py
（有 pytest 时也可 uv run python -m pytest tests/test_codex.py -q）
"""

from __future__ import annotations

import base64
import contextlib
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.settings import CODEX_CLIENT_ID, CODEX_REDIRECT_URI
from gateway.base import RequestIDs
from providers.codex import classify, oauth, shape
from providers.codex import upstream as cx_upstream
from providers.codex.pool import Pool
from providers.codex.provider import CodexProvider, _Catalog
from providers.codex.store import Account, CodexStore

# ── 工具 ──


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_id_token(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


def _provider_with_catalog(store: CodexStore, models: list[str]) -> CodexProvider:
    provider = CodexProvider(store=store)
    provider._catalog = _Catalog(models={m: {"id": m} for m in models}, refreshed_at="t")
    return provider


def _seed_account(store: CodexStore, uid: str = "acc-1", **kw) -> Account:
    account = Account(
        uid=uid,
        account_id=kw.pop("account_id", uid),
        access_token=kw.pop("access_token", "at"),
        refresh_token=kw.pop("refresh_token", "rt"),
        token_expires_ms=kw.pop("token_expires_ms", int(time.time() * 1000) + 3_600_000),
        **kw,
    )
    store.upsert_account(account)
    return account


@contextlib.contextmanager
def _patched(target, name: str, value):
    """临时替换模块/对象属性（刷新路径的测试要同时打桩 oauth 与 store 上的方法）。"""
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


class _TokenResp:
    """token 端点响应桩（只用到 _post_token 读的两个字段）。"""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


# ── OAuth ──


def test_pkce_challenge_is_s256_base64url():
    # RFC 7636 附录 B 的已知向量
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    v, c = oauth.new_pkce()
    assert len(v) == 128 and "=" not in c and c == oauth.code_challenge(v)


def test_authorize_url_has_required_params():
    url = oauth.authorize_url("state123", "chal456")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    for frag in (
        "response_type=code",
        f"client_id={CODEX_CLIENT_ID}",
        "code_challenge_method=S256",
        "code_challenge=chal456",
        "state=state123",
        "codex_cli_simplified_flow=true",
        "id_token_add_organizations=true",
        "offline_access",
    ):
        assert frag in url, frag
    assert CODEX_REDIRECT_URI.split("://")[1].split("/")[0] in url.replace("%3A", ":").replace("%2F", "/")


def test_parse_id_token_extracts_account_info():
    token = _make_id_token({
        "email": "user@example.com",
        "name": "初十",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-xyz",
            "chatgpt_plan_type": "plus",
        },
    })
    info = oauth.parse_id_token(token)
    assert info.email == "user@example.com"
    assert info.account_id == "acc-xyz"
    assert info.plan_type == "plus"
    assert info.name == "初十"                 # 昵称取自 name claim（面板「上昵称下邮箱」用）
    # 坏输入不炸
    assert oauth.parse_id_token("").email == ""
    assert oauth.parse_id_token("").name == ""
    assert oauth.parse_id_token("not.a.jwt").account_id == ""
    # uid 优先 account_id，退化 email
    assert oauth.account_uid(info) == "acc-xyz"
    assert oauth.account_uid(oauth.AccountInfo(email="e@x.y")) == "e@x.y"


def test_account_public_dict_includes_nickname():
    """账号视图带 nickname（从 id_token 现解）；无 id_token 时为空串，不抛。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            token = _make_id_token({"email": "n@x.y", "name": "初十"})
            with_name = _seed_account(store, "acc-1", id_token=token)
            assert with_name.public_dict()["nickname"] == "初十"
            without = _seed_account(store, "acc-2", id_token="")
            assert without.public_dict()["nickname"] == ""
        finally:
            store.close()


# ── 出站塑形 ──


def test_shape_strips_and_normalizes():
    body = {
        "model": "gpt-5-codex",
        "stream": False,
        "store": True,
        "previous_response_id": "resp_1",
        "prompt_cache_retention": "24h",
        "safety_identifier": "s",
        "disable_response_storage": True,
        "type": "response.create",
        "input": [],
    }
    changed = shape.shape_codex_body(body, "")
    assert changed is True
    for gone in ("previous_response_id", "prompt_cache_retention", "safety_identifier",
                 "disable_response_storage", "type"):
        assert gone not in body, gone
    assert body["instructions"] == ""
    assert body["store"] is False
    assert body["stream"] is True
    # 空会话不注入 prompt_cache_key
    assert "prompt_cache_key" not in body
    # 幂等
    assert shape.shape_codex_body(body, "") is False


def test_shape_injects_client_cache_key():
    body = {"model": "m", "input": []}
    assert shape.shape_codex_body(body, "cli-1") is True
    assert body["prompt_cache_key"] == "cli-1"
    assert shape.shape_codex_body(body, "cli-1") is False


# ── 头族 ──


def test_codex_headers_minimal_trusted_set():
    from providers.codex import upstream

    account = Account(uid="acc-1", account_id="acc-1", access_token="AT")
    headers = upstream.codex_headers(account, "ses-9")
    assert headers["Authorization"] == "Bearer AT"
    assert headers["Chatgpt-Account-Id"] == "acc-1"
    assert headers["Accept"] == "text/event-stream"
    assert headers["Originator"] == "codex_cli_rs"
    assert headers["session-id"] == headers["thread-id"] == headers["x-client-request-id"] == "ses-9"
    assert "Connection" not in headers
    # 无 account_id 时不发该头（宁缺毋滥）
    assert "Chatgpt-Account-Id" not in upstream.codex_headers(
        Account(uid="u", access_token="AT"), "s"
    )


def test_codex_user_agent_matches_official_form():
    """出站 UA 形态对齐官方（codex-rs login/src/auth/default_client.rs）：

        {originator}/{version} ({os_type} {os_version}; {arch}) {terminal}

    originator 与 Originator 头同源；后缀只有 app-server 通道注入时才出现——
    CLI 形态是四段、无后缀（0.155.0 时代已无 codex-tui workaround）。
    """
    from core.settings import CODEX_CLI_VERSION, CODEX_ORIGINATOR, CODEX_USER_AGENT

    assert CODEX_ORIGINATOR == "codex_cli_rs"
    assert CODEX_USER_AGENT == (
        f"codex_cli_rs/{CODEX_CLI_VERSION} (Mac OS 15.5.0; arm64) xterm-256color"
    )
    # 只有平台那一对括号：app-server 后缀会是紧随其后的第二个 (...) 组
    assert CODEX_USER_AGENT.count("(") == 1 and CODEX_USER_AGENT.count(")") == 1
    assert "codex-tui" not in CODEX_USER_AGENT  # 已从官方移除的兼容 workaround 形态


def test_turn_state_header_helpers():
    """x-codex-turn-state：响应头取值大小写不敏感；无令牌时不发该头。"""
    account = Account(uid="acc-1", account_id="acc-1", access_token="AT")
    assert cx_upstream.turn_state_of({"X-Codex-Turn-State": "ts-1"}) == "ts-1"
    assert cx_upstream.turn_state_of({"x-codex-turn-state": " ts-2 "}) == "ts-2"
    assert cx_upstream.turn_state_of({"content-type": "text/event-stream"}) == ""
    headers = cx_upstream.codex_headers(account, "ses-9")
    assert cx_upstream.TURN_STATE_HEADER not in headers  # 无令牌不出现


# ── 错误分类 ──


def test_classify_status_mapping():
    assert classify.classify(401, "").kind == classify.AUTH
    assert classify.classify(429, "").kind == classify.RATE_LIMIT
    assert classify.classify(404, "").kind == classify.NOT_FOUND
    assert classify.classify(503, "").kind == classify.SERVER
    assert classify.classify(400, "").kind == classify.CLIENT
    assert classify.classify(403, "<html>cloudflare</html>").kind == classify.WAF
    assert classify.classify(403, '{"error":{"message":"nope"}}').kind == classify.AUTH
    assert classify.classify(400, "").retryable is False
    assert classify.classify(429, "").retryable is True


def test_classify_retry_after_header():
    c = classify.apply_retry_after(classify.Classified(kind=classify.RATE_LIMIT), {"retry-after": "42"})
    assert c.retry_after_s == 42.0
    c = classify.apply_retry_after(classify.Classified(kind=classify.RATE_LIMIT), {"retry-after": "999999"})
    assert c.retry_after_s == 0.0  # 超上限丢弃


def test_error_message_extraction():
    assert classify.error_message('{"error":{"message":"boom"}}') == "boom"
    assert classify.error_message('{"detail":"nope"}') == "nope"
    assert classify.error_message("plain text") == "plain text"


# ── store ──


def test_store_crud_and_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", label="me@x.y", email="me@x.y", plan_type="plus")
            got = store.get_account("acc-1")
            assert got is not None and got.email == "me@x.y" and got.plan_type == "plus"
            assert got.account_cooldown_active(int(time.time() * 1000)) is False

            store.update_tokens("acc-1", "AT2", "", 123456, id_token="idt", plan_type="pro")
            got = store.get_account("acc-1")
            assert got.access_token == "AT2"
            assert got.refresh_token == "rt"  # 未换发新 RT → 保留旧值
            assert got.token_expires_ms == 123456 and got.plan_type == "pro"

            store.update_runtime("acc-1", cooldown_until_ms=999, cooldown_kind="soft", bogus="x")
            got = store.get_account("acc-1")
            assert got.cooldown_kind == "soft"
            assert not hasattr(got, "bogus")

            assert store.count_by_status() == {"active": 1}
            assert store.delete_account("acc-1") is True
            assert store.get_account("acc-1") is None
        finally:
            store.close()


def test_store_uid_validation():
    assert CodexStore.__name__ == "CodexStore"
    from providers.codex.store import valid_uid

    assert valid_uid("acc-1")
    assert valid_uid("a@b.co")
    assert not valid_uid("")
    assert not valid_uid("has space")
    assert not valid_uid("x" * 200)


# ── 池 ──


def test_pool_skips_cooldown_and_breaker():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            now = int(time.time() * 1000)
            _seed_account(store, "healthy")
            _seed_account(store, "cooling", cooldown_until_ms=now + 60_000, cooldown_kind="soft")
            _seed_account(store, "broken", breaker_until_ms=now + 60_000, breaker_count=3)
            _seed_account(store, "no-token", access_token="")
            pool = Pool(store)
            picked = {pool.pick("s").uid for _ in range(40)}
            assert picked == {"healthy"}
            # 全冷却兜底：只剩冷却号时也能选出来（不返回 None）
            store.update_runtime("healthy", cooldown_until_ms=now + 5_000, cooldown_kind="soft")
            assert pool.pick("s") is not None
        finally:
            store.close()


def test_pool_sticky_and_in_flight():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "a")
            _seed_account(store, "b")
            pool = Pool(store)
            pool.rebind_sticky("ses-1", "b")
            assert pool.sticky_uid("ses-1") == "b"
            assert pool.pick("ses-1").uid == "b"
            # 在途占满 → 不再选它
            assert pool.acquire("b", "r1") and pool.acquire("b", "r2") and pool.acquire("b", "r3")
            assert pool.acquire("b", "r4") is False
            assert pool.pick("other").uid == "a"
        finally:
            store.close()


# ── provider 端到端 ──


def test_provider_route_and_models():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = _provider_with_catalog(store, ["gpt-5-codex", "gpt-5"])
            assert provider.supported("cx:gpt-5-codex")
            assert not provider.supported("cx:nope")
            route = provider.route("cx:gpt-5-codex")
            assert route.protocol == "responses"
            assert route.upstream_model == "gpt-5-codex"
            assert route.model == "cx:gpt-5-codex"
            infos = {m.id: m for m in provider.list_models()}
            assert set(infos) == {"cx:gpt-5-codex", "cx:gpt-5"}
            assert infos["cx:gpt-5-codex"].protocol == "responses"
            assert infos["cx:gpt-5-codex"].capabilities["supports_reasoning"] is True
        finally:
            store.close()


def test_send_injects_client_cache_key_and_passes_session_through():
    import providers.codex.upstream as cx_upstream

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")
            provider = _provider_with_catalog(store, ["gpt-5-codex"])

            class _Resp:
                status_code = 200

                def __init__(self) -> None:
                    self.headers: dict[str, str] = {"content-type": "text/event-stream"}

                def iter_content(self, chunk_size: int = 8192):
                    yield b"data: {}\n\n"

                def close(self) -> None:
                    pass

            seen: dict[str, object] = {}

            def fake_request(account, body, session_id, turn_state=""):
                seen["body"] = json.loads(body)
                seen["session"] = session_id
                return _Resp()

            original = cx_upstream.responses_request
            cx_upstream.responses_request = fake_request
            try:
                # 真实链路：inference 先调 shape()（静态归一），再调 send()（客户端键注入）
                outbound = {"model": "gpt-5-codex", "input": [], "store": True}
                provider.shape(outbound, "responses")
                resp = provider.send(
                    provider.route("cx:gpt-5-codex"),
                    "responses",
                    json.dumps(outbound).encode(),
                    RequestIDs(session="ses-42", request="r1", project="p", client="cli-42"),
                    stream=True,
                )
            finally:
                cx_upstream.responses_request = original

            assert resp.status == 200
            assert resp.account == "acc-1"
            # 出站 session 头仍是会话键（粘性选号靠它）
            assert seen["session"] == "ses-42"
            body = seen["body"]
            # prompt_cache_key 用客户端键：静态前缀跨会话共享
            assert body["prompt_cache_key"] == "cli-42"
            assert body["store"] is False
            assert body["stream"] is True
            assert body["instructions"] == ""
        finally:
            store.close()


def test_turn_state_captured_and_echoed_per_account_session():
    """响应头里的 x-codex-turn-state 记住并在同「账号×会话」的后续请求回显。

    官方客户端就是会话级持有、换账号即重置（codex-rs core/src/client.rs:2461-2484
    与 :1688-1692）；网关按 uid×session 作键，轮换到别的号自然取不到旧令牌。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")
            provider = _provider_with_catalog(store, ["gpt-5-codex"])

            seen: list[str] = []
            states = iter(["ts-1", "ts-2", "ts-3"])

            class _Resp:
                status_code = 200

                def __init__(self, turn_state: str) -> None:
                    self.headers: dict[str, str] = {"content-type": "text/event-stream"}
                    self.headers[cx_upstream.TURN_STATE_HEADER] = turn_state

                def iter_content(self, chunk_size: int = 8192):
                    yield b"data: {}\n\n"

                def close(self) -> None:
                    pass

            def fake_request(account, body, session_id, turn_state=""):
                seen.append(turn_state)
                return _Resp(next(states))

            original = cx_upstream.responses_request
            cx_upstream.responses_request = fake_request
            try:
                route = provider.route("cx:gpt-5-codex")
                outbound = {"model": "gpt-5-codex", "input": []}
                provider.shape(outbound, "responses")
                raw = json.dumps(outbound).encode()
                for session, request_id in (("ses-tt", "r1"), ("ses-tt", "r2"), ("ses-other", "r3")):
                    provider.send(
                        route, "responses", raw,
                        RequestIDs(session=session, request=request_id, project="p"),
                        stream=True,
                    )
            finally:
                cx_upstream.responses_request = original

            # 第一次没有令牌；第二次带上第一次捕获的；另一个会话不串用
            assert seen == ["", "ts-1", ""], seen
        finally:
            store.close()


def test_send_rotates_on_error():
    """非 2xx 且可重试 → 换号重试；预算耗尽返回最后一个上游错误。"""
    import providers.codex.upstream as cx_upstream

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "a")
            _seed_account(store, "b")
            provider = _provider_with_catalog(store, ["m"])
            calls: list[str] = []

            class _Resp:
                def __init__(self, status: int) -> None:
                    self.status_code = status
                    self.headers: dict[str, str] = {}

                def iter_content(self, chunk_size: int = 8192):
                    yield b'{"error":{"message":"rate limited"}}'

                def close(self) -> None:
                    pass

            def fake_request(account, body, session_id, turn_state=""):
                calls.append(account.uid)
                return _Resp(429)

            original = cx_upstream.responses_request
            cx_upstream.responses_request = fake_request
            try:
                resp = provider.send(
                    provider.route("cx:m"), "responses",
                    json.dumps({"model": "m", "input": []}).encode(),
                    RequestIDs(session="s", request="r", project="p"), stream=True,
                )
            finally:
                cx_upstream.responses_request = original

            assert resp.status == 429
            assert len(calls) >= 2            # 至少换过一次号
            assert len(set(calls)) >= 2
        finally:
            store.close()


def test_manual_add_with_access_token_only():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            account = provider.manual_add_account(
                {"access_token": "AT", "account_id": "acc-9", "label": "x"}
            )
            assert account.uid == "acc-9"
            assert store.get_account("acc-9").access_token == "AT"
            # 缺 account_id 报错
            try:
                provider.manual_add_account({"access_token": "AT"})
                raise AssertionError("should have raised")
            except ValueError:
                pass
            # 两者都缺报错
            try:
                provider.manual_add_account({})
                raise AssertionError("should have raised")
            except ValueError:
                pass
        finally:
            store.close()


def test_account_actions():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")
            provider = CodexProvider(store=store)
            assert provider.account_action("acc-1", "pause")["status"] == "paused"
            assert provider.account_action("acc-1", "resume")["status"] == "active"
            store.update_runtime("acc-1", cooldown_until_ms=10 ** 13, breaker_count=5)
            provider.account_action("acc-1", "revive")
            got = store.get_account("acc-1")
            assert got.cooldown_until_ms == 0 and got.breaker_count == 0
            try:
                provider.account_action("nope", "pause")
                raise AssertionError("should have raised")
            except KeyError:
                pass
            try:
                provider.account_action("acc-1", "bogus")
                raise AssertionError("should have raised")
            except ValueError:
                pass
        finally:
            store.close()


def test_parse_rate_limits_from_headers():
    """额度来自 /responses 响应头的 x-codex-{primary,secondary}-* 三件套。"""
    from providers.codex import upstream

    snapshot = upstream.parse_rate_limits({
        "x-codex-primary-used-percent": "37",
        "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-after-seconds": "120",
        "x-codex-secondary-used-percent": "8",
        "x-codex-secondary-window-minutes": "10080",
        "x-codex-plan-type": "plus",
    })
    assert snapshot is not None
    assert snapshot["primary"]["used_percent"] == 37.0
    assert snapshot["primary"]["window_minutes"] == 300.0
    assert snapshot["primary"]["reset_at_ms"] > snapshot["at_ms"]
    assert snapshot["secondary"]["used_percent"] == 8.0
    assert snapshot["plan_type"] == "plus"
    # 没有额度头（错误响应/非 Codex 上游）→ None，调用方保留旧快照
    assert upstream.parse_rate_limits({"content-type": "application/json"}) is None
    # 脏值不炸，按缺失处理
    dirty = upstream.parse_rate_limits({"x-codex-primary-used-percent": "n/a"})
    assert dirty is None


def test_parse_usage_payload_keeps_concrete_fields():
    """/usage 才有的字段（上游判定 + 积分 + 重置券）进快照；free 档的空壳不进。

    形状取自 2026-10-14 实测的 /usage 原始响应（free 档：credits 全 null/false、
    reset_credits 0、单个 30d 窗口）。"""
    from providers.codex import upstream

    rich = upstream.parse_usage_payload(
        {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 37,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 120,
                },
                "secondary_window": None,
            },
            "credits": {"has_credits": True, "unlimited": False, "balance": "12.5"},
            "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 1},
        },
        now_ms=1_000_000,
    )
    assert rich is not None
    assert rich["allowed"] is True and rich["limit_reached"] is False
    assert rich["primary"]["window_minutes"] == 300.0
    assert "secondary" not in rich  # 上游 secondary_window 为 null → 快照里没有这条
    assert rich["credits"] == {"has_credits": True, "unlimited": False, "balance": 12.5}
    assert rich["reset_credits"] == 2
    assert rich["plan_type"] == "plus"

    free = upstream.parse_usage_payload(
        {
            "plan_type": "free",
            "rate_limit": {
                "allowed": False,
                "limit_reached": True,
                "primary_window": {
                    "used_percent": 100,
                    "limit_window_seconds": 2592000,
                    "reset_after_seconds": 109141,
                    "reset_at": 1792074587,
                },
                "secondary_window": None,
            },
            "credits": {
                "has_credits": False,
                "unlimited": False,
                "balance": None,
                "approx_local_messages": None,
                "approx_cloud_messages": None,
            },
            "rate_limit_reset_credits": {"available_count": 0, "applicable_available_count": 0},
        },
        now_ms=1_000_000,
    )
    # 空壳不落库：前端就不会多长出一段空文案
    assert "credits" not in free and "reset_credits" not in free
    assert free["limit_reached"] is True and free["allowed"] is False
    assert free["primary"]["reset_at_ms"] == 1792074587 * 1000
    # 没有额度段（错误形状）→ None，调用方保留旧快照
    assert upstream.parse_usage_payload({"rate_limit": None}, now_ms=0) is None


def test_usage_only_fields_survive_header_backfill():
    """响应头回填不能抹掉 /usage 才有的字段（allowed / 积分 / 重置券）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", label="me@x.y", email="me@x.y")
            provider = CodexProvider(store=store)
            # 先来一次主动查询（/usage）：带上响应头没有的那几个字段
            store.update_runtime(
                "acc-1",
                rate_limit={
                    "primary": {"used_percent": 37.0, "window_minutes": 300.0},
                    "allowed": True,
                    "limit_reached": False,
                    "credits": {"has_credits": True, "balance": 12.5},
                    "reset_credits": 2,
                    "at_ms": 1_000_000,
                },
            )
            account = store.get_account("acc-1")
            # 再来一次普通响应（只有响应头）：窗口变了，触发回填
            provider._note_rate_limit(
                account,
                {
                    "x-codex-primary-used-percent": "44",
                    "x-codex-primary-window-minutes": "300",
                    "x-codex-secondary-used-percent": "8",
                    "x-codex-secondary-window-minutes": "10080",
                },
            )
            got = store.get_account("acc-1").rate_limit
            assert got["primary"]["used_percent"] == 44.0
            assert got["allowed"] is True and got["limit_reached"] is False
            assert got["credits"] == {"has_credits": True, "balance": 12.5}
            assert got["reset_credits"] == 2
        finally:
            store.close()


def test_mark_window_rebaselines_only_on_new_window():
    """窗口基线是容量估算的分母来源（见 providers/codex/quota.py）。

    判定看 reset_at_ms：每个窗口的重置时刻都不同，它变了就是进了新窗口，必须重立基线
    （旧的百分比已被清零，拿旧基线算出来的跨度是负的）。同一窗口内重复观测不许覆盖，
    否则"窗口内最早那个观测点"这个前提就没了，估算会随流量漂移。
    """
    reset_a = 1_794_305_321_000
    reset_b = reset_a + 30 * 86_400_000
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            first = {"primary": {"used_percent": 12.0, "window_minutes": 43200, "reset_at_ms": reset_a}}
            provider._mark_window("acc-1", first, {})
            assert first["window"]["used_percent"] == 12.0
            assert first["window"]["reset_at_ms"] == reset_a

            again = {"primary": {"used_percent": 40.0, "window_minutes": 43200, "reset_at_ms": reset_a}}
            provider._mark_window("acc-1", again, first)
            assert "window" not in again, "同一窗口内不该重立基线"

            rolled = {"primary": {"used_percent": 3.0, "window_minutes": 43200, "reset_at_ms": reset_b}}
            provider._mark_window("acc-1", rolled, first)
            assert rolled["window"]["reset_at_ms"] == reset_b
            assert rolled["window"]["used_percent"] == 3.0
        finally:
            store.close()


def test_parse_rate_limits_keeps_active_limit_and_over_secondary():
    snapshot = cx_upstream.parse_rate_limits(
        {
            "x-codex-primary-used-percent": "44",
            "x-codex-primary-window-minutes": "43200",
            "x-codex-primary-reset-at": "1794305321",
            "x-codex-active-limit": "premium",
            "x-codex-primary-over-secondary-limit-percent": "7",
        }
    )
    assert snapshot is not None
    assert snapshot["active_limit"] == "premium"
    assert snapshot["over_secondary_percent"] == 7.0
    # reset-at 优先于 reset-after-seconds：绝对时刻少一层本地时钟漂移
    assert snapshot["primary"]["reset_at_ms"] == 1794305321000
    # free 档恒 0 的占位不落库，免得界面上多一个恒零字段
    zero = cx_upstream.parse_rate_limits(
        {
            "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "43200",
            "x-codex-primary-over-secondary-limit-percent": "0",
        }
    )
    assert zero is not None and "over_secondary_percent" not in zero


def test_quota_exhausted_locks_account_until_window_reset():
    """额度用尽 → 按重置时刻挂 quota 冷却；额度恢复 → 解除。

    为什么要有这条：号池只认冷却、不认百分比（`_healthy` 里没有额度判断），所以"用尽"
    如果不落成冷却，一个 100% 的号会被反复选中、每次撞 429。也不能改成 status=disabled：
    那要额外回答"谁来恢复、何时恢复"，而冷却天然带到期时刻。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            reset_at = int(time.time() * 1000) + 30 * 86_400_000
            _seed_account(store, "acc-1")
            provider = CodexProvider(store=store)
            exhausted = {
                "primary": {"used_percent": 100.0, "window_minutes": 43200,
                            "reset_at_ms": reset_at},
                "allowed": False,
                "limit_reached": True,
            }
            assert provider._arm_quota_cooldown("acc-1", exhausted) is True
            cooled = store.get_account("acc-1")
            assert cooled.cooldown_kind == "quota"
            assert cooled.cooldown_until_ms == reset_at
            # 幂等：同一个窗口重复调用不再写库（返回值即"有没有改动"）
            assert provider._arm_quota_cooldown("acc-1", exhausted) is False

            # 额度恢复：解除冷却
            provider._clear_quota_cooldown(store.get_account("acc-1"))
            recovered = store.get_account("acc-1")
            assert recovered.cooldown_until_ms == 0 and recovered.cooldown_kind == ""

            # 别的冷却类别不该被"额度恢复"顺手清掉
            store.update_runtime("acc-1", cooldown_until_ms=reset_at, cooldown_kind="auth")
            provider._clear_quota_cooldown(store.get_account("acc-1"))
            assert store.get_account("acc-1").cooldown_kind == "auth"

            # 重置时刻已过（快照陈旧）不动手：宁可让它去试一次，也不凭猜测锁号
            store.update_runtime("acc-1", cooldown_until_ms=0, cooldown_kind="")
            assert provider._arm_quota_cooldown(
                "acc-1", {"primary": {"reset_at_ms": int(time.time() * 1000) - 1000}}
            ) is False
        finally:
            store.close()


def test_quota_cooldown_not_shortened_by_rate_limit_backoff():
    """429 的短退避不许覆盖额度冷却：额度用尽等到重置（可能几十天），429 退避最长 2 小时。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            reset_at = int(time.time() * 1000) + 30 * 86_400_000
            _seed_account(store, "acc-1", cooldown_until_ms=reset_at, cooldown_kind="quota")
            provider = CodexProvider(store=store)
            account = store.get_account("acc-1")
            provider._penalize(account, classify.Classified(kind=classify.RATE_LIMIT), "429")
            cooled = store.get_account("acc-1")
            assert cooled.cooldown_kind == "quota", "额度冷却被 429 覆盖了"
            assert cooled.cooldown_until_ms == reset_at, "冷却被缩短了"
        finally:
            store.close()


def test_quota_429_locks_until_window_reset():
    """额度用尽的 429（固定文案）按窗口重置锁定，而不是 2 小时软退避。

    这是"100% 但 allowed=True"那种自相矛盾读数的兜底：百分比是整数、上游偶尔自相矛盾，
    而错误文案是它真正拒绝的原因，优先信它。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            reset_at = int(time.time() * 1000) + 30 * 86_400_000
            _seed_account(
                store, "acc-1",
                rate_limit={
                    "primary": {"used_percent": 100.0, "reset_at_ms": reset_at},
                    "allowed": True,          # 上游自相矛盾：说 allowed，但请求被拒
                    "limit_reached": False,
                },
            )
            provider = CodexProvider(store=store)
            account = store.get_account("acc-1")
            provider._penalize(
                account, classify.Classified(kind=classify.RATE_LIMIT),
                '{"error":{"message":"The usage limit has been reached"}}',
            )
            cooled = store.get_account("acc-1")
            assert cooled.cooldown_kind == "quota", f"应锁到窗口重置，实际 {cooled.cooldown_kind}"
            assert cooled.cooldown_until_ms == reset_at
        finally:
            store.close()


def test_quota_signal_parses_official_error_shape():
    """结构化额度信号（官方 codex-api/api_bridge.rs:192-226 的字段名）。

    文案判据只认固定英文串；`error.type` / `error.code` 才是权威判据，
    `resets_at` 给出精确重置时刻。
    """
    now = int(time.time() * 1000)
    future = time.time() + 7200
    body = json.dumps(
        {
            "error": {
                "type": "usage_limit_reached",
                "message": "usage limit reached",
                "plan_type": "pro",
                "resets_at": future,
                "limit_window_minutes": 300,
            }
        }
    )
    signal = classify.quota_signal(body, now)
    assert signal is not None
    assert signal.reset_at_ms == int(future * 1000)
    assert signal.window_minutes == 300 and signal.plan_type == "pro"
    # 普通限流不是额度信号
    assert classify.quota_signal('{"error":{"message":"Rate limit exceeded."}}', now) is None
    # 余额/积分耗尽（官方同段的 code 判据）：是额度类，但没有重置时刻
    credits = classify.quota_signal('{"error":{"code":"insufficient_quota"}}', now)
    assert credits is not None and credits.reset_at_ms is None
    # 已过期的 resets_at 视为没有（不拿陈旧时刻做判断）
    stale = classify.quota_signal('{"error":{"type":"usage_limit_reached","resets_at":1}}', now)
    assert stale is not None and stale.reset_at_ms is None
    # 非 JSON / 空体宽容返回 None
    assert classify.quota_signal("", now) is None
    assert classify.quota_signal("not json", now) is None


def test_quota_429_body_reset_locks_account_without_snapshot():
    """错误体自带 resets_at 时，账号还没有任何快照也要锁到重置时刻。

    这是新导入账号的唯一数据源：没有它，首次撞 429 只能按 2 小时软退避反复回池、
    反复撞同一条错误（退避上限远短于窗口长度）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")  # 注意：不带 rate_limit 快照
            provider = CodexProvider(store=store)
            resets_at = time.time() + 3 * 86_400
            body = json.dumps(
                {
                    "error": {
                        "type": "usage_limit_reached",
                        "message": "usage limit reached",
                        "plan_type": "plus",
                        "resets_at": resets_at,
                        "limit_window_minutes": 10080,
                    }
                }
            )
            provider._penalize(
                store.get_account("acc-1"),
                classify.Classified(kind=classify.RATE_LIMIT),
                body,
            )
            cooled = store.get_account("acc-1")
            assert cooled.cooldown_kind == "quota", cooled.cooldown_kind
            assert cooled.cooldown_until_ms == int(resets_at * 1000)
        finally:
            store.close()


def test_plain_rate_limit_429_still_uses_short_backoff():
    """普通限流（没有额度文案）仍走软冷却：别把瞬时 429 也锁几十天。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            reset_at = int(time.time() * 1000) + 30 * 86_400_000
            _seed_account(
                store, "acc-1",
                rate_limit={"primary": {"used_percent": 40.0, "reset_at_ms": reset_at}},
            )
            provider = CodexProvider(store=store)
            provider._penalize(
                store.get_account("acc-1"), classify.Classified(kind=classify.RATE_LIMIT),
                '{"error":{"message":"Rate limit exceeded. Please try again later."}}',
            )
            cooled = store.get_account("acc-1")
            assert cooled.cooldown_kind == "soft"
            assert cooled.cooldown_until_ms < reset_at, "普通限流不该被锁到窗口重置"
        finally:
            store.close()


def test_pool_skips_quota_locked_account():
    """锁定后号池不再选它，池里全锁时返回 None（而不是硬选一个去撞 429）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            reset_at = int(time.time() * 1000) + 30 * 86_400_000
            _seed_account(store, "locked", cooldown_until_ms=reset_at, cooldown_kind="quota")
            _seed_account(store, "alive")
            provider = CodexProvider(store=store)
            picked = {provider._pool.pick("sess-x").uid for _ in range(12)}
            assert picked == {"alive"}, f"选到了被额度锁住的号: {picked}"

            store.update_runtime("alive", cooldown_until_ms=reset_at, cooldown_kind="quota")
            assert provider._pool.pick("sess-x") is None, "全池额度用尽时应返回 None"
        finally:
            store.close()


def test_store_rate_limit_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")
            snapshot = {"primary": {"used_percent": 12.0, "window_minutes": 300.0},
                        "at_ms": 123}
            store.update_runtime("acc-1", rate_limit=snapshot, plan_type="plus")
            got = store.get_account("acc-1")
            assert got.rate_limit == snapshot
            assert got.plan_type == "plus"
            assert got.public_dict()["rate_limit"] == snapshot
            # 坏 JSON 回落成 {}，不抛
            store._conn.execute("UPDATE accounts SET rate_limit = 'not json' WHERE uid = 'acc-1'")
            store._conn.commit()
            assert store.get_account("acc-1").rate_limit == {}
        finally:
            store.close()


def test_refresh_expiring_only_touches_expiring_accounts():
    """后台巡检只刷「快过期」的号，且跳过不该刷的（照 codex2api 的取舍）。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            now = int(time.time() * 1000)
            hour = 3_600_000
            _seed_account(store, "soon", token_expires_ms=now + 60_000)
            _seed_account(store, "later", token_expires_ms=now + hour)
            _seed_account(store, "paused", token_expires_ms=now + 60_000, status="paused")
            _seed_account(store, "disabled", token_expires_ms=now + 60_000, status="disabled")
            _seed_account(store, "no-rt", token_expires_ms=now + 60_000, refresh_token="")
            _seed_account(store, "auth-cool", token_expires_ms=now + 60_000,
                          cooldown_until_ms=now + hour, cooldown_kind="auth")
            _seed_account(store, "soft-cool", token_expires_ms=now + 60_000,
                          cooldown_until_ms=now + hour, cooldown_kind="soft")

            provider = CodexProvider(store=store)
            calls: list[str] = []

            def fake_refresh(refresh_token):
                calls.append(refresh_token)
                return cx_oauth.TokenData(access_token="AT2", refresh_token="RT2",
                                          id_token="", expires_in=3600)

            original = cx_oauth.refresh
            cx_oauth.refresh = fake_refresh
            try:
                refreshed = provider.refresh_expiring()
            finally:
                cx_oauth.refresh = original

            # 只刷了 soon 与 soft-cool（限流冷却要刷，好让冷却结束时手里是新票）
            assert refreshed == 2
            assert sorted(calls) == ["rt", "rt"]
            assert store.get_account("soon").access_token == "AT2"
            assert store.get_account("later").access_token == "at"
            assert store.get_account("paused").access_token == "at"
            assert store.get_account("auth-cool").access_token == "at"
        finally:
            store.close()


def test_refresh_lock_serializes_and_rereads_db():
    """并发刷同一个号会被串行化；拿到锁后重读库，用最新 RT（防 RT 双花）。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", refresh_token="RT-old")
            provider = CodexProvider(store=store)
            seen: list[str] = []

            def fake_refresh(refresh_token):
                seen.append(refresh_token)
                return cx_oauth.TokenData(access_token="AT2", refresh_token="RT-new",
                                          id_token="", expires_in=3600)

            original = cx_oauth.refresh
            cx_oauth.refresh = fake_refresh
            try:
                account = store.get_account("acc-1")
                lock = provider._refresh_lock("acc-1")
                lock.acquire()
                # 锁被占：后台巡检路径直接跳过，不打上游
                assert provider._try_refresh(account, wait=False) is False
                assert seen == []
                lock.release()
                # 期间库里 RT 被换过 → 重读后用的是新值
                store.update_tokens("acc-1", "AT-x", "RT-newer", 0)
                assert provider._try_refresh(account, wait=False) is True
                assert seen == ["RT-newer"]
            finally:
                cx_oauth.refresh = original
        finally:
            store.close()


# ── token 刷新健壮性（失败分类 / 落库不可丢 / 请求用新票；取舍对照 codex2api）──


def test_oauth_refresh_failure_classification():
    """换票失败分两类：上游明确拒绝（终局）vs 结果不确定（网络/5xx/响应畸形）。

    判据只认错误体里的 code（白名单照 codex2api isNonRetryable）——CF/网页拦截
    那种没有 code 的 4xx 必须归到"不确定"，否则一次拦截抖动就会把好号判死。
    """
    import providers.codex.oauth as cx_oauth

    cases = [
        (400, '{"error":"invalid_grant"}', cx_oauth.TokenRejected),
        (400, '{"error":{"code":"refresh_token_reused"}}', cx_oauth.TokenRejected),
        (403, "<html>blocked by edge</html>", cx_oauth.RefreshUncertain),
        (500, "upstream boom", cx_oauth.RefreshUncertain),
        (200, "not json", cx_oauth.RefreshUncertain),
        (200, '{"token_type":"bearer"}', cx_oauth.RefreshUncertain),
    ]
    for status, body, expected in cases:
        def _respond(*_a, _status=status, _body=body, **_kw):
            return _TokenResp(_status, _body)

        with _patched(cx_oauth.crequests, "post", _respond):
            try:
                cx_oauth.refresh("RT-secret")
            except Exception as exc:
                assert isinstance(exc, expected), f"{status} {body}: {type(exc).__name__}"
            else:
                raise AssertionError(f"{status} {body} 应当抛 {expected.__name__}")

    def _transport_boom(*_a, **_kw):
        raise cx_oauth.crequests.RequestsError("curl 56 recv failure")

    with _patched(cx_oauth.crequests, "post", _transport_boom):
        try:
            cx_oauth.refresh("RT-secret")
        except cx_oauth.RefreshUncertain:
            pass
        else:
            raise AssertionError("传输失败应当抛 RefreshUncertain")


def test_oauth_error_message_redacts_credential():
    """上游把 refresh_token 原样回显在错误体里时，异常消息必须抹掉它。

    这些消息会进日志、也会回给前端（手动导入/授权兑换的报错），凭证不能跟着走。
    """
    import providers.codex.oauth as cx_oauth

    body = '{"error":"invalid_grant","refresh_token":"RT-secret","detail":"RT-secret 已失效"}'

    def _echo(*_a, **_kw):
        return _TokenResp(400, body)

    with _patched(cx_oauth.crequests, "post", _echo):
        try:
            cx_oauth.refresh("RT-secret")
        except Exception as exc:
            message = str(exc)
        else:
            raise AssertionError("应当抛异常")
    assert "RT-secret" not in message
    assert "[REDACTED]" in message and "invalid_grant" in message


def test_refresh_persist_retry_recovers():
    """换票成功后落库瞬时失败（库被锁/抖动）：重试一次就恢复，账号照常可用。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", refresh_token="RT-old",
                          token_expires_ms=int(time.time() * 1000) + 1_000)
            provider = CodexProvider(store=store)
            real_update = store.update_tokens
            calls: list[int] = []

            def flaky(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise sqlite3.OperationalError("database is locked")
                return real_update(*args, **kwargs)

            fresh_tokens = cx_oauth.TokenData(access_token="AT2", refresh_token="RT2",
                                              id_token="", expires_in=3600)
            with _patched(cx_oauth, "refresh", lambda _rt: fresh_tokens), \
                 _patched(store, "update_tokens", flaky):
                assert provider._try_refresh(store.get_account("acc-1"), wait=False) is True

            assert len(calls) == 2
            fresh = store.get_account("acc-1")
            assert (fresh.access_token, fresh.refresh_token) == ("AT2", "RT2")
            assert fresh.status == "active"
        finally:
            store.close()


def test_refresh_persist_failure_disables_and_stops_reusing_old_rt():
    """换票成功但落库彻底失败：停用该号，绝不再拿库里那张废票去换。

    这是真能把号玩死的一条路：上游已经把 RT 轮换走了，库里还是旧的；再拿旧票去换
    只会拿到 invalid_grant（还可能被上游判成重放）。取舍照 codex2api 的
    "新 Token 保存失败，已停止重复消费旧 RT"。
    """
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", refresh_token="RT-old",
                          token_expires_ms=int(time.time() * 1000) + 1_000)
            provider = CodexProvider(store=store)
            seen: list[str] = []
            persisted: list[int] = []

            def fake_refresh(refresh_token):
                seen.append(refresh_token)
                return cx_oauth.TokenData(access_token="AT2", refresh_token="RT2",
                                          id_token="", expires_in=3600)

            def broken(*_a, **_kw):
                persisted.append(1)
                raise sqlite3.OperationalError("disk I/O error")

            with _patched(cx_oauth, "refresh", fake_refresh), \
                 _patched(store, "update_tokens", broken):
                assert provider._try_refresh(store.get_account("acc-1"), wait=False) is False
                assert len(persisted) == 3        # 重试到上限就放弃
                assert provider.refresh_expiring() == 0

            assert seen == ["RT-old"]             # 旧票只被消费这一次
            dead = store.get_account("acc-1")
            assert dead.status == "disabled"
            assert "persist failed" in dead.status_reason
        finally:
            store.close()


def test_refresh_rejected_rt_disables_account():
    """上游明确拒绝这张 RT：直接停用待人工重新授权，不再按连败阶梯慢慢爬。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", refresh_token="RT-dead",
                          token_expires_ms=int(time.time() * 1000) + 1_000)
            provider = CodexProvider(store=store)
            calls: list[str] = []

            def rejected(refresh_token):
                calls.append(refresh_token)
                raise cx_oauth.TokenRejected("token 端点 HTTP 400 code=invalid_grant")

            with _patched(cx_oauth, "refresh", rejected):
                assert provider._try_refresh(store.get_account("acc-1"), wait=False) is False
                assert provider.refresh_expiring() == 0

            assert calls == ["RT-dead"]           # 停用后巡检不再拿死票去打上游
            dead = store.get_account("acc-1")
            assert dead.status == "disabled"
            assert "invalid_grant" in dead.status_reason
        finally:
            store.close()


def test_refresh_uncertain_failure_keeps_account():
    """结果不确定（超时/5xx）不判死：账号保持可用，下一轮拿同一张票再试。

    与"明确拒绝"分开的意义：一次网络抖动不该让好号被停用；真死了下一轮会拿到
    invalid_grant，自会收敛到停用。
    """
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", refresh_token="RT-maybe",
                          token_expires_ms=int(time.time() * 1000) + 1_000)
            provider = CodexProvider(store=store)
            attempts: list[str] = []

            def uncertain(refresh_token):
                attempts.append(refresh_token)
                raise cx_oauth.RefreshUncertain("token 端点传输失败: curl 28 timeout")

            with _patched(cx_oauth, "refresh", uncertain):
                assert provider._try_refresh(store.get_account("acc-1"), wait=False) is False

            alive = store.get_account("acc-1")
            assert alive.status == "active"
            assert alive.refresh_token == "RT-maybe"
            assert attempts == ["RT-maybe"]
        finally:
            store.close()


def test_patrol_isolates_unexpected_account_exception():
    """巡检里某个号抛意外异常，不该让排在它后面的号这轮全被跳过。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            soon = int(time.time() * 1000) + 1_000
            _seed_account(store, "acc-1", refresh_token="RT-1", token_expires_ms=soon)
            _seed_account(store, "acc-2", refresh_token="RT-2", token_expires_ms=soon)
            provider = CodexProvider(store=store)
            seen: list[str] = []

            def fake_refresh(refresh_token):
                seen.append(refresh_token)
                if refresh_token == "RT-1":
                    raise RuntimeError("意外的库异常")
                return cx_oauth.TokenData(access_token="AT2", refresh_token="RT2",
                                          id_token="", expires_in=3600)

            with _patched(cx_oauth, "refresh", fake_refresh):
                refreshed = provider.refresh_expiring()

            assert sorted(seen) == ["RT-1", "RT-2"]   # 两个号都被照看到
            assert refreshed == 1                     # 只有换出票的那个算数
            assert store.get_account("acc-2").access_token == "AT2"
        finally:
            store.close()


def test_request_uses_rotated_token_after_expiry_refresh():
    """请求路径刷新后必须用新 AT 发出去，不能拿刷新前那份过期快照去撞 401。

    旧行为：_maybe_refresh 刷完仍用调用方手里的旧 AT 发请求，白挨一个 401，之后还
    会被判 AUTH 再触发一次同号刷新——刚换出来的新票立刻被再轮换一次。
    """
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", access_token="AT-old", refresh_token="RT-old",
                          token_expires_ms=int(time.time() * 1000) + 30_000)
            provider = _provider_with_catalog(store, ["gpt-5-codex"])

            class _Resp:
                status_code = 200

                def __init__(self) -> None:
                    self.headers: dict[str, str] = {"content-type": "text/event-stream"}

                def iter_content(self, chunk_size: int = 8192):
                    yield b"data: {}\n\n"

                def close(self) -> None:
                    pass

            seen: list[str] = []

            def fake_request(account, body, session_id, turn_state=""):
                seen.append(account.access_token)
                return _Resp()

            fresh_tokens = cx_oauth.TokenData(access_token="AT-new", refresh_token="RT-new",
                                              id_token="", expires_in=3600)
            outbound = {"model": "gpt-5-codex", "input": [], "store": True}
            provider.shape(outbound, "responses")
            with _patched(cx_oauth, "refresh", lambda _rt: fresh_tokens), \
                 _patched(cx_upstream, "responses_request", fake_request):
                provider.send(
                    provider.route("cx:gpt-5-codex"),
                    "responses",
                    json.dumps(outbound).encode(),
                    RequestIDs(session="ses-1", request="r1", project="p"),
                    stream=True,
                )
            assert seen == ["AT-new"]
            persisted = store.get_account("acc-1")
            assert (persisted.access_token, persisted.refresh_token) == ("AT-new", "RT-new")
        finally:
            store.close()


def test_manual_add_reports_lost_credential_on_persist_failure():
    """手动导入：兑换成功但落库失败，必须说清"票已作废、请重新导出"。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            id_token = _make_id_token({
                "email": "a@b.c",
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-x"},
            })

            def broken(*_a, **_kw):
                raise sqlite3.OperationalError("disk I/O error")

            tokens = cx_oauth.TokenData(access_token="AT", refresh_token="RT2",
                                        id_token=id_token, expires_in=3600)
            with _patched(cx_oauth, "refresh", lambda _rt: tokens), \
                 _patched(store, "upsert_account", broken):
                try:
                    provider.manual_add_account({"refresh_token": "RT-old"})
                except ValueError as exc:
                    message = str(exc)
                else:
                    raise AssertionError("应当抛 ValueError")
            assert "重新导出" in message
        finally:
            store.close()


def test_manual_add_reports_rejected_rt_clearly():
    """手动导入撞上明确拒绝：报"已被上游拒绝"，而不是笼统的"校验失败"。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)

            def rejected(_refresh_token):
                raise cx_oauth.TokenRejected("HTTP 400 code=invalid_grant")

            with _patched(cx_oauth, "refresh", rejected):
                try:
                    provider.manual_add_account({"refresh_token": "RT-old"})
                except ValueError as exc:
                    message = str(exc)
                else:
                    raise AssertionError("应当抛 ValueError")
            assert "拒绝" in message and "重新授权" in message
        finally:
            store.close()


def test_login_exchange_reports_lost_authorization_on_persist_failure():
    """授权兑换成功但落库失败：报"授权码已作废、请重新发起"，别让用户反复贴同一段回调。"""
    import providers.codex.oauth as cx_oauth
    from providers.codex.login import LoginManager

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            manager = LoginManager(store)
            session = manager.start()
            id_token = _make_id_token({
                "email": "a@b.c",
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-y"},
            })

            def broken(*_a, **_kw):
                raise sqlite3.OperationalError("disk I/O error")

            tokens = cx_oauth.TokenData(access_token="AT", refresh_token="RT",
                                        id_token=id_token, expires_in=3600)
            with _patched(cx_oauth, "exchange_code", lambda _code, _verifier, _redirect="": tokens), \
                 _patched(store, "upsert_account", broken):
                try:
                    manager.exchange(
                        session["session_id"],
                        f"http://localhost:1455/auth/callback?code=abc&state={session['state']}",
                    )
                except RuntimeError as exc:
                    message = str(exc)
                else:
                    raise AssertionError("应当抛 RuntimeError")
            assert "重新发起授权" in message
            # 会话留在 pending 会让回调监听占着 1455 端口不放，标记掉让它自己释放
            manager.fail(session["state"], "测试收尾")
        finally:
            store.close()


def test_penalize_leaves_disabled_account_reason_intact():
    """已判死的号再吃 401：只留错误证据，不覆盖状态与理由、也不再写冷却/连败。

    请求路径判死之后本次请求仍会带着旧 AT 打出去，那个 401 会回到 _penalize——
    它必须让位给"要重新授权"这个更具体的原因，而不是把它改写成笼统的连败文案。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            # 连败到阈值-1：没有守卫的话，这次 401 正好够到阈值并覆盖 status_reason
            _seed_account(store, "acc-1", consecutive_failures=2)
            provider = CodexProvider(store=store)
            account = store.get_account("acc-1")
            provider._mark_needs_reauth(account, "refresh_token rejected: invalid_grant")
            provider._penalize(
                account, classify.Classified(kind=classify.AUTH), "401 unauthorized"
            )
            after = store.get_account("acc-1")
            assert after.status == "disabled"
            assert "invalid_grant" in after.status_reason
            assert after.consecutive_failures == 2 and after.cooldown_until_ms == 0
            assert after.cooldown_kind == ""
            assert "401" in after.last_error
        finally:
            store.close()


def test_export_cpa_shape():
    """导出为 CPA(CLIProxyAPI) 的单账号 auth 形状；额度字段有才带。"""
    from providers.codex import transfer

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", label="me@x.y", email="me@x.y", plan_type="plus",
                          account_id="acc-1", id_token="IDT", access_token="AT",
                          refresh_token="RT", token_expires_ms=1_800_000_000_000)
            store.update_runtime("acc-1", rate_limit={
                "primary": {"used_percent": 37.0, "window_minutes": 300.0,
                            "reset_at_ms": 1_800_000_600_000},
                "secondary": {"used_percent": 8.0, "window_minutes": 10080.0},
                "at_ms": 1_800_000_000_000,
            })
            account = store.get_account("acc-1")

            entries = transfer.export([account], "cpa")
            assert isinstance(entries, list) and len(entries) == 1
            entry = entries[0]
            assert entry["type"] == "codex"
            assert entry["email"] == "me@x.y"
            assert entry["account_id"] == "acc-1"
            assert entry["refresh_token"] == "RT"
            assert entry["access_token"] == "AT"
            assert entry["expired"] == "2027-01-15T08:00:00Z"   # RFC3339 UTC
            assert entry["codex_5h_used_percent"] == "37"
            assert entry["codex_7d_used_percent"] == "8"
            assert entry["codex_5h_reset_at"].endswith("Z")
            # 没有额度快照的账号：这些键不出现（omitempty 同款）
            bare = transfer.export([Account(uid="u", email="a@b.c", refresh_token="rt")], "cpa")
            assert "codex_5h_used_percent" not in bare[0]
            # codex2api = 带 exported_at 的批量封装
            wrapped = transfer.export([account], "codex2api")
            assert wrapped["exported_at"].endswith("Z")
            assert wrapped["accounts"][0]["refresh_token"] == "RT"
        finally:
            store.close()


def test_parse_entries_accepts_all_shapes():
    """导入宽容：单对象 / 扁平数组 / {"accounts":[...]} / 再嵌一层的字符串 JSON。"""
    from providers.codex import transfer

    one = '{"refresh_token":"rt-1","email":"one@x.y"}'
    assert transfer.parse_entries(one)[0]["email"] == "one@x.y"

    array = '[{"refresh_token":"rt-1"},{"access_token":"at-2","account_id":"a2"}]'
    assert len(transfer.parse_entries(array)) == 2

    wrapped = '{"exported_at":"2026-04-03T14:49:53Z","proxies":[],"accounts":[{"refresh_token":"rt-3"}]}'
    assert transfer.parse_entries(wrapped)[0]["refresh_token"] == "rt-3"

    nested = json.dumps(json.dumps({"refresh_token": "rt-4"}))
    assert transfer.parse_entries(nested)[0]["refresh_token"] == "rt-4"

    for bad in ("", "   ", "not json", '{"hello":"world"}', "[]", "42"):
        try:
            transfer.parse_entries(bad)
            raise AssertionError(f"should have raised for {bad!r}")
        except ValueError:
            pass


def test_expires_field_parsing():
    from providers.codex import transfer

    assert transfer._expires_ms("2026-11-09T12:00:00Z") == 1_794_225_600_000
    assert transfer._expires_ms(1_793_016_000) == 1_793_016_000_000       # epoch 秒
    assert transfer._expires_ms(1_793_016_000_000) == 1_793_016_000_000   # epoch 毫秒
    assert transfer._expires_ms("1793016000") == 1_793_016_000_000        # 字符串数字
    assert transfer._expires_ms("nonsense") == 0
    assert transfer._expires_ms(None) == 0
    assert transfer._expires_ms(True) == 0


def test_import_roundtrip_and_skips_bad_entries():
    """导出 → 导入应还原账号；坏条目只计入 skipped，不中断整批。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", label="me@x.y", email="me@x.y", plan_type="pro",
                          account_id="acc-1", refresh_token="RT-1",
                          token_expires_ms=1_800_000_000_000)
            provider = CodexProvider(store=store)
            exported = provider.export_accounts("codex2api")

            # 清空后重新导入
            store.delete_account("acc-1")
            payload = json.dumps(exported)
            summary = provider.import_accounts(payload)
            assert summary["imported"] == 1 and summary["updated"] == 0
            restored = store.get_account("acc-1")
            assert restored.email == "me@x.y"
            assert restored.refresh_token == "RT-1"
            assert restored.plan_type == "pro"
            assert restored.token_expires_ms == 1_800_000_000_000

            # 再导一次 = 更新（不是新增）
            assert provider.import_accounts(payload)["updated"] == 1

            # 坏条目：缺凭据 / 缺标识 / 同批次重复
            mixed = json.dumps([
                {"email": "no-creds@x.y"},
                {"refresh_token": "rt"},
                {"refresh_token": "rt-2", "account_id": "a2"},
                {"refresh_token": "rt-2", "account_id": "a2"},
            ])
            summary = provider.import_accounts(mixed)
            assert summary["total"] == 4
            assert summary["imported"] == 1
            assert summary["skipped"] == 3
            assert len(summary["errors"]) == 3
        finally:
            store.close()


# ── 登录会话（本机回调监听 + 轮询）──


def test_login_poll_and_cancel_lifecycle():
    """会话生命周期：start → pending；cancel → expired；重复 cancel 幂等。"""
    import providers.codex.login as cx_login

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            manager = cx_login.LoginManager(store, callback_port=0)
            started = manager.start()
            assert started["listening"] is True
            assert started["auth_url"].startswith("https://auth.openai.com/oauth/authorize?")
            assert manager.poll(started["state"])["status"] == "pending"
            assert manager.poll("")["status"] == "expired"
            assert manager.poll("no-such-state")["status"] == "expired"
            assert manager.cancel(started["session_id"]) is True
            assert manager.poll(started["state"])["status"] == "expired"
            assert manager.cancel(started["session_id"]) is False
            manager.cancel("no-such-session")
            # 没有未完成会话 → 监听释放（端口还给 Codex CLI）
            assert manager.callback_endpoint() is None
        finally:
            store.close()


def test_login_callback_listener_completes_login():
    """本机监听端到端：浏览器打回调 URL → 自动兑换入库 → poll 转 done。"""
    import urllib.error
    import urllib.request

    import providers.codex.login as cx_login
    import providers.codex.oauth as cx_oauth

    token = _make_id_token(
        {"email": "a@b.co", "chatgpt_account_id": "acc-xyz", "plan_type": "plus"}
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            manager = cx_login.LoginManager(store, callback_port=0)
            started = manager.start()
            endpoint = manager.callback_endpoint()
            assert endpoint is not None
            host, port = endpoint

            original = cx_oauth.exchange_code
            cx_oauth.exchange_code = lambda code, verifier, redirect: cx_oauth.TokenData(
                access_token="AT", refresh_token="RT", id_token=token, expires_in=3600,
            )
            try:
                # state 不匹配：拒绝回调，且不消费会话（可继续等正确回调）
                try:
                    urllib.request.urlopen(
                        f"http://{host}:{port}/auth/callback?code=abc&state=wrong", timeout=5
                    )
                    raise AssertionError("state 不匹配应回 400")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 400
                assert manager.poll(started["state"])["status"] == "pending"

                # 正确回调：兑换入库，页面提示已入池
                with urllib.request.urlopen(
                    f"http://{host}:{port}/auth/callback?code=abc&state={started['state']}",
                    timeout=5,
                ) as response:
                    body = response.read().decode("utf-8")
                assert "已入池" in body
                done = manager.poll(started["state"])
                assert done["status"] == "done"
                assert done["result"]["uid"] == "acc-xyz"
                account = store.get_account("acc-xyz")
                assert account is not None
                assert account.refresh_token == "RT" and account.access_token == "AT"

                # 完成的会话不能再兑换（粘贴路径同样被挡住）
                try:
                    manager.exchange(started["session_id"], "code2")
                    raise AssertionError("已完成会话应拒绝重复兑换")
                except ValueError as exc:
                    assert "已完成" in str(exc)
            finally:
                cx_oauth.exchange_code = original

            manager.cancel(started["session_id"])
            assert manager.callback_endpoint() is None
        finally:
            store.close()


def test_note_rate_limit_syncs_plan_when_usage_flat():
    """用量没动但响应头带了新档位 → 仍要同步 plan_type。

    回归点：早退条件只看 used_percent，用量持平时会把档位变化一起吞掉，
    plan_type 永远停在旧值（响应头是档位的权威来源）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1", plan_type="plus")
            store.update_runtime(
                "acc-1",
                rate_limit={"primary": {"used_percent": 37.0, "window_minutes": 300.0}},
            )
            provider = CodexProvider(store=store)
            provider._note_rate_limit(store.get_account("acc-1"), {
                "x-codex-primary-used-percent": "37",
                "x-codex-primary-window-minutes": "300",
                "x-codex-plan-type": "pro",
            })
            assert store.get_account("acc-1").plan_type == "pro"
        finally:
            store.close()


def test_penalize_auth_counts_from_fresh_row():
    """AUTH 连败以库里的最新值为基准累加（并发下传入的 account 快照可能过期）。

    回归点：RATE_LIMIT 分支读库里最新值、AUTH 分支用快照值，同函数两种口径；
    并发下 AUTH 会少算连败、够不到熔断阈值。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            _seed_account(store, "acc-1")
            store.update_runtime("acc-1", consecutive_failures=2)
            provider = CodexProvider(store=store)
            # 过期快照（连败 0）：应从库里的 2 续到 3，而不是从 0 记成 1
            stale = Account(uid="acc-1", access_token="at", refresh_token="rt")
            provider._penalize(stale, classify.classify(401, "unauthorized"), "unauthorized")
            assert store.get_account("acc-1").consecutive_failures == 3
        finally:
            store.close()


def test_zero_length_windows_are_placeholders_not_windows():
    """长度非正的窗口 = "没有这个窗口"的占位写法，不能当窗口收下。

    回归点：上游在没有第二窗口时会发一组全 0 的 secondary（响应头与 /usage 都一样）。
    照单全收会让界面渲染出「额度 剩余 100%」，与主窗口的「已用尽」并排、自相矛盾。
    """
    from providers.codex import upstream

    by_headers = upstream.parse_rate_limits({
        "x-codex-primary-used-percent": "100",
        "x-codex-primary-window-minutes": "43200",
        "x-codex-primary-reset-after-seconds": "1986178",
        "x-codex-secondary-used-percent": "0",
        "x-codex-secondary-window-minutes": "0",
    })
    assert by_headers is not None and "primary" in by_headers
    assert "secondary" not in by_headers

    by_usage = upstream.parse_usage_payload(
        {
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 100,
                    "limit_window_seconds": 2592000,
                    "reset_after_seconds": 1986178,
                },
                "secondary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 0,
                    "reset_after_seconds": 0,
                },
            },
        },
        now_ms=1_000_000,
    )
    assert by_usage is not None and "primary" in by_usage
    assert "secondary" not in by_usage


# ── 内建运行入口（无 pytest 环境直接跑）──

_ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    failed = 0
    for test in _ALL:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_ALL) - failed}/{len(_ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
