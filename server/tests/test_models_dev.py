"""models.dev 兜底目录测试：索引采值规则 + 字段补齐语义（不碰网络）。

覆盖三块：
  - 索引采值（vendor 源优先 / 票数众数 / provider 名字典序 tie-break / 脏值丢弃 / 裸 id 归一）
  - 补齐语义（厂商值优先、只填空、0 视为未知、输出未知不编造、自定义键名）
  - 本地缓存（只写问过的模型、与既有条目合并、字段形状）

运行：cd server && uv run python tests/test_models_dev.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import models_dev  # noqa: E402

_STATE_FIELDS = (
    "_index",
    "_cache",
    "_cache_loaded",
    "_asked",
    "CACHE_PATH",
    "_ensure_async",
)


def _isolate(index: dict | None, cache: dict | None = None, cache_path: Path | None = None) -> tuple:
    """把模块状态换成测试值（含阻断 _ensure_async 的网络动作），返回原状态供还原。"""
    saved = tuple(getattr(models_dev, name) for name in _STATE_FIELDS)
    models_dev._index = index
    models_dev._cache = dict(cache or {})
    models_dev._cache_loaded = True          # 不读真实缓存文件
    models_dev._asked = set()
    if cache_path is not None:
        models_dev.CACHE_PATH = cache_path
    models_dev._ensure_async = lambda: None  # 测试不触发拉取
    return saved


def _restore(saved: tuple) -> None:
    for name, value in zip(_STATE_FIELDS, saved):
        setattr(models_dev, name, value)


def _doc(*providers: tuple) -> dict:
    """构造 models.dev 文档片段：("provider", {"模型 id": (context, output)})。"""
    doc: dict = {}
    for provider, models in providers:
        doc[provider] = {
            "models": {
                model_id: {"limit": {"context": context, "output": output}}
                for model_id, (context, output) in models.items()
            }
        }
    return doc


def test_index_vendor_source_wins():
    """官方源与聚合网关分歧时取官方值（票数少也算赢）。"""
    index = models_dev._build_index(_doc(
        ("nano-gpt", {"m": (1000, 100), "o": (1000, 100), "p": (1000, 100)}),
        ("openai", {"m": (2000, 200)}),
    ))
    assert index["m"] == {"context_window": 2000, "max_output_tokens": 200}, index["m"]


def test_index_majority_vote():
    """无官方源时按众数采值。"""
    index = models_dev._build_index(_doc(
        ("alpha", {"m": (1000, 100)}),
        ("beta", {"m": (1000, 100)}),
        ("gamma", {"m": (3000, 300)}),
    ))
    assert index["m"] == {"context_window": 1000, "max_output_tokens": 100}, index["m"]


def test_index_tie_break_is_deterministic():
    """同票数时按 provider 名字典序取最小者，且与文档遍历顺序无关（值不抖）。"""
    forward = models_dev._build_index(_doc(
        ("zzz-aggregator", {"m": (1000, 100)}),
        ("aaa-aggregator", {"m": (3000, 300)}),
    ))
    backward = models_dev._build_index(_doc(
        ("aaa-aggregator", {"m": (3000, 300)}),
        ("zzz-aggregator", {"m": (1000, 100)}),
    ))
    assert forward["m"] == backward["m"], (forward["m"], backward["m"])
    # aaa 名字更小 → 采它的值
    assert forward["m"] == {"context_window": 3000, "max_output_tokens": 300}, forward["m"]


def test_index_drops_dirty_values():
    """上下文缺失/非正/超量级整条丢弃；输出为负丢弃；输出 0 保留（表示未知）。"""
    index = models_dev._build_index(_doc(("openai", {
        "zero-context": (0, 100),
        "huge-context": (models_dev.VALUE_MAX + 1, 100),
        "negative-output": (1000, -5),
        "unknown-output": (1000, 0),
    })))
    assert "zero-context" not in index
    assert "huge-context" not in index
    assert "negative-output" not in index, index.get("negative-output")
    assert index["unknown-output"] == {"context_window": 1000, "max_output_tokens": 0}


def test_index_bare_id_normalization():
    """命名空间 id（openai/gpt-5.5）与裸名归一，大小写不敏感。"""
    index = models_dev._build_index(_doc(("openai", {"openai/gpt-5.5": (1050000, 128000)})))
    assert "gpt-5.5" in index, index
    assert index["gpt-5.5"]["max_output_tokens"] == 128000
    assert models_dev._bare("OpenAI/GPT-5.5") == "gpt-5.5"


def test_fill_limits_only_fills_missing():
    """厂商值优先：已有数字不动；None / 0 视为缺失才补。"""
    saved = _isolate({"m": {"context_window": 1000, "max_output_tokens": 128}})
    try:
        values: dict = {"context_window": 272000, "max_output_tokens": None}
        filled = models_dev.fill_limits("m", values)
        assert values["context_window"] == 272000, values          # 不覆盖厂商值
        assert values["max_output_tokens"] == 128, values          # 补上空值
        assert filled == ["max_output_tokens"], filled

        zero: dict = {"context_window": 0, "max_output_tokens": 0}
        assert models_dev.fill_limits("m", zero) == ["context_window", "max_output_tokens"]
        assert zero == {"context_window": 1000, "max_output_tokens": 128}, zero
    finally:
        _restore(saved)


def test_fill_limits_unknown_output_not_invented():
    """索引里输出为 0（未知）→ 不写该键；上下文照补。"""
    saved = _isolate({"m": {"context_window": 1000, "max_output_tokens": 0}})
    try:
        values: dict = {"context_window": None, "max_output_tokens": None}
        filled = models_dev.fill_limits("m", values)
        assert filled == ["context_window"], filled
        assert values == {"context_window": 1000, "max_output_tokens": None}, values
    finally:
        _restore(saved)


def test_fill_limits_custom_keys_and_cache_fallback():
    """opencode 的 camelCase 键名走参数；索引未就绪时回落本地缓存。"""
    saved = _isolate(None, cache={"m": {"context_window": 1000, "max_output_tokens": 64}})
    try:
        values: dict = {"contextWindow": None, "maxOutput": None}
        filled = models_dev.fill_limits(
            "m", values, context_key="contextWindow", output_key="maxOutput"
        )
        assert filled == ["contextWindow", "maxOutput"], filled
        assert values == {"contextWindow": 1000, "maxOutput": 64}, values
    finally:
        _restore(saved)


def test_lookup_unknown_model_is_none():
    """未收录模型返回 None（调用方保持「—」，不编造）。"""
    saved = _isolate({"other": {"context_window": 1, "max_output_tokens": 1}})
    try:
        assert models_dev.lookup("never-heard-of-it") is None
    finally:
        _restore(saved)


def test_cache_round_trip():
    """缓存只写问过的模型，字段形状固定，且带来源与时间戳。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "modelsdev.json"
        saved = _isolate(
            {"m": {"context_window": 1000, "max_output_tokens": 128}},
            cache_path=cache_path,
        )
        try:
            assert models_dev.lookup("m") == {"context_window": 1000, "max_output_tokens": 128}
            models_dev._write_cache_locked()
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            assert raw["models"] == {"m": {"context_window": 1000, "max_output_tokens": 128}}, raw
            assert raw["source"] == models_dev.URL
            assert raw["fetched_at_ms"] > 0
        finally:
            _restore(saved)


def test_cache_merges_previous_entries():
    """缓存写回与既有条目合并：本轮没问过的模型不被清掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "modelsdev.json"
        saved = _isolate(
            {"new": {"context_window": 2, "max_output_tokens": 2}},
            cache={"old": {"context_window": 1, "max_output_tokens": 1}},
            cache_path=cache_path,
        )
        try:
            models_dev.lookup("new")
            models_dev._write_cache_locked()
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            assert set(raw["models"]) == {"new", "old"}, raw["models"]
        finally:
            _restore(saved)


def test_build_index_survives_malformed_doc():
    """形状不对的文档返回空索引而不是抛异常（拉取失败静默降级）。"""
    assert models_dev._build_index(None) == {}
    assert models_dev._build_index({"p": "not-a-dict"}) == {}
    assert models_dev._build_index({"p": {"models": {"m": {"limit": "nope"}}}}) == {}


_ALL = [value for key, value in sorted(globals().items()) if key.startswith("test_")]


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
