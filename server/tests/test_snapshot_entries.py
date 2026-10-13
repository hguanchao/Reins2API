"""models.json 统一核心字段投影测试（opencode / workbuddy / codex 三家同名）。

三家快照条目由各自的 _snapshot_entry 投影到 models_snapshot.CORE_FIELDS：上游没
声明的字段省略（不落 False 冒充「确定没有」），上游的大块字段（codex manifest 的
base_instructions 等）不进快照。这里直接喂上游原样的字段名，断言投影结果。

运行：cd server && uv run python tests/test_snapshot_entries.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import models_snapshot
from providers.codex.provider import _core_entry as codex_entry
from providers.opencode.discovery import _snapshot_entry as opencode_entry
from providers.workbuddy.provider import _snapshot_entry as workbuddy_entry


def test_core_entry_drops_none_and_rejects_unknown():
    entry = models_snapshot.core_entry(
        id="m", name="M", supports_images=False, description=None, context_window=None,
    )
    assert entry == {"id": "m", "name": "M", "supports_images": False}  # 显式 False 是声明，保留
    try:
        models_snapshot.core_entry(id="m", display_name="M")
    except ValueError:
        pass
    else:
        raise AssertionError("词表外字段名应报错")


def test_opencode_entry_unified():
    raw = {
        "id": "ling-3.0-flash-fin-free",
        "name": "Ling 3.0 Flash Fin Free",
        "description": "响应快，适合简单任务",
        "reasoning": True,
        "reasoning_options": [
            {"type": "effort", "values": ["low", "medium"]},
            {"type": "toggle"},
        ],
        "tool_call": True,
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "limit": {"context": 262144, "input": 200000, "output": 32768},
        "cost": {"input": 0, "output": 0},
    }
    entry = opencode_entry("ling-3.0-flash-fin-free", raw)
    assert entry == {
        "id": "ling-3.0-flash-fin-free",
        "name": "Ling 3.0 Flash Fin Free",
        "description": "响应快，适合简单任务",
        "context_window": 262144,
        "max_output_tokens": 32768,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supports_images": True,
        "supports_reasoning": True,
        "supports_tool_call": True,
        "reasoning_efforts": ["low", "medium"],   # toggle 不是档位，不进 efforts
    }
    # 上游没声明的字段省略；name 缺失时回落模型 id
    assert opencode_entry("bare", {"id": "bare"}) == {"id": "bare", "name": "bare"}


def test_workbuddy_entry_unified():
    raw = {
        "id": "fast-model",
        "name": "Fast",
        "descriptionZh": "响应快，适合简单任务",
        "descriptionEn": "Fast and snappy",
        "maxInputTokens": 200000,
        "maxOutputTokens": 32000,
        "supportsImages": True,
        "supportsReasoning": True,
        "supportsToolCall": True,
        "reasoning": {"supportedEfforts": ["low", "medium", "high"], "defaultEffort": "medium"},
        "credits": "x0.34 credits",
        "vendor": "vendor-x",
    }
    entry = workbuddy_entry(raw)
    assert entry == {
        "id": "fast-model",
        "name": "Fast",
        "description": "响应快，适合简单任务",     # 优先中文描述
        "context_window": 200000,
        "max_output_tokens": 32000,
        "supports_images": True,
        "supports_reasoning": True,
        "supports_tool_call": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "default_effort": "medium",
    }
    # 上游只声明能力布尔位、没有模态列表，不合成 input_modalities
    assert "input_modalities" not in entry
    # 没声明的能力位省略，不落 False；effort 可作默认档位回落
    assert workbuddy_entry({"id": "x", "name": "X", "reasoning": {"effort": "low"}}) == {
        "id": "x", "name": "X", "default_effort": "low",
    }


def test_codex_entry_unified():
    raw = {
        "id": "gpt-6-luna",
        "slug": "gpt-6-luna",
        "display_name": "GPT-6-Luna",
        "description": "Fast and affordable model for easier tasks.",
        "context_window": 272000,
        "max_context_window": 872000,
        "input_modalities": ["text", "image"],
        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
        "default_reasoning_level": "high",
        "tool_mode": "code_mode_only",
        "supports_parallel_tool_calls": True,
        "base_instructions": "…几十 KB 的提示词，不进快照…",
        "model_messages": {"instructions_template": "…"},
    }
    entry = codex_entry(raw)
    assert entry == {
        "id": "gpt-6-luna",
        "name": "GPT-6-Luna",
        "description": "Fast and affordable model for easier tasks.",
        "context_window": 272000,
        "input_modalities": ["text", "image"],
        "supports_images": True,
        "supports_reasoning": True,
        "supports_tool_call": True,
        "reasoning_efforts": ["low", "high"],
        "default_effort": "high",
    }
    assert "max_output_tokens" not in entry    # manifest 不下发输出上限，省略而非填 0
    assert "output_modalities" not in entry    # manifest 也不下发输出模态
    assert "base_instructions" not in entry
    assert "model_messages" not in entry


def test_three_providers_share_core_field_names():
    """三家的输出键都在统一词表内——和 models_snapshot.CORE_FIELDS 是唯一口径。"""
    entries = [
        opencode_entry("m", {"id": "m"}),
        workbuddy_entry({"id": "m"}),
        codex_entry({"id": "m", "display_name": "M"}),
    ]
    for entry in entries:
        assert set(entry) <= set(models_snapshot.CORE_FIELDS), entry


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
