"""codex provider 单元测试（OAuth / 塑形 / 头族 / 分类 / 池 / 端到端 send）。

运行：cd server && uv run python tests/test_codex.py
（有 pytest 时也可 uv run python -m pytest tests/test_codex.py -q）
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.settings import CODEX_CLIENT_ID, CODEX_ORIGINATOR, CODEX_REDIRECT_URI
from gateway.base import RequestIDs
from providers.codex import classify, oauth, shape
from providers.codex import upstream as cx_upstream
from providers.codex.pool import Pool
from providers.codex.provider import CodexProvider, _Catalog
from providers.codex.store import AUTH_MODE_SIWC, Account, CodexStore

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


def test_shape_injects_session_cache_key():
    body = {"model": "m", "input": []}
    assert shape.shape_codex_body(body, "ses-1") is True
    assert body["prompt_cache_key"] == "ses-1"
    assert shape.shape_codex_body(body, "ses-1") is False


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
            assert store.next_used_seq() == 1
            store.update_runtime("acc-1", used_seq=store.next_used_seq())
            assert store.next_used_seq() == 2
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


def test_send_injects_session_and_passes_through():
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
                # 真实链路：inference 先调 shape()（静态归一），再调 send()（会话相关注入）
                outbound = {"model": "gpt-5-codex", "input": [], "store": True}
                provider.shape(outbound, "responses")
                resp = provider.send(
                    provider.route("cx:gpt-5-codex"),
                    "responses",
                    json.dumps(outbound).encode(),
                    RequestIDs(session="ses-42", request="r1", project="p"),
                    stream=True,
                )
            finally:
                cx_upstream.responses_request = original

            assert resp.status == 200
            assert resp.account == "acc-1"
            assert seen["session"] == "ses-42"
            body = seen["body"]
            assert body["prompt_cache_key"] == "ses-42"
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

            def fake_refresh(refresh_token, *, timeout_s=None):
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

            def fake_refresh(refresh_token, *, timeout_s=None):
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
    """导出 → 导入应还原账号；坏条目只计入 skipped，不中断整批。

    这里 claim=False：本用例只验证形状与落库，校验+认领另有专门用例（且会打上游）。
    """
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
            summary = provider.import_accounts(payload, claim=False)
            assert summary["imported"] == 1 and summary["updated"] == 0
            restored = store.get_account("acc-1")
            assert restored.email == "me@x.y"
            assert restored.refresh_token == "RT-1"
            assert restored.plan_type == "pro"
            assert restored.token_expires_ms == 1_800_000_000_000

            # 再导一次 = 更新（不是新增）
            assert provider.import_accounts(payload, claim=False)["updated"] == 1

            # 坏条目：缺凭据 / 缺标识 / 同批次重复
            mixed = json.dumps([
                {"email": "no-creds@x.y"},
                {"refresh_token": "rt"},
                {"refresh_token": "rt-2", "account_id": "a2"},
                {"refresh_token": "rt-2", "account_id": "a2"},
            ])
            summary = provider.import_accounts(mixed, claim=False)
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


# ── SIWC（Sign in with ChatGPT，CPA auth 文件的 use_siwc 分区）──


def test_siwc_entry_prefers_nested_credentials():
    """凭据取嵌套 siwc 分区（实际在用的那套），身份/额度仍取外层。"""
    from providers.codex import transfer

    entry = {
        "type": "codex", "email": "s@x.y", "plan_type": "plus", "account_id": "acc-s",
        "access_token": "OUT-AT", "refresh_token": "OUT-RT", "id_token": "OUT-ID",
        "expired": "2027-01-15T08:00:00Z",
        "use_siwc": True,
        "siwc": {
            "access_token": "SIWC-AT", "refresh_token": "SIWC-RT", "id_token": "SIWC-ID",
            "client_id": "dynamic_agent_client", "device_id": "dev-1",
            "expired": "2027-02-01T00:00:00Z",
            "scope": "openid profile email chatgpt.tokens.use.direct",
        },
    }
    account, notes = transfer.to_account(entry)
    assert notes == []
    assert account.is_siwc
    assert account.access_token == "SIWC-AT" and account.refresh_token == "SIWC-RT"
    assert account.id_token == "SIWC-ID"
    assert account.oauth_client_id == "dynamic_agent_client"
    assert account.oauth_device_id == "dev-1"
    assert account.email == "s@x.y" and account.account_id == "acc-s"
    assert account.plan_type == "plus"
    assert account.token_expires_ms == transfer._expires_ms("2027-02-01T00:00:00Z")

    # use_siwc 为真但分区里没有凭据 → 回退外层，留备注
    outer_only, notes = transfer.to_account(
        {"refresh_token": "RT", "account_id": "a", "use_siwc": True}
    )
    assert not outer_only.is_siwc and outer_only.refresh_token == "RT"
    assert notes and "回退" in notes[0]

    # 分区有凭据但 use_siwc 不为真 → 以凭据为准，同样留备注
    by_creds, notes = transfer.to_account(
        {"refresh_token": "OUT-RT", "account_id": "a", "siwc": {"refresh_token": "S-RT"}}
    )
    assert by_creds.is_siwc and by_creds.refresh_token == "S-RT" and notes

    # 少数导出把嵌套块塞成 JSON 字符串
    as_text, _ = transfer.to_account(
        {"refresh_token": "OUT-RT", "account_id": "a", "use_siwc": True,
         "siwc": json.dumps({"refresh_token": "S-RT", "client_id": "c"})}
    )
    assert as_text.is_siwc and as_text.refresh_token == "S-RT"


def test_siwc_export_roundtrip():
    """SIWC 账号导出：use_siwc + 嵌套分区 + 外层镜像；再导入回来仍是 SIWC。"""
    from providers.codex import transfer

    account = Account(
        uid="acc-s", account_id="acc-s", email="s@x.y", access_token="AT",
        refresh_token="RT", id_token="ID", token_expires_ms=1_800_000_000_000,
        auth_mode=AUTH_MODE_SIWC, oauth_client_id="dynamic_agent_client",
        oauth_device_id="dev-1", updated_ms=1_800_000_000_000,
    )
    entry = transfer.export([account], "cpa")[0]
    assert entry["use_siwc"] is True
    assert entry["siwc"]["refresh_token"] == "RT"
    assert entry["siwc"]["client_id"] == "dynamic_agent_client"
    assert entry["siwc"]["device_id"] == "dev-1"
    assert entry["siwc"]["expired"] == "2027-01-15T08:00:00Z"
    assert entry["refresh_token"] == "RT"      # 外层镜像同一套票
    back, notes = transfer.to_account(entry)
    assert notes == []
    assert back.is_siwc and back.oauth_client_id == "dynamic_agent_client"
    assert back.oauth_device_id == "dev-1" and back.refresh_token == "RT"
    # 外层账号不带这些键（omitempty 同款）
    plain = transfer.export([Account(uid="u", refresh_token="rt")], "cpa")[0]
    assert "use_siwc" not in plain and "siwc" not in plain


def test_siwc_refresh_uses_own_endpoint_and_resource():
    """SIWC 刷新走另一套端点，且必须带 resource（受众）：缺了票在 api.openai.com 不认。"""
    import providers.codex.oauth as cx_oauth

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = json.dumps({
            "access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600,
            "scope": "openid profile email chatgpt.tokens.use.direct",
        })

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs["data"]
        return FakeResponse()

    original = cx_oauth.crequests.post
    cx_oauth.crequests.post = fake_post
    try:
        tokens = cx_oauth.refresh_siwc("RT", "cli-1")
    finally:
        cx_oauth.crequests.post = original

    assert captured["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    form = parse_qs(captured["data"])
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_id"] == ["cli-1"]
    assert form["refresh_token"] == ["RT"]
    assert form["resource"] == ["https://api.openai.com/v1"]
    assert tokens.access_token == "AT2" and tokens.refresh_token == "RT2"
    assert tokens.scope.endswith("chatgpt.tokens.use.direct")

    # 缺 client_id 本地就拒（上游只认签发时那个）
    try:
        cx_oauth.refresh_siwc("RT", "")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_siwc_headers_drop_chatgpt_specific_fields():
    """SIWC 账号：不带 ChatGPT 后端专用头，会话头换成 session_id，域换到 api.openai.com。"""
    from providers.codex import upstream

    account = Account(uid="acc-s", account_id="acc-s", access_token="AT",
                      auth_mode=AUTH_MODE_SIWC, oauth_client_id="c")
    headers = upstream.codex_headers(account, "sess-1")
    assert "Chatgpt-Account-Id" not in headers
    assert "Originator" not in headers and "Version" not in headers
    assert "x-codex-beta-features" not in headers
    assert headers["session_id"] == "sess-1"
    assert headers["x-client-request-id"] == "sess-1"
    assert "session-id" not in headers
    assert headers["Authorization"] == "Bearer AT"
    assert upstream.base_url(account) == "https://api.openai.com/v1"

    outer = Account(uid="acc-o", account_id="acc-o", access_token="AT")
    assert upstream.base_url(outer).endswith("chatgpt.com/backend-api/codex")
    oh = upstream.codex_headers(outer, "sess-2")
    assert oh["Chatgpt-Account-Id"] == "acc-o" and oh["Originator"] == CODEX_ORIGINATOR


def test_siwc_models_parsed_from_standard_list():
    """SIWC 的目录来自 api.openai.com/v1/models（标准 list 形状，无 client_version）。"""
    from providers.codex import upstream

    account = Account(uid="acc-s", access_token="AT", auth_mode=AUTH_MODE_SIWC,
                      oauth_client_id="c")
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"object": "list", "data": [
                {"id": "gpt-5.1", "object": "model"},
                {"id": "", "object": "model"},
                {"object": "model"},
                {"id": "text-embedding-3-small", "object": "model"},
                {"id": "gpt-4o-mini-tts", "object": "model"},
                {"id": "gpt-4o-realtime-preview", "object": "model"},
                {"id": "dall-e-3", "object": "model"},
            ]}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse()

    original = upstream.crequests.get
    upstream.crequests.get = fake_get
    try:
        entries = upstream.fetch_models(account)
    finally:
        upstream.crequests.get = original

    assert captured["url"] == "https://api.openai.com/v1/models"
    # 只留对话模型：embeddings / 语音 / realtime / 图像不进目录（它们打 /responses 必错）
    assert entries == [{"id": "gpt-5.1", "name": "gpt-5.1"}]


def test_trim_siwc_body_drops_unsupported_fields():
    """SIWC 的 API 侧规则与外层相反：空的 instructions 要删，采样/长度参数不收。"""
    from providers.codex import shape

    body = {"model": "m", "instructions": "", "max_output_tokens": 100, "temperature": 0.5,
            "max_completion_tokens": 10, "prompt_cache_options": {"x": 1}, "input": []}
    assert shape.trim_siwc_body(body) is True
    for key in ("instructions", "max_output_tokens", "temperature",
                "max_completion_tokens", "prompt_cache_options"):
        assert key not in body
    assert body["input"] == []

    # 真 instructions 留着；无可裁剪时返回 False
    keep = {"instructions": "be nice"}
    assert shape.trim_siwc_body(keep) is False
    assert keep["instructions"] == "be nice"


def test_store_migrates_siwc_columns():
    """老库（缺 SIWC 三列）打开时补列：存量账号按外层凭证看待。"""
    import sqlite3

    from providers.codex.store import _DDL

    keys = ("auth_mode", "oauth_client_id", "oauth_device_id")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "codex.db"
        old_ddl = "\n".join(
            line for line in _DDL.splitlines() if not any(key in line for key in keys)
        )
        con = sqlite3.connect(path)
        con.executescript(old_ddl.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE"))
        con.execute("INSERT INTO accounts (uid, created_ms, updated_ms) VALUES ('old', 1, 1)")
        con.commit()
        con.close()

        store = CodexStore(path)
        try:
            account = store.get_account("old")
            assert account.auth_mode == "" and account.oauth_client_id == ""
            assert account.oauth_device_id == "" and not account.is_siwc
            # 新列可写可读
            store.update_runtime("old", plan_type="plus")     # 不碰新列
            store.upsert_account(Account(uid="old", account_id="old", auth_mode=AUTH_MODE_SIWC,
                                         oauth_client_id="cli-1", oauth_device_id="dev-1",
                                         access_token="AT", refresh_token="RT"))
            again = store.get_account("old")
            assert again.is_siwc and again.oauth_client_id == "cli-1"
        finally:
            store.close()


# ── 导入时的「校验+认领」──


def test_import_claims_refresh_token_by_default():
    """默认导入会用 RT 换一次新票：库里换成新票，摘要记 claimed。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            seen: list[str] = []

            def fake_refresh(refresh_token, *, timeout_s=None):
                seen.append(refresh_token)
                return cx_oauth.TokenData(
                    access_token="AT-NEW", refresh_token="RT-NEW", expires_in=3600,
                    id_token=_make_id_token({
                        "email": "a@b.c",
                        "https://api.openai.com/auth": {"chatgpt_account_id": "a"},
                    }),
                )

            original = cx_oauth.refresh
            cx_oauth.refresh = fake_refresh
            try:
                summary = provider.import_accounts(json.dumps([{
                    "refresh_token": "RT-OLD", "access_token": "AT-OLD", "account_id": "a",
                    "expired": "2030-01-01T00:00:00Z",
                }]))
            finally:
                cx_oauth.refresh = original

            assert seen == ["RT-OLD"]
            assert summary["imported"] == 1 and summary["claimed"] == 1
            assert summary["unverified"] == 0 and summary["warnings"] == []
            stored = store.get_account("a")
            assert stored.access_token == "AT-NEW" and stored.refresh_token == "RT-NEW"
        finally:
            store.close()


