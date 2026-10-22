# 空回复判定：上游"正常结束"却什么都没说时拒绝放行。
#
# 三条件（对照 magpie 的 emptyFails / repliesCounted / uncounted / answersNothing）：
#   1. 回复没说任何实质内容（无文本、无思考、无工具块）；
#   2. 没有计任何 token（计了费说明上游真的处理过内容）；
#   3. 停止原因不是模型自己的意思（length / content_filter 是"被截断/被拦"，
#      说明有东西没放出来，如实透传；其余停止原因配上零内容即可疑）。
#
# 三条件全占的"完成"几乎总是上游线路的坏形态（代理吞内容、网关中途故障），
# 交出去会让客户端静默结束回合且不重试。拒绝它比转交它好。

from __future__ import annotations

from gateway.types import BridgeResponse, Usage, counted_nothing


def empty_reply_stop(stop: str) -> bool:
    """这个停止原因下的空回复需要被拒绝：不是模型主动收尾的两种停止都算。"""
    return stop not in ("length", "content_filter")


def assert_not_empty(said_anything: bool, stop: str, usage: Usage) -> None:
    """空回复守卫：三条件全占即抛 EmptyReply，由调用方翻成协议化错误。"""
    from gateway.stream.errors import EmptyReply

    if not said_anything and counted_nothing(usage) and empty_reply_stop(stop):
        raise EmptyReply(
            "an empty reply (the model answered nothing and counted no tokens; try again)"
        )


def response_said_anything(response: BridgeResponse) -> bool:
    """完整响应是否说过实质内容（文本/思考/可用工具块）。"""
    has_tools = any(tool.name.strip() != "" for tool in response.tools)
    has_reasoning = any(
        (block.text.strip() != "" or block.encrypted != "") for block in response.reasoning
    )
    return response.text.strip() != "" or has_reasoning or has_tools
