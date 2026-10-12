"""各 provider 的上游档案：结论 + 证据 + 日期。

共享代码（parts.py / capability_block）不认识任何厂商名；换上游、加 provider 只动
这个文件。每条结论都标明是**实测**还是**未验证**——未验证的按「part 原样透传 +
不替上游声明能力」处理，端点恢复、补完实测再收紧。
"""

from __future__ import annotations

from providers.capabilities import UpstreamProfile

# workbuddy（StepFun 系 relay）。2026-10-12 实测，账号 41c30f6b、模型 glb:step-5-preview：
#   - file part → 400（装图片 / 装文本 / 装 PDF 都一样）；
#   - video_url / input_audio → 200（传输层收得下）；
#   - 视频是模型层的死路：收下 video_url，但平台明确告诉模型「不支持视频」
#     （换成真实 http mp4 链接同样看不到，prompt_tokens 也没涨）；
#   - 对话中段的 role:system 消息模型照做（"从现在开始…"生效）→ 支持；
#   - response_format：json_schema / json_object 都被忽略（返回散文）→ 结构化输出不可用；
#   - tools 里的 web_search：StepFun 文档明确 step 系不支持（会回 unsupported_content_type）。
WORKBUDDY = UpstreamProfile(
    name="workbuddy",
    accepted_parts=frozenset({"text", "image_url", "video_url", "input_audio"}),
    defaults={
        "output_modalities": ["text"],
        "supports_video_input": False,
        "supports_pdf_input": False,
        "supports_structured_output": False,
        "supports_system_messages": True,
        "supports_web_search": False,
    },
    evidence="实测 2026-10-12：file→400；video_url→200 但模型看不到；response_format 被忽略",
)

# opencode（OpenCode Zen，多厂商聚合，模型目录来自 models.dev）。
# 2026-10-12 实测（模型 oc:big-pickle；其它 free 端点多数时间 429，样本有限）：
#   - text → 200；image_url → 200（模型准确答出图片颜色，尽管目录把 big-pickle 声明成纯文本）；
#   - file（装文本 / 装 PDF）→ 400；video_url → 400；input_audio → 400。
# 所以传输层只认 text / image_url：file、视频、音频在 chat 方言上会被上游直接拒。
# 目录里声明的 video / pdf / audio 模态因此会在能力块里被过滤掉——送不达的能力不声明。
# 样本限制：只测到一个模型，且它与目录声明有出入（目录说纯文本，实测能看图）；
# 下一轮验证时扩大样本，并把差异单独记一笔。
OPENCODE = UpstreamProfile(
    name="opencode",
    accepted_parts=frozenset({"text", "image_url"}),
    defaults={},
    evidence="实测 2026-10-12（big-pickle）：text/image_url→200；file/video_url/input_audio→400",
)
