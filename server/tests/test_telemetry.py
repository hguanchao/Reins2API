"""遥测链路测试：首字耗时（first_byte_ms）的采集、落库与老库迁移。

运行：cd server && uv run python tests/test_telemetry.py
（无 pytest 时可 `uv run python -m pytest tests/test_telemetry.py -q`）

为什么单独立文件：这里测的是 db/ 与 gateway/base.py 的接缝，既不属于协议桥
（test_protocol_bridge）也不属于某个 provider（test_workbuddy）。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (
    ProviderStore,
    RequestRecord,
    schema,
)
from gateway.base import UpstreamResponse


class _FakeRaw:
    """最小 raw 视图：只实现 iter_content/content，外加一个属性验证透传。"""

    def __init__(self, chunks: list[bytes], content: bytes = b""):
        self._chunks = chunks
        # curl 在 stream 模式下 .content 恒为空——body 只在 iter_content 里。
        # 默认复现这个前提，read_body() 才会走 iter_content 分支（打点所在）；
        # 传 content 则模拟"非流式上游、curl 已整包缓冲"的情形。
        self.content = content
        self.headers = {"x-test": "1"}

    def iter_content(self, chunk_size: int = 8192):
        yield from self._chunks

    def close(self) -> None:
        return None


# ── 打点：首个非空块触发一次 ──


def test_stamp_fires_once_on_first_non_empty_chunk():
    raw = _FakeRaw([b"", b"first", b"second"])
    response = UpstreamResponse(status=200, headers={}, raw=raw)
    hits: list[int] = []
    response.stamp_first_chunk(lambda: hits.append(len(hits)))
    got = b"".join(response.iter_chunks(chunk_size=8))
    assert got == b"firstsecond"  # 内容一字不差地透传
    assert len(hits) == 1  # 只打一次


def test_stamp_survives_read_body_path():
    """非流式路径走 read_body()：它内部也落到 iter_content，打点同样生效。"""
    raw = _FakeRaw([b"a", b"b"])
    response = UpstreamResponse(status=200, headers={}, raw=raw)
    hits: list[int] = []
    response.stamp_first_chunk(lambda: hits.append(1))
    assert response.read_body() == b"ab"
    assert hits == [1]


def test_stamp_passthrough_and_empty_body():
    """空体：读通（content/close 都靠 __getattr__ 透传），但不该打点。"""
    raw = _FakeRaw([])
    response = UpstreamResponse(status=204, headers={}, raw=raw)
    hits: list[int] = []
    response.stamp_first_chunk(lambda: hits.append(1))
    assert response.read_body() == b""
    assert hits == []  # 没有非空块
    assert raw.headers["x-test"] == "1"  # 原对象未被改动


def test_stamp_not_fired_when_body_never_read():
    raw = _FakeRaw([b"x"])
    response = UpstreamResponse(status=200, headers={}, raw=raw)
    hits: list[int] = []
    response.stamp_first_chunk(lambda: hits.append(1))
    assert hits == []  # 没人读流 → 不打点（失败请求就是这么留 0 的）


def test_no_stamp_when_upstream_was_buffered():
    """非流式上游（curl 整包缓冲）：网关看不到"第一个字节"，必须留 0。

    这是有意的限制，不是漏埋点：读 .content 时整包已经到齐，
    那一刻打点等于把总耗时当首字上报——错数据比空着更糟。
    opencode 的 curl 流模式跟随客户端意图，客户端要非流式就会走到这里。
    """
    raw = _FakeRaw([], content=b"buffered-body")
    response = UpstreamResponse(status=200, headers={}, raw=raw)
    hits: list[int] = []
    response.stamp_first_chunk(lambda: hits.append(1))
    assert response.read_body() == b"buffered-body"  # 缓冲内容照常可用
    assert hits == []  # 但绝不打点


# ── 落库：字段顺序与 INSERT 列表必须对齐 ──


def test_record_persists_first_byte_ms():
    with tempfile.TemporaryDirectory() as tmp:
        store = ProviderStore("t", Path(tmp) / "t.db")
        try:
            store.log_request(RequestRecord(
                id="req_test", created_ms=1_700_000_000_000,
                first_byte_ms=321, duration_ms=1024, http_status=200,
            ))
            store.log_request(RequestRecord(
                id="req_fail", created_ms=1_700_000_001_000,
                first_byte_ms=0, duration_ms=88, http_status=429,
            ))
            total, rows = store.recent(limit=10)
            assert total == 2
            by_id = {r["request_id"]: r for r in rows}
            assert by_id["req_test"]["first_byte_ms"] == 321
            assert by_id["req_test"]["duration_ms"] == 1024
            # 失败请求留 0 = 无数据（前端渲染成「—」）
            assert by_id["req_fail"]["first_byte_ms"] == 0
            # 首字必然早于总耗时，否则打点位置就错了
            assert by_id["req_test"]["first_byte_ms"] < by_id["req_test"]["duration_ms"]
        finally:
            store.close()


# ── 迁移：老库（无该列）打开后自动补列 ──


def test_migration_adds_first_byte_ms_to_legacy_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.db"
        # 用"去掉 first_byte_ms 行"的当前 DDL 造老库：比手抄一份更能跟随 DDL 演进
        legacy_ddl = "\n".join(
            line for line in schema.DDL.splitlines() if "first_byte_ms" not in line
        )
        assert "first_byte_ms" not in legacy_ddl
        conn = sqlite3.connect(str(path))
        conn.executescript(legacy_ddl)
        conn.execute(
            "INSERT INTO requests (created_ms, created_at, http_status) VALUES (1, 'x', 200)"
        )
        conn.commit()
        conn.close()

        store = ProviderStore("legacy", path)
        try:
            cols = {r[1] for r in store._conn.execute("PRAGMA table_info(requests)")}
            assert "first_byte_ms" in cols  # 补列生效
            # 旧行读到 0（无数据），不报错
            _, rows = store.recent(limit=1)
            assert rows[0]["first_byte_ms"] == 0
            # 新写入能带上该列
            store.log_request(RequestRecord(id="new", created_ms=2, first_byte_ms=50))
            _, rows = store.recent(limit=1)
            assert rows[0]["first_byte_ms"] == 50
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
