"""上游 SSE 流包装：行缓冲 + 流内 error 帧观测 + DSML 标记修复展开 + 帧规范化。

provider 把 curl_cffi 的 iter_chunks 包成本模块的生成器再交给 UpstreamResponse，
网关无感。逐帧语义对齐 wb2api sse.go StreamHint：
  - `data: [DONE]` 终止透传（之后的数据一律丢弃）；
  - 带 `error` 键的帧先回调观察者（罚号记账）再**原样透传**（不走白名单，
    保住 error 字段供客户端看到上游 code/msg）;
  - 其余帧喂 MarkupRepair：命中标记时一帧展开为多帧（正文帧 + N 个
    delta.tool_calls 帧 + 收尾 finish 帧）；
  - 出口一律过 _normalize_frame 重建成标准 OpenAI 形态（见该函数注释）；
  - 流尾 flush 回吐未判定字节；还原过调用但上游没发收尾帧时补一帧
    finish_reason: tool_calls。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from providers.workbuddy.dsml import (
    MarkupCall,
    MarkupRepair,
    markup_stream_tool_calls,
)

# 帧顶层白名单（对齐 wb2api normalizeFrame）：其余键——包括上游私有键
# extra_fields / 各类空占位——全部丢弃。
_FRAME_TOP_KEYS = ("id", "object", "created", "model", "system_fingerprint", "service_tier")
# delta 里按「非空才出现」处理的字符串键
_DELTA_TEXT_KEYS = ("role", "content", "reasoning_content", "refusal")


def _normalize_frame(obj: dict[str, Any]) -> dict[str, Any]:
    """把一帧上游 chunk 重建成标准 OpenAI 形态（逐字段对齐 wb2api normalizeFrame）。

    为什么必须做：上游（腾讯那套）在 delta 里塞满空占位——`content: ""`、
    `reasoning_content: ""`、`tool_calls: []`、`function_call: null`、`refusal: ""`，
    外加 `extra_fields` 之类的私有键；`finish_reason` 还用 `""` 表示"没结束"
    （标准是 null）。这些噪声会把客户端的流式解析带偏：实测 ZCode 会把
    reasoning_content 的**每个增量**当成一段新的思考块，思考链被切成"一词一块"。

    规则：顶层只留白名单键（缺 object/id 时补标准值）；choices 只留
    index / delta / finish_reason；delta 里的字符串键只在非空时出现，
    空 tool_calls 列表与空占位 function_call 丢弃；finish_reason 只留非空字符串、
    否则 null；usage 原样保留（缺省 null）。
    """
    out: dict[str, Any] = {}
    for key in _FRAME_TOP_KEYS:
        value = obj.get(key)
        if value is not None:
            out[key] = value
    out.setdefault("object", "chat.completion.chunk")
    out.setdefault("id", "chatcmpl-wb2api")

    choices = obj.get("choices")
    if isinstance(choices, list):
        normalized: list[Any] = []
        for item in choices:
            if not isinstance(item, dict):
                continue
            choice: dict[str, Any] = {}
            if "index" in item:
                choice["index"] = item["index"]
            delta: dict[str, Any] = {}
            raw_delta = item.get("delta")
            if isinstance(raw_delta, dict):
                for key in _DELTA_TEXT_KEYS:
                    value = raw_delta.get(key)
                    if isinstance(value, str) and value:
                        delta[key] = value
                calls = raw_delta.get("tool_calls")
                if isinstance(calls, list) and calls:
                    delta["tool_calls"] = calls
                function_call = raw_delta.get("function_call")
                if isinstance(function_call, dict):
                    # 空占位（name/arguments 全空）视为噪声剔除
                    if function_call.get("name") or function_call.get("arguments"):
                        delta["function_call"] = function_call
                elif function_call is not None:
                    delta["function_call"] = function_call
            choice["delta"] = delta
            finish = item.get("finish_reason")
            choice["finish_reason"] = finish if isinstance(finish, str) and finish else None
            normalized.append(choice)
        out["choices"] = normalized

    out["usage"] = obj.get("usage") if "usage" in obj else None
    return out


def _call_frames(src: dict[str, Any], calls: list[MarkupCall], base: int) -> list[dict[str, Any]]:
    """以 src 帧为模板构造「只带一个 delta.tool_calls」的帧序列。"""
    frames: list[dict[str, Any]] = []
    for offset, call in enumerate(calls):
        frame: dict[str, Any] = {
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": markup_stream_tool_calls([call], base + offset)}}],
        }
        for key in ("created", "model", "id"):
            if src.get(key) is not None:
                frame[key] = src[key]
        frames.append(frame)
    return frames


def _finish_frame(src: dict[str, Any], finish: str) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    }
    for key in ("created", "model", "id"):
        if src.get(key) is not None:
            frame[key] = src[key]
    return frame


class SseRepairStream:
    """把上游 SSE 字节块迭代器包装为「修复后」的字节块迭代器。

    行语义对齐 Go bufio.ReadString('\n')：逐行处理，`data: ` 行按帧处理，
    其余行（注释/空行）原样透出；行终止符统一规范为 \n。
    """

    def __init__(
        self,
        chunks: Iterable[bytes],
        repair: MarkupRepair | None,
        on_error_frame: Callable[[str], None] | None = None,
        closer: Callable[[], None] | None = None,
        on_usage_frame: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._chunks = chunks
        self._repair = repair
        self._on_error_frame = on_error_frame
        self._on_usage_frame = on_usage_frame
        self._closer = closer
        self._buffer = b""
        self._done = False          # 上游 [DONE] 之后的帧一律丢弃
        self._synth_idx = 0         # 本流已还原的调用数（合成 index 接续上游最大值）
        self._max_tool_idx = -1     # 上游已用过的最大 tool_call index
        self._tool_finish_sent = False

    # ── UpstreamResponse 适配面（raw 视图契约：content / iter_content / close）──

    @property
    def content(self) -> bytes:
        # 流式响应按契约恒返回空，迫使 read_body 走 iter_content 聚合。
        return b""

    def iter_content(self, chunk_size: int = 4096) -> Iterator[bytes]:
        return iter(self)

    def close(self) -> None:
        if self._closer is not None:
            try:
                self._closer()
            except Exception:
                pass

    # ── 迭代器外壳 ──

    def __iter__(self) -> Iterator[bytes]:
        return self._generate()

    def _generate(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._buffer += chunk
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                out = self._handle_line(line.decode("utf-8", "replace"))
                if out is not None:
                    yield out
        # 上游 EOF：残留半行按完整行处理（Go 同款——EOF 行也会被 ReadString 返回）。
        if self._buffer and not self._done:
            out = self._handle_line(self._buffer.decode("utf-8", "replace"))
            self._buffer = b""
            if out is not None:
                yield out
        yield from self._tail()

    # ── 帧处理 ──

    def _handle_line(self, line: str) -> bytes | None:
        text = line.rstrip("\r")
        if self._done:
            return None
        if not text.startswith("data: "):
            return (text + "\n").encode("utf-8") if text else b"\n"
        payload = text[len("data: "):]
        if payload.strip() == "[DONE]":
            # 上游显式结束：本行不透出（[DONE] 统一在流尾写出，保证恰好一个）；
            # 此后的任何数据帧一律丢弃。
            self._done = True
            return None
        return self._handle_frame(payload)

    def _handle_frame(self, payload: str) -> bytes | None:
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return f"data: {payload}\n\n".encode()  # 非 JSON 帧：原样降级
        if not isinstance(obj, dict):
            return f"data: {payload}\n\n".encode()
        # 上游错误帧：旁路观测（账号处置）后原样透出，保住 error 字段。
        if "error" in obj:
            if self._on_error_frame is not None:
                try:
                    self._on_error_frame(payload)
                except Exception:
                    pass  # 观察者缺陷不得击穿流透传主路径
            return f"data: {payload}\n\n".encode()
        frames = self._repair_frame(obj, payload)
        if self._on_usage_frame is not None:
            # 末帧 usage 观测（成本台账）：上游把本次真实扣费放在 usage.credit 里，
            # 同一账号在不同模型上"免不免费"只有实测才知道。观察者缺陷不得击穿流。
            for frame in frames:
                usage = frame.get("usage")
                if isinstance(usage, dict):
                    try:
                        self._on_usage_frame(usage)
                    except Exception:
                        pass
        return "".join(
            f"data: {json.dumps(_normalize_frame(f), ensure_ascii=False)}\n\n" for f in frames
        ).encode("utf-8")

    def _repair_frame(self, obj: dict[str, Any], fallback: str) -> list[dict[str, Any]]:
        """DSML 修复：返回「可能多帧」。未命中时单元素（或原样透传）。"""
        if self._repair is None or not self._repair.enabled():
            return [obj]
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return [obj]
        calls: list[MarkupCall] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if not isinstance(text, str) or text == "":
                continue
            emit, got = self._repair.feed(text)
            if got:
                calls.extend(got)
            if emit == "":
                delta.pop("content", None)
            else:
                delta["content"] = emit
        self._track_max_tool_idx(obj)
        if not calls:
            # 本帧没命中标记；但本流此前还原过调用 → stop 收尾改写 tool_calls。
            if self._synth_idx > 0 and self._rewrite_finish(obj, "tool_calls"):
                self._tool_finish_sent = True
            return [obj]
        # 命中：finish_reason 必须挪到工具调用帧之后，否则客户端读到调用前就认为回合结束。
        finish = self._take_finish_reason(obj)
        out = [obj]
        for call in calls:
            out.extend(_call_frames(obj, [call], self._max_tool_idx + 1 + self._synth_idx))
            self._synth_idx += 1
        if finish:
            if finish == "stop":
                finish = "tool_calls"
            out.append(_finish_frame(obj, finish))
            self._tool_finish_sent = finish == "tool_calls"
        return out

    @staticmethod
    def _take_finish_reason(obj: dict[str, Any]) -> str:
        """取出并删除所有 choice 的 finish_reason，返回第一个非空值。"""
        first = ""
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            value = choice.pop("finish_reason", None)
            if isinstance(value, str) and value and not first:
                first = value
        return first

    @staticmethod
    def _rewrite_finish(obj: dict[str, Any], want: str) -> bool:
        """把「已有且为 stop」的 finish_reason 改写；本就没有的不凭空造收尾。"""
        changed = False
        for choice in obj.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason") == "stop":
                choice["finish_reason"] = want
                changed = True
        return changed

    def _track_max_tool_idx(self, obj: dict[str, Any]) -> None:
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc, dict) and isinstance(tc.get("index"), (int, float)):
                    self._max_tool_idx = max(self._max_tool_idx, int(tc["index"]))

    # ── 流尾收尾 ──

    def _tail(self) -> Iterator[bytes]:
        # 修复层尾部收尾：无论上游是否显式 [DONE] 都要跑（Go 同款——[DONE] 只终止
        # 读取循环，不终止修复层收尾）。
        if self._repair is not None and self._repair.enabled():
            tail = self._repair.flush()
            if tail:
                tail_obj: dict[str, Any] = {
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": tail}}],
                }
                yield f"data: {json.dumps(tail_obj, ensure_ascii=False)}\n\n".encode()
            # 还原过调用但上游从未给收尾帧 → 补一帧，保证客户端不会把
            # 「工具调用回合」读成「模型只说了话」。
            if self._synth_idx > 0 and not self._tool_finish_sent:
                yield f"data: {json.dumps(_finish_frame({}, 'tool_calls'), ensure_ascii=False)}\n\n".encode()
        # 保证恰好一个 [DONE]（上游漏发时兜底）。
        yield b"data: [DONE]\n\n"
