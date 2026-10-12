"""各 provider 的上游档案：声明「它收哪些 part」与「目录没声明时能力位填什么」。

取值依据（探针与日期）记录在 `tests/upstream_evidence.md`——生产代码这里不写实验
明细；重测后先改那份记录，再回来同步这两个字段。没有实测依据的项不要猜：
`accepted_parts` 只放验证过的类型（漏了会让可用 part 被降级，多了会把 400 透给
客户端），`defaults` 里拿不准就不写（不声明 ≠ 声明 false）。
"""

from __future__ import annotations

from providers.capabilities import UpstreamProfile

# workbuddy：传输层只认 text / image_url / video_url / input_audio；
# 视频收得下但模型层不可用（平台对模型关闭），结构化输出与 web_search 同理不可用。
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
)

# opencode：chat 路线只认 text / image_url；走 responses 的模型另有 PDF 通道。
# 服务端能力位不进 defaults——它的模型目录自带 models.dev 的声明，交给目录说。
OPENCODE = UpstreamProfile(
    name="opencode",
    accepted_parts=frozenset({"text", "image_url"}),
    defaults={},
)

# codex：上游是 OpenAI Responses 协议（不是 chat 方言），part 归一由桥的 responses
# 编码器负责，这里只声明传输层与目录缺省的能力位。PDF 走 Responses 原生 input_file，
# 不经 chat 的 part 白名单，故不列在 accepted_parts 里（capabilities._deliverable
# 对非 chat 协议放行 pdf）。
CODEX = UpstreamProfile(
    name="codex",
    accepted_parts=frozenset({"text", "image_url"}),
    defaults={
        "output_modalities": ["text"],
        "supports_reasoning": True,
        "supports_function_calling": True,
        "supports_system_messages": True,
    },
)