def test_import_keeps_account_when_refresh_token_rejected():
    """RT 已被上游作废但 AT 未过期：号照常入库（能用到到期），摘要标 unverified。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)

            def fake_refresh(refresh_token, *, timeout_s=None):
                raise cx_oauth.TokenRejected(401, json.dumps({
                    "error": {"message": "Invalid refresh token.", "code": "invalid_refresh_token"}
                }))

            original = cx_oauth.refresh
            cx_oauth.refresh = fake_refresh
            try:
                summary = provider.import_accounts(json.dumps([{
                    "refresh_token": "RT-DEAD", "access_token": "AT", "account_id": "a",
                    "expired": "2030-01-01T00:00:00Z",
                }]))
            finally:
                cx_oauth.refresh = original

            assert summary["imported"] == 1 and summary["skipped"] == 0
            assert summary["claimed"] == 0 and summary["unverified"] == 1
            assert "invalid_refresh_token" in summary["warnings"][0]
            assert store.get_account("a").access_token == "AT"
        finally:
            store.close()


def test_import_skips_entry_without_live_credential():
    """RT 被拒 + AT 也过期/缺失：跳过（导进来只是占位，还要重新授权）。"""
    import providers.codex.oauth as cx_oauth

    def rejected(refresh_token, *, timeout_s=None):
        raise cx_oauth.TokenRejected(401, json.dumps({"error": {"code": "invalid_refresh_token"}}))

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            original = cx_oauth.refresh
            cx_oauth.refresh = rejected
            try:
                summary = provider.import_accounts(json.dumps([
                    {"refresh_token": "RT-DEAD", "access_token": "AT", "account_id": "expired",
                     "expired": "2000-01-01T00:00:00Z"},
                    {"refresh_token": "RT-DEAD2", "account_id": "no-at"},
                ]))
            finally:
                cx_oauth.refresh = original

            assert summary["skipped"] == 2 and summary["imported"] == 0
            assert store.get_account("expired") is None
            assert len(summary["errors"]) == 2
        finally:
            store.close()


def test_import_keeps_account_on_transport_failure():
    """传输失败不算"凭证已死"：号照常入库，只提示校验未完成（留给巡检/请求路径重试）。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)

            def flaky(refresh_token, *, timeout_s=None):
                raise RuntimeError("token 端点 HTTP 502: bad gateway")

            original = cx_oauth.refresh
            cx_oauth.refresh = flaky
            try:
                summary = provider.import_accounts(json.dumps([{
                    "refresh_token": "RT", "access_token": "AT", "account_id": "a",
                    "expired": "2030-01-01T00:00:00Z",
                }]))
            finally:
                cx_oauth.refresh = original

            assert summary["imported"] == 1 and summary["skipped"] == 0
            assert summary["unverified"] == 1
            assert "校验未完成" in summary["warnings"][0]
            assert store.get_account("a") is not None
        finally:
            store.close()


