"""模型能力档案机制：把「哪家上游能吃什么」从共享代码里挪出来，做成 provider 的声明。

三层数据流（/v1/models 的能力块）：
    provider 目录声明  >  provider 档案默认  >  省略

「省略」是刻意的第三档：**「不知道」和「确定没有」是两回事**——`supports_video_input:
false` 会让客户端把视频入口藏起来，而没有任何依据时不该做这种断言。所以档案里用
None（或干脆不写）表示「不知道」，机制只负责把有依据的值输出。

两层能力也要分开（2026-10-12 那轮实测学到的）：
- **传输层**（`accepted_parts`）：上游会不会因为这种 content part 直接报错。
  实测 workbuddy：`file` → 400；`video_url` / `input_audio` → 200。parts.py 只按这个
  集合决定「放行 / 无损改写 / 降级」，不认识任何厂商名。
- **模型层**（模态与 `supports_*`）：part 能送到也不代表模型能用——实测 workbuddy
  收下 `video_url` 却告诉模型「不支持视频」。这一层由目录元数据 + provider 档案表达。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gateway.types import PROTOCOL_CHAT

# 模态 ↔ chat content part 类型：传输层能不能送达看这张表。
MODALITY_PARTS: Mapping[str, str] = {
    "text": "text",
    "image": "image_url",
    "video": "video_url",
    "audio": "input_audio",
    "pdf": "file",
}


@dataclass(frozen=True)
class UpstreamProfile:
    """一个 provider 的上游档案：字段即结论，结论必须带证据（evidence，含日期）。"""

    name: str
    # 传输层：上游接受的 chat content part 类型；不在集合里的 part 由 parts.py
    # 归一（能无损升级时）或降级成省略说明。
    accepted_parts: frozenset[str] = frozenset({"text", "image_url"})
    # 模型层默认值：目录元数据没声明时用。None / 缺省 = 不知道，不输出。
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # 证据与日期：重测一个上游只改这里，不动共享代码。
    evidence: str = ""


def as_flag(value: Any) -> bool | None:
    """目录元数据里的布尔位 → bool 或 None（「没声明」不落成 False）。"""
    return value if isinstance(value, bool) else None


def capability_block(
    profile: UpstreamProfile,
    protocol: str,
    declared: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 /v1/models 的能力块：declared（目录声明）> profile.defaults > 省略。

    模态再按传输层过滤一次：part 类型不在 accepted_parts 里的模态，厂商声明过也去掉
    ——送不达的能力不该写进声明；例外是协议自带原生载体的（anthropic 上游的
    `document` 就是 PDF 的原生形态，不经过 chat 的 part 白名单）。
    """
    merged: dict[str, Any] = {}
    for source in (profile.defaults, declared or {}):
        for key, value in source.items():
            if value is None:
                continue
            merged[key] = value
    modalities = merged.get("input_modalities")
    if isinstance(modalities, list):
        kept = [str(item) for item in modalities if _deliverable(str(item), profile, protocol)]
        merged["input_modalities"] = kept
        # 三个派生位以模态列表为依据；厂商单独声明过就不覆盖（setdefault）
        merged.setdefault("supports_vision", "image" in kept)
        merged.setdefault("supports_video_input", "video" in kept)
        merged.setdefault("supports_pdf_input", "pdf" in kept)
    return merged


def _deliverable(modality: str, profile: UpstreamProfile, protocol: str) -> bool:
    part = MODALITY_PARTS.get(modality)
    if part is None:
        return False
    if part in profile.accepted_parts:
        return True
    return modality == "pdf" and protocol != PROTOCOL_CHAT
