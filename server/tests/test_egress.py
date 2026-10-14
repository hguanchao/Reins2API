"""出口 IP 探测缓存的行为约定（转发路径的硬约束）。

运行：cd server && uv run python tests/test_egress.py

为什么单独立文件：`cached_ip` 被放在每条请求的落库路径上，几条底线必须钉住——
① 它绝不能联网：缓存冷时也得立刻返回空串，不能让转发去等 echo 服务；
② 冷缓存 + 并发请求只许甩出**一个**后台探测（单飞），否则每条请求一个线程去打 echo；
③ 改代理（`invalidate`）之前发出、之后才回来的探测不能写回缓存，否则旧出口 IP 会
   顶着"新配置"的答案一直错到 TTL 到期。

测试不联网：只把 egress 命名空间里的 `crequests` 换成假 curl（不 patch `probe`
本身——那等于把要测的缓存与代际判断一起换掉了）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from net import egress

_UPSTREAM = "https://upstream.example"
_OTHER = "https://another-upstream.example"
_IP = "203.0.113.9"


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str):
        self.content = text.encode("utf-8")


class _FakeCurl:
    """假 curl：记下调用、卡在闸门上、返回一个固定 IP。"""

    def __init__(self, gate: threading.Event, calls: list[tuple[str, dict | None]]):
        self._gate = gate
        self.calls = calls

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs.get("proxies")))  # type: ignore[arg-type]
        self._gate.wait(2)
        return _FakeResponse(_IP)


def _drain(timeout: float = 2.0) -> None:
    """等上一用例留下的后台探测收尾：否则它会在下一用例中途写缓存，制造假通过。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with egress._lock:
            if not egress._inflight:
                return
        time.sleep(0.01)


def _reset(gate: threading.Event) -> list[tuple[str, dict | None]]:
    _drain()
    egress.invalidate()
    fake = _FakeCurl(gate, [])
    egress.crequests = fake  # type: ignore[assignment]
    return fake.calls


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_cold_cache_returns_empty_without_probing_inline() -> None:
    """缓存冷：立刻返回空串（不阻塞），探测甩给后台线程。"""
    gate = threading.Event()
    calls = _reset(gate)
    started = time.time()
    assert egress.cached_ip(_UPSTREAM) == "", "冷缓存必须返回空串"
    # 假 curl 卡在闸门上，cached_ip 仍秒回 = 它没在探测上等
    assert time.time() - started < 0.5, "cached_ip 不能阻塞在探测上"
    assert _wait_for(lambda: len(calls) == 1), "应有一个后台探测去打 echo"
    assert egress.cached_ip(_UPSTREAM) == "", "探测还没回来，这一条仍应为空"
    gate.set()


def test_warm_probe_fills_next_rows() -> None:
    """后台探测写回缓存后，后续请求就能拿到出口 IP。"""
    gate = threading.Event()
    _reset(gate)
    assert egress.cached_ip(_UPSTREAM) == ""
    gate.set()
    assert _wait_for(lambda: egress.cached_ip(_UPSTREAM) == _IP), "探测完成后应有值"


def test_cold_cache_probes_once_under_concurrency() -> None:
    """单飞：缓存冷时并发调 cached_ip 只起一个后台探测。"""
    gate = threading.Event()
    calls = _reset(gate)
    for _ in range(8):
        assert egress.cached_ip(_UPSTREAM) == ""
    gate.set()
    assert _wait_for(lambda: egress.cached_ip(_UPSTREAM) == _IP)
    time.sleep(0.05)  # 给"多起"的线程一点时间暴露自己
    assert len(calls) == 1, f"应只探一次，实际 {len(calls)} 次"


def test_per_url_cache_and_invalidate() -> None:
    """缓存按上游分键；invalidate 后必须重探（代理配置变了，出口就变了）。"""
    gate = threading.Event()
    gate.set()  # 本用例里探测立即完成
    calls = _reset(gate)

    assert _wait_for(lambda: egress.cached_ip(_UPSTREAM) == _IP)
    assert _wait_for(lambda: egress.cached_ip(_OTHER) == _IP), "另一个上游不该顶掉第一个的缓存"
    assert egress.cached_ip(_UPSTREAM) == _IP, "分键缓存：第一个上游的值还在"
    assert len(calls) == 2, f"两个上游各探一次，实际 {len(calls)} 次"

    egress.invalidate()
    assert egress.cached_ip(_UPSTREAM) == "", "invalidate 后缓存应清空"
    assert _wait_for(lambda: egress.cached_ip(_UPSTREAM) == _IP)
    assert len(calls) == 3, f"invalidate 后应重探，实际 {len(calls)} 次"


def test_probe_from_before_invalidate_does_not_poison_cache() -> None:
    """改代理之前发出、之后才回来的探测不能写回缓存。

    写回就等于把"旧配置的出口 IP"当成新配置的答案，一直错到 TTL 到期——而改代理的
    那一刻正是用户最想看到新出口的时候。"""
    gate = threading.Event()
    calls = _reset(gate)
    assert egress.cached_ip(_UPSTREAM) == ""  # 后台探测发出
    assert _wait_for(lambda: len(calls) == 1), "探测应已在飞"
    egress.invalidate()  # 这期间换了代理
    gate.set()  # 旧探测这才回来
    _drain()
    assert egress.cached_ip(_UPSTREAM) == "", "旧配置的探测结果不该进缓存"


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
