"""出站请求体序列化回归测试（对照官方客户端 2.0.29 的 compile 期清理）。

客户端能用 `\\ud800` 这类 JSON 转义把**孤立代理项**送进来：json.loads 照单全收，
而 `json.dumps(..., ensure_ascii=False).encode("utf-8")` 会直接抛 UnicodeEncodeError
——出站序列化的唯一崩溃点（线上表现：请求 500，且错误信息里没有用户可理解的原因）。
官方客户端在编译请求时统一做 `toWellFormed`（utils/sanitize.ts），这里对齐同一
语义：孤立代理项 → U+FFFD，成对代理项合并成真实字符，正常字符串零改动。

运行：cd server && .venv/Scripts/python.exe tests/test_outbound_body.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import inference
from gateway.jsonutil import encode_json_body, sanitize_surrogates, well_formed


def test_well_formed_semantics():
    # 孤立代理项 -> U+FFFD（与官方 toWellFormed 同义）
    assert well_formed("abc\ud800def") == "abc\ufffddef"
    assert well_formed("x\udc00") == "x\ufffd"
    # 成对代理项合并成真实字符；已是真实字符 / 正常文本原样返回
    assert well_formed("\ud83d\ude00") == "\U0001F600"
    assert well_formed("\U0001F600") == "\U0001F600"
    assert well_formed("héllo 世界") == "héllo 世界"
    assert well_formed("") == ""


def test_sanitize_walks_containers_and_keys():
    payload = {
        "messages": [{"role": "user", "content": "hi \ud800"}],
        "meta\ud800": {"nested": ["\udc00", 5, None, True]},
    }
    cleaned = sanitize_surrogates(payload)
    assert cleaned["messages"][0]["content"] == "hi \ufffd"
    assert "meta\ufffd" in cleaned
    assert cleaned["meta\ufffd"]["nested"][0] == "\ufffd"
    # 非字符串原样保留
    assert cleaned["meta\ufffd"]["nested"][1:] == [5, None, True]


def test_encode_json_body_survives_lone_surrogates():
    # 这是修复前的崩溃形态：json.dumps(ensure_ascii=False).encode("utf-8") 抛
    # UnicodeEncodeError；修后同一份 body 必须编得出来且是合法 JSON。
    body = {"messages": [{"role": "user", "content": "hi \ud800"}]}
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        raise AssertionError("前置条件不成立：孤立代理项本该让裸编码崩溃")
    except UnicodeEncodeError:
        pass
    raw = encode_json_body(body)
    assert json.loads(raw.decode("utf-8"))["messages"][0]["content"] == "hi \ufffd"


def test_encode_json_body_normal_path_is_unchanged():
    body = {"model": "big-pickle", "messages": [{"role": "user", "content": "中文 ok"}]}
    assert encode_json_body(body) == json.dumps(body, ensure_ascii=False).encode("utf-8")


def test_inference_serialization_is_wired_to_the_helper():
    """防"修在模块里、没接进推理层"：出站序列化点必须走 encode_json_body。"""
    source = inspect.getsource(inference)
    assert "encode_json_body(upstream_body)" in source, "推理层没有走 encode_json_body"


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
