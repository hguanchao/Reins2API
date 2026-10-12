"""协议化错误响应（移植自 zen2api app/errors.ts）。

每个外部协议有自己的错误信封：Anthropic 在 "error" 下嵌套一个带类型的错误，
OpenAI 形协议（chat / responses）用标准 error 对象。TS 的 sendError 依赖
express Response，Python 侧由调用方拿信封 + 状态码自行写出。
"""

from __future__ import annotations

from gateway.types import PROTOCOL_ANTHROPIC


class BridgeError(Exception):
    """协议桥的统一异常。

    对应 TS 里 bridge 各处裸抛的 Error：请求侧（decode/encode 校验失败）与
    响应侧（上游响应无法表示）共用一个类型，由调用方按阶段映射状态码
    （TS inference.ts：prepareRequest 失败 → 400，convertResponse 失败 → 502）。
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