def test_import_claim_can_be_disabled():
    """claim=False：完全不做校验、不与源系统抢票，凭证照原样入库。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            calls: list[str] = []

            def should_not_run(refresh_token, *, timeout_s=None):
                calls.append(refresh_token)
                raise AssertionError("claim 关掉后不该打上游")

            original = cx_oauth.refresh
            cx_oauth.refresh = should_not_run
            try:
                summary = provider.import_accounts(json.dumps([{
                    "refresh_token": "RT", "access_token": "AT", "account_id": "a",
                    "expired": "2000-01-01T00:00:00Z",   # 已过期也照收
                }]), claim=False)
            finally:
                cx_oauth.refresh = original

            assert calls == []
            assert summary["imported"] == 1 and summary["skipped"] == 0
            assert summary["claimed"] == 0 and summary["unverified"] == 0
            assert store.get_account("a").refresh_token == "RT"
        finally:
            store.close()


def test_import_siwc_account_claims_via_siwc_endpoint():
    """SIWC 条目：认领走 SIWC 端点，client_id 取文件里的，形态住进库里。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            seen: dict = {}

            def fake_siwc(refresh_token, client_id, *, timeout_s=None):
                seen["rt"] = refresh_token
                seen["client"] = client_id
                return cx_oauth.TokenData(
                    access_token="AT-S", refresh_token="RT-S", expires_in=600,
                    scope="openid profile email chatgpt.tokens.use.direct",
                )

            def outer_not_used(refresh_token, *, timeout_s=None):
                raise AssertionError("SIWC 账号不该走外层刷新")

            original_siwc, original_outer = cx_oauth.refresh_siwc, cx_oauth.refresh
            cx_oauth.refresh_siwc = fake_siwc
            cx_oauth.refresh = outer_not_used
            try:
                summary = provider.import_accounts(json.dumps([{
                    "type": "codex", "email": "s@x.y", "account_id": "acc-s",
                    "access_token": "OUT-AT", "refresh_token": "OUT-RT",
                    "expired": "2027-01-15T08:00:00Z", "use_siwc": True,
                    "siwc": {"access_token": "S-AT", "refresh_token": "S-RT",
                             "client_id": "cli-9", "device_id": "dev-9"},
                }]))
            finally:
                cx_oauth.refresh_siwc, cx_oauth.refresh = original_siwc, original_outer

            assert seen == {"rt": "S-RT", "client": "cli-9"}
            assert summary["claimed"] == 1 and summary["unverified"] == 0
            stored = store.get_account("acc-s")
            assert stored.is_siwc and stored.oauth_client_id == "cli-9"
            assert stored.oauth_device_id == "dev-9"
            assert stored.access_token == "AT-S" and stored.refresh_token == "RT-S"
            # API 可见视图带上形态；SIWC 的 AT 是短票，提前量按 5 分钟
            assert stored.public_dict()["auth_mode"] == AUTH_MODE_SIWC
            assert stored.refresh_ahead_ms(600) == 300_000
        finally:
            store.close()


