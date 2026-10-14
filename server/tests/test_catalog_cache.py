"""模型目录缓存：快照写入护栏 + 三家「缓存 → 内存重建」等价性。

背景：模型目录不再自动刷新（只在手动「获取模型」时拉上游），启动时内存目录只能由
data/models.json 快照重建——而内存目录是 route() 的判据，重建不全 = 模型不可用。
所以这里盯三件事：

  ① 快照写入护栏：内容没变不写盘、内容变了先轮转 .bak、meta 只增不改内容时间；
  ② 三家从快照重建后的 route()/list_models() 与刷新后一致（workbuddy 的倍率/活动、
     opencode 的协议与凭证可见清单都要在）；
  ③ 上游返回空目录时不覆盖旧目录（一次抽风不该让网关"没有任何模型"）。

全程不碰网络：上游拉取与 models.dev 兜底都换成桩/断网。

运行：cd server && uv run python tests/test_catalog_cache.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import models_dev, models_snapshot
from providers.codex import upstream as cx_upstream
from providers.codex.provider import CodexProvider
from providers.codex.provider import _Catalog as CodexCatalog
from providers.codex.store import Account as CodexAccount
from providers.codex.store import CodexStore
from providers.opencode import OpenCodeProvider
from providers.workbuddy import upstream as wb_upstream
from providers.workbuddy.provider import WorkBuddyProvider
from providers.workbuddy.store import Account as WBAccount
from providers.workbuddy.store import WorkBuddyStore

# ── 脚手架 ──


@contextlib.contextmanager
def _temp_snapshot(tmp: str):
    """把快照落盘位置指到临时目录（.bak 由 PATH 派生，跟着走）。"""
    old_path = models_snapshot.PATH
    models_snapshot.PATH = Path(tmp) / "models.json"
    try:
        yield
    finally:
        models_snapshot.PATH = old_path


@contextlib.contextmanager
def _no_directory():
    """断网 + 空目录：不给测试起后台拉取线程，也不让真实 modelsdev.json 影响断言。

    （codex 的元数据现在"目录优先"：不隔离的话，本机缓存里的 gpt-5.5 会盖掉
    快照里的 272000，断言就跟跑测机器的缓存内容绑在一起了。）
    """
    saved = (
        models_dev._ensure_async,
        models_dev._index,
        models_dev._cache,
        models_dev._cache_loaded,
    )
    models_dev._ensure_async = lambda: None
    models_dev._index = None
    models_dev._cache = {}
    models_dev._cache_loaded = True          # 不读真实缓存文件
    try:
        yield
    finally:
        (
            models_dev._ensure_async,
            models_dev._index,
            models_dev._cache,
            models_dev._cache_loaded,
        ) = saved


@contextlib.contextmanager
def _fake_directory(index: dict[str, dict[str, Any]]):
    """把 models.dev 索引换成给定值（测"目录命中就以目录为准"）。"""
    saved = (
        models_dev._ensure_async,
        models_dev._index,
        models_dev._cache,
        models_dev._cache_loaded,
    )
    models_dev._ensure_async = lambda: None
    models_dev._index = index
    models_dev._cache = {}
    models_dev._cache_loaded = True
    try:
        yield
    finally:
        (
            models_dev._ensure_async,
            models_dev._index,
            models_dev._cache,
            models_dev._cache_loaded,
        ) = saved


@contextlib.contextmanager
def _patched(obj: Any, name: str, value: Any):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


# ── ① 快照写入护栏 ──


def test_snapshot_unchanged_skips_write_and_rotates_bak():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp):
        # .bak 必须跟着 PATH 走：另立常量会让重绑 PATH 的测试把合成内容轮转进真实 data/
        assert models_snapshot.backup_path().parent == models_snapshot.PATH.parent
        assert models_snapshot.write("codex", [{"id": "a"}]) is True
        updated_ms = models_snapshot.read_meta("codex")["updated_at_ms"]
        assert updated_ms > 0
        assert not models_snapshot.backup_path().exists()  # 首次写入没有旧文件可轮转

        # 内容不变（同值的新对象）→ 不写盘、内容时间不动
        assert models_snapshot.write("codex", [{"id": "a"}]) is False
        assert models_snapshot.read_meta("codex")["updated_at_ms"] == updated_ms
        assert not models_snapshot.backup_path().exists()

        # 内容变了 → 写盘，且旧内容轮转进 .bak（上游给出坏目录时可人工回滚）
        assert models_snapshot.write("codex", [{"id": "a"}, {"id": "b"}]) is True
        assert [e["id"] for e in models_snapshot.read()["codex"]] == ["a", "b"]
        backup = json.loads(models_snapshot.backup_path().read_text(encoding="utf-8"))
        assert [e["id"] for e in backup["codex"]] == ["a"]

        # meta 变更（如 opencode 的凭证可见清单）算变化要写盘，但不动内容时间
        changed_ms = models_snapshot.read_meta("codex")["updated_at_ms"]
        assert models_snapshot.write(
            "codex", [{"id": "a"}, {"id": "b"}], {"available": ["a"]}
        ) is True
        meta = models_snapshot.read_meta("codex")
        assert meta["available"] == ["a"] and meta["updated_at_ms"] == changed_ms
        # 其它 provider 的键与缺省形状不受影响
        assert models_snapshot.read()["workbuddy"] == {"cn": [], "glb": []}


# ── ② 三家缓存重建 ──


def test_codex_cache_round_trip_restores_route_and_fields():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            models_snapshot.write("codex", [{
                "id": "gpt-5.5", "name": "GPT-5.5", "context_window": 272000,
                "max_output_tokens": 128000, "input_modalities": ["text", "image"],
                "supports_reasoning": True, "supports_tool_call": True,
                "protocol": "responses",
            }])
            provider = CodexProvider(store=store)
            assert provider.load_from_snapshot() == 1

            route = provider.route("cx:gpt-5.5")
            assert route.upstream_model == "gpt-5.5" and route.protocol == "responses"
            info = {m.id: m for m in provider.list_models()}["cx:gpt-5.5"]
            assert info.display_name == "GPT-5.5"
            assert info.metadata["context_window"] == 272000
            assert info.metadata["max_output_tokens"] == 128000
            assert info.capabilities.get("supports_function_calling") is True
            assert info.capabilities.get("supports_vision") is True
            # 缓存时间进了 status（refreshed_at 留空：不是本进程刷的，不冒充新鲜度）
            status = provider.status()
            assert status["refreshed_at"] is None and status["cached_at"]
        finally:
            store.close()


def test_workbuddy_cache_round_trip_restores_route_and_promo():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            models_snapshot.write("workbuddy", {
                "cn": [],
                "glb": [{
                    "id": "gpt-6-luna", "name": "GPT-6-Luna", "description": "响应快",
                    "context_window": 200000, "max_output_tokens": 32000,
                    "supports_images": True, "supports_reasoning": True,
                    "supports_tool_call": True,
                    "reasoning_efforts": ["low", "high"], "default_effort": "low",
                    "protocol": "chat",
                    "extras": {
                        "credits": "x0.34 credits", "vendor": "vendor-x",
                        "promo_label": "限时五折", "promo_factor": 0.5,
                    },
                }],
            })
            provider = WorkBuddyProvider(store=store)
            assert provider.load_from_snapshot() == 1

            route = provider.route("glb:gpt-6-luna")
            assert route.upstream_model == "gpt-6-luna" and route.realm == "global"
            md = {m.id: m for m in provider.list_models()}["glb:gpt-6-luna"].metadata
            # extras（倍率/活动/credits）与档位都要从快照回到 _model_info 的那几列
            assert md["credits"] == "x0.34 credits"
            assert md["vendor"] == "vendor-x"
            assert md["promo_label"] == "限时五折"
            assert md["promo_factor"] == 0.5
            assert md["efforts"] == ["low", "high"] and md["default_effort"] == "low"
            assert md["context_window"] == 200000
            assert md["description"] == "响应快"
        finally:
            store.close()


def test_opencode_cache_round_trip_restores_route_and_available():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        models_snapshot.write(
            "opencode",
            [
                {
                    "id": "alpha", "name": "Alpha", "protocol": "chat", "free": True,
                    "context_window": 262144, "max_output_tokens": 32768,
                    "input_modalities": ["text"], "supports_reasoning": True,
                    "reasoning_efforts": ["low", "high"],
                    "extras": {"max_input": 200000, "structured_output": True},
                },
                {
                    "id": "beta", "name": "Beta", "protocol": "responses", "free": True,
                    "context_window": 1050000,
                },
            ],
            {"available": ["alpha"]},
        )
        provider = OpenCodeProvider()
        assert provider.load_from_snapshot() == 2
        assert provider._index.counts()["protocols"] == 2

        assert provider.route("oc:alpha").protocol == "chat"
        # 凭证可见清单也恢复了：不在清单里的模型不对外声明（否则会给出打不通的模型名）
        assert provider.supported("oc:alpha") is True
        assert provider.supported("oc:beta") is False

        info = {m.id: m for m in provider.list_models()}["oc:alpha"]
        assert info.metadata["maxInput"] == 200000
        assert info.metadata["reasoningOptions"] == ["low", "high"]
        assert info.metadata["structuredOutput"] is True
        assert info.protocol == "chat" and info.free is True
        assert provider.status()["cached_at"]


def test_opencode_old_snapshot_without_protocol_is_not_loaded():
    """老快照（没有 protocol 字段）重建不出路由 → 返回 0，交给启动兜底去拉一次。"""
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        models_snapshot.write("opencode", [{"id": "x", "name": "X"}])
        provider = OpenCodeProvider()
        assert provider.load_from_snapshot() == 0
        assert provider._index.counts()["protocols"] == 0
        assert provider.supported("oc:x") is False


# ── ③ 空结果护栏与差异回报 ──


def test_codex_empty_result_keeps_catalog_and_reports_diff():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            provider._catalog = CodexCatalog(
                models={"old": {"id": "old"}, "keep": {"id": "keep"}}
            )
            store.upsert_account(CodexAccount(
                uid="a1", account_id="a1", access_token="at", refresh_token="rt"
            ))

            # 上游整源给空：旧目录与旧快照都保留，回报里说明原因
            with _patched(cx_upstream, "fetch_models", lambda account: []):
                result = provider.refresh()
            assert result["error"].startswith("上游返回空目录"), result
            assert result["failed"] == 1, result      # 没有产出不算成功，界面要报出来
            assert set(provider._catalog.models) == {"old", "keep"}
            assert models_snapshot.read()["codex"] == []

            # 正常一轮：新增/移除如实回报，快照整体替换成新目录
            with _patched(cx_upstream, "fetch_models", lambda account: [
                {"id": "keep", "slug": "keep", "display_name": "Keep"},
                {"id": "new", "slug": "new", "display_name": "New"},
            ]):
                result = provider.refresh()
            assert result == {"added": 1, "removed": 1, "failed": 0, "error": ""}, result
            assert set(provider._catalog.models) == {"keep", "new"}
            assert sorted(e["id"] for e in models_snapshot.read()["codex"]) == ["keep", "new"]
        finally:
            store.close()


def test_workbuddy_empty_result_keeps_realm_catalog():
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp), _no_directory():
        store = WorkBuddyStore(Path(tmp) / "wb.db")
        try:
            provider = WorkBuddyProvider(store=store)
            provider._catalogs["global"] = type(provider._catalogs["global"])(
                models={"gpt-6-luna": {"id": "gpt-6-luna"}}, refreshed_at="t"
            )
            store.upsert_account(WBAccount(
                uid="g1", label="glb 号", access_token="at", refresh_token="rt",
                realm="global",
            ))

            with _patched(wb_upstream, "fetch_models", lambda account: []):
                result = provider.refresh()
            assert "空目录" in result["error"], result
            assert "gpt-6-luna" in provider._catalogs["global"].models
            assert provider.route("glb:gpt-6-luna").upstream_model == "gpt-6-luna"
        finally:
            store.close()


# ── ④ codex 元数据「目录优先」 ──


def test_codex_model_info_follows_directory_when_matched():
    """目录命中 → 上下文与输入模态都按 models.dev（manifest 的 272k / text,image 被盖掉），
    能力块也跟着走目录值；没命中的模型仍按 manifest 且不标来源。"""
    with tempfile.TemporaryDirectory() as tmp, _temp_snapshot(tmp):
        store = CodexStore(Path(tmp) / "codex.db")
        try:
            provider = CodexProvider(store=store)
            provider._catalog = CodexCatalog(models={
                "gpt-5.5": {
                    "id": "gpt-5.5", "name": "GPT-5.5",
                    "context_window": 272_000,
                    "input_modalities": ["text", "image"],
                    "supports_reasoning": True,
                },
                "gpt-reserve": {
                    "id": "gpt-reserve", "name": "GPT-Reserve",
                    "context_window": 272_000,
                    "input_modalities": ["text", "image"],
                },
            }, refreshed_at="t")
            directory = {
                "gpt-5.5": {
                    "context_window": 1_050_000, "max_output_tokens": 128_000,
                    "input_modalities": ["text", "image", "pdf"],
                    "output_modalities": ["text"],
                },
            }
            with _fake_directory(directory):
                info = {m.id: m for m in provider.list_models()}
            hit = info["cx:gpt-5.5"]
            assert hit.metadata["context_window"] == 1_050_000, hit.metadata
            assert hit.metadata["max_output_tokens"] == 128_000, hit.metadata
            assert hit.metadata["input_modalities"] == ["text", "image", "pdf"], hit.metadata
            assert hit.metadata["limits_source"] == {
                "context_window": "models.dev",
                "max_output_tokens": "models.dev",
                "input_modalities": "models.dev",
                "output_modalities": "models.dev",
            }, hit.metadata["limits_source"]
            # 能力块读的是目录值（对外声明与展示一致）
            assert hit.capabilities.get("context_window") == 1_050_000, hit.capabilities
            assert hit.capabilities.get("supports_pdf_input") is True, hit.capabilities

            miss = info["cx:gpt-reserve"]
            assert miss.metadata["context_window"] == 272_000, miss.metadata
            assert miss.metadata["input_modalities"] == ["text", "image"], miss.metadata
            assert "limits_source" not in miss.metadata, miss.metadata
        finally:
            store.close()


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
