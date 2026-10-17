"""控制台渲染器的行格式契约（core/console_view.py）。

运行：cd server && uv run python tests/test_console_view.py

为什么单独立文件：渲染器把 logger.py 的文件行格式当**输入契约**解析（gateway/inference.py
发出的受理/完成两行拼一条请求），格式一漂移，控制台就退化成"原文兜底"——看起来还在滚，
其实列已经错位、请求也对不上。这里把契约钉住：
① 受理/完成两行必须渲成同列（正文起始位置一致，身份格都是请求短 ID）；
② 完成行 errs 要给原因，499 认成"客户端断开"而不是 ERROR，2xx 是 OK；
③ 带 request= 的散行要把请求 ID 提到身份列（与请求行同列），域名行保留域名；
④ 没有毫秒的旧行（历史日志）也要照常解析；异常堆栈续行缩进到正文列、不参与解析；
⑤ --since/--tail 的挑选逻辑与"从第 N 行接续"的行号一致；渲染不产生 ANSI 转义。
测试不碰日志文件：直接喂行文本给 _emit，抓 stdout。
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import console_view as cv
from core.term import message_col, width

_ACCEPT = (
    "2026-10-16 21:52:36.014 | INFO     | gateway.inference:_serve_inner:232 - "
    "[推理] 受理 request=req_7f3a91c4e5f6a7b8 provider=opencode model=oc:big-pickle "
    "upstream=big-pickle proto=chat stream=True free=True shaped=True ip=127.0.0.1"
)
_DONE = (
    "2026-10-16 21:52:40.318 | INFO     | gateway.inference:_serve:126 - "
    "[推理] 完成 request=req_7f3a91c4e5f6a7b8 provider=opencode model=oc:big-pickle "
    "proto=chat status=200 dur_ms=4310 ttfb_ms=480 in=1220 out=3410 cached=0 acct=9f3c1d2a…"
)
_FAIL = (
    "2026-10-16 21:52:44.630 | INFO     | gateway.inference:_serve:126 - "
    "[推理] 完成 request=req_0c5b2ad9ff11aa22 provider=workbuddy model=cn:instruct "
    "proto=responses status=429 dur_ms=2720 ttfb_ms=0 in=0 out=0 cached=0 "
    'acct=4b81e0c7… err="全部账号额度用尽"'
)
_BODY_COL = message_col()  # 正文的固定起始列


def _render(text: str, min_level: int = 20) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cv._emit(text, min_level)
    return buf.getvalue().rstrip("\n")


def test_request_pair_shares_columns() -> None:
    accepted = _render(_ACCEPT)
    done = _render(_DONE)
    # 头部网格:时钟 12 列 + 空格 + 级别右对齐 5 + ` --- [ID] ` + 正文
    assert accepted.startswith("21:52:36.014   INFO  --- [req_7f3a91c4…"), accepted
    assert done.startswith("21:52:40.318     OK  --- [req_7f3a91c4…"), done
    # 受理带事件与关键字段;完成是 OK 级别带状态/耗时/首字/用量/账号
    assert "受理" in accepted and "model=oc:big-pickle" in accepted and "免费档" in accepted, accepted
    for piece in ("status=200", "dur=4.31s", "首字480ms", "1.2k↑3.4k↓", "oc·9f3c1d2a…"):
        assert piece in done, f"{piece!r} 不在完成行里: {done}"
    # 身份列对齐:两行的 [ ] 恰好都在同一列
    assert accepted.index("[") == done.index("["), (accepted, done)


def test_failure_row_keeps_reason() -> None:
    row = _render(_FAIL)
    assert "ERROR" in row and "status=429" in row and "dur=2.72s" in row, row
    assert "全部账号额度用尽" in row, row
    assert "wb·4b81e0c7…" in row, row
    assert not row.startswith((" ", "✗", "!")), row  # 头部网格,无符号


def test_canceled_row_is_not_an_error() -> None:
    row = _render(_DONE.replace("status=200", "status=499"))
    assert "INFO" in row and "客户端断开" in row and "ERROR" not in row, row


def test_missing_millis_still_parses() -> None:
    # 历史日志（格式加毫秒之前）也要能读：毫秒可选,时钟补齐到 12 列不错位
    row = _render(_DONE.replace("21:52:40.318", "21:52:40"))
    assert "req_7f3a91c4…" in row, row
    assert row.index("[") == _render(_DONE).index("["), row
    assert row.startswith("21:52:40       OK") or " OK  --- " in row, row


def test_request_scoped_line_hoists_id() -> None:
    row = _render(
        "2026-10-16 21:52:43.115 | WARNING  | gateway.inference:_copy_upstream_error:459 - "
        "[推理] request=req_0c5b2ad9ff11aa22 上游返回错误 status=429: 全部账号额度用尽"
    )
    assert row.startswith("21:52:43.115   WARN  --- [req_0c5b2ad9…"), row
    assert "request=" not in row, row  # 身份列里已经是 ID，正文里不该再留一份
    assert "上游返回错误 status=429" in row, row


def test_domain_line_keeps_domain() -> None:
    row = _render(
        "2026-10-16 21:52:43.115 | WARNING  | providers.workbuddy.provider:send:705 - "
        "[workbuddy] uid=a12c9f04… 上游错误 status=429 kind=quota: 额度用尽"
    )
    assert row.startswith("21:52:43.115   WARN  --- [workbuddy    ]"), row
    assert "uid=a12c9f04…" in row, row


def test_level_filter_and_continuation() -> None:
    assert _render("2026-10-16 21:52:49.881 | TRACE    | providers.codex.pool:pick:88 - [codex] 选号") == ""
    trace = _render('  File "gateway/inference.py", line 100, in _serve')
    assert trace.startswith(" " * message_col()) and "in _serve" in trace, trace


def test_no_ansi_escape_in_any_output() -> None:
    for line_text in (_ACCEPT, _DONE, _FAIL):
        out = _render(line_text)
        assert "\x1b" not in out, out


def test_identifier_and_width_helpers() -> None:
    assert cv._short_id("req_7f3a91c4e5f6a7b8") == "req_7f3a91c4…"
    assert cv._short_id("msg_145507020506cfff") == "msg_14550702…"
    assert cv._short_id("-") == "-"
    assert cv._fmt_ms(880) == "880ms" and cv._fmt_ms(4310) == "4.31s"
    assert cv._fmt_tokens(980) == "980" and cv._fmt_tokens(1220) == "1.2k"
    assert width("推理") == 4 and width("codex") == 5


def test_select_since_and_tail_line_numbers() -> None:
    lines = [
        "2026-10-16 21:52:31.101 | INFO     | a:b:1 - [启动] 第一条",
        "2026-10-16 21:52:36.014 | INFO     | a:b:2 - [推理] 受理 request=req_1",
        "2026-10-16 21:52:40.318 | INFO     | a:b:3 - [推理] 完成 request=req_1 status=200",
    ]
    start, picked = cv._select(lines, since="2026-10-16 21:52:36", tail=0)
    assert start == 2 and len(picked) == 2, (start, picked)
    start, picked = cv._select(lines, since="", tail=2)
    assert start == 2 and len(picked) == 2, (start, picked)
    start, picked = cv._select(lines, since="2026-10-16 23:00", tail=0)
    assert start == 4 and picked == [], (start, picked)


def test_file_format_parses() -> None:
    """logger 的文件行格式与视图解析器是同一份契约：格式漂移必须在这里红。

    上一条测试的样本行是手抄的，抄写与真实格式会各自腐化；这里从 core/logger 的
    FILE_LINE_FORMAT 生成样本行，格式改动会立刻反映到解析结果上。
    """
    import re

    from core.logger import FILE_LINE_FORMAT

    def render(**fields: str) -> str:
        return re.sub(r"\{(\w+)(?::[^}]*)?\}", lambda m: fields[m.group(1)], FILE_LINE_FORMAT)

    accepted = render(
        time="2026-10-16 21:52:36.014", level="INFO    ",
        name="gateway.inference", function="_serve_inner", line="232",
        message="[推理] 受理 request=req_7f3a91c4e5f6a7b8 provider=opencode model=oc:big-pickle "
                "proto=chat stream=True free=False shaped=False ip=127.0.0.1",
    )
    match = cv._LINE.match(accepted)
    assert match is not None, f"视图解析不了 logger 的格式行: {accepted!r}"
    assert match["ts"] == "2026-10-16 21:52:36.014" and match["level"] == "INFO"
    rendered = _render(accepted)
    assert rendered.startswith("21:52:36.014   INFO  --- [req_7f3a91c4…"), rendered

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