def test_import_keeps_live_stored_credentials_over_dead_file_token():
    """重导一份旧导出：文件里的 RT 已死而库里那份还活着 → 保留库里的，不用死票覆盖。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            future = int(time.time() * 1000) + 86_400_000
            _seed_account(store, "a", refresh_token="RT-LIVE", access_token="AT-LIVE",
                          token_expires_ms=future)
            provider = CodexProvider(store=store)

            def rejected(refresh_token, *, timeout_s=None):
                raise cx_oauth.TokenRejected(401, json.dumps({
                    "error": {"code": "invalid_refresh_token"}
                }))

            payload = json.dumps([{
                "refresh_token": "RT-STALE", "access_token": "AT-STALE", "account_id": "a",
                "expired": "2030-01-01T00:00:00Z",
            }])
            original = cx_oauth.refresh
            cx_oauth.refresh = rejected
            try:
                summary = provider.import_accounts(payload)
            finally:
                cx_oauth.refresh = original

            assert summary["updated"] == 1 and summary["claimed"] == 0
            assert summary["unverified"] == 0
            assert "保留库里的" in summary["warnings"][0]
            kept = store.get_account("a")
            assert kept.refresh_token == "RT-LIVE" and kept.access_token == "AT-LIVE"

            # 库里那份的 AT 也过期时，没有"库里更活"的证据，照旧按文件落库并提示
            store.update_tokens("a", "AT-OLD", "RT-OLD", 1)
            original = cx_oauth.refresh
            cx_oauth.refresh = rejected
            try:
                summary = provider.import_accounts(payload)
            finally:
                cx_oauth.refresh = original
            assert summary["unverified"] == 1
            assert store.get_account("a").refresh_token == "RT-STALE"
        finally:
            store.close()


def test_import_skips_batch_duplicate_after_claim_reassigns_uid():
    """认领后归属变了（凭据属于另一个账号）时，批内重复要在落库前拦住。"""
    import providers.codex.oauth as cx_oauth

    with tempfile.TemporaryDirectory() as tmp:
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)

            def fake_refresh(refresh_token, *, timeout_s=None):
                return cx_oauth.TokenData(
                    access_token="AT-NEW", refresh_token="RT-NEW", expires_in=3600,
                    id_token=_make_id_token({
                        "https://api.openai.com/auth": {"chatgpt_account_id": "real"},
                    }),
                )

            original = cx_oauth.refresh
            cx_oauth.refresh = fake_refresh
            try:
                summary = provider.import_accounts(json.dumps([
                    {"refresh_token": "RT-1", "access_token": "AT", "account_id": "file-1",
                     "expired": "2030-01-01T00:00:00Z"},
                    {"refresh_token": "RT-2", "access_token": "AT", "account_id": "file-2",
                     "expired": "2030-01-01T00:00:00Z"},
                ]))
            finally:
                cx_oauth.refresh = original

            # 两条都认领成 real：第一条入库，第二条按重复跳过
            assert summary["imported"] == 1 and summary["skipped"] == 1
            assert store.get_account("real") is not None
            assert "重复" in summary["errors"][0]
        finally:
            store.close()


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
