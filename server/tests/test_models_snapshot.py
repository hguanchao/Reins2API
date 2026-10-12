"""providers/models_snapshot 单元测试（上游模型目录快照）。

运行：cd server && uv run python tests/test_models_snapshot.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import models_snapshot


def _with_tmp_path(tmp: str):
    """把模块的 PATH 指到临时目录，避免动到真实 data/models.json。"""
    models_snapshot.PATH = Path(tmp) / "models.json"
    return models_snapshot.PATH


def test_ensure_creates_default_shape():
    with tempfile.TemporaryDirectory() as tmp:
        path = _with_tmp_path(tmp)
        assert not path.exists()
        models_snapshot.ensure()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"opencode": [], "workbuddy": {"cn": [], "glb": []}, "codex": []}


def test_write_replaces_one_key_and_keeps_others():
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_path(tmp)
        models_snapshot.ensure()
        models_snapshot.write("opencode", [{"id": "big-pickle", "name": "Big Pickle"}])
        models_snapshot.write("workbuddy", {"cn": [{"id": "cn-1"}], "glb": [{"id": "glb-1"}]})
        data = models_snapshot.read()
        assert data["opencode"] == [{"id": "big-pickle", "name": "Big Pickle"}]
        assert data["workbuddy"] == {"cn": [{"id": "cn-1"}], "glb": [{"id": "glb-1"}]}
        assert data["codex"] == []          # 未写过的键保持默认
        # 再写 opencode 不冲掉 workbuddy
        models_snapshot.write("opencode", [])
        assert models_snapshot.read()["workbuddy"]["cn"] == [{"id": "cn-1"}]


def test_read_fills_missing_keys_and_survives_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        path = _with_tmp_path(tmp)
        path.write_text('{"opencode": [1]}', encoding="utf-8")   # 缺其余键
        data = models_snapshot.read()
        assert data["opencode"] == [1]
        assert data["workbuddy"] == {"cn": [], "glb": []}
        assert data["codex"] == []
        # 坏 JSON → 回落默认形状，不抛
        path.write_text("{not json", encoding="utf-8")
        assert models_snapshot.read() == {
            "opencode": [], "workbuddy": {"cn": [], "glb": []}, "codex": [],
        }


def test_write_preserves_unknown_keys():
    with tempfile.TemporaryDirectory() as tmp:
        path = _with_tmp_path(tmp)
        path.write_text('{"future_provider": [9]}', encoding="utf-8")
        models_snapshot.write("codex", [{"id": "gpt-5-codex"}])
        data = models_snapshot.read()
        assert data["future_provider"] == [9]
        assert data["codex"] == [{"id": "gpt-5-codex"}]


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
