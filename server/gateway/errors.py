"""协议化错误响应。

每个外部协议有自己的错误信封：Anthropic 在 "error" 下嵌套一个带类型的错误，
OpenAI 形协议（chat / responses）用标准 error 对象。本模块只产出信封 body，
HTTP 状态码由调用方自行写出。
"""

from __future__ import annotations

from gateway.types import PROTOCOL_ANTHROPIC


class BridgeError(Exception):
    """协议桥的统一异常。

    桥内部各处的失败统一成这一种异常：请求侧（decode/encode 校验失败）与
    响应侧（上游响应无法表示）共用一个类型，由调用方按阶段映射状态码
    （请求侧 prepare_request 失败 → 400，响应侧 convert_response 失败 → 502）。
    """

    def __init__(self, message: str = "bridge failure") -> None:
        super().__init__(message)


def error_payload(protocol: str, message: str, kind: str) -> dict:
    """按协议生成错误信封 body（HTTP 状态码由调用方持有，信封里不含它）。

    Anthropic 嵌套 {"type":"error","error":{"type":kind,"message":message}}；
    OpenAI 形协议扁平 {"error":{"message","type":kind,"param":None,"code":None}}。
    流式错误帧与整包错误响应共用本函数——此前两处各手抄一份信封，会漂移。
    """
    if protocol == PROTOCOL_ANTHROPIC:
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "param": None, "code": None}}
