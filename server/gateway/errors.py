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


def error_payload(protocol: str, status: int, message: str, kind: str) -> dict:
    """TS errorPayload 的移植：按协议生成错误信封 body。

    Anthropic 嵌套 {"type":"error","error":{"type":kind,"message":message}}；
    OpenAI 形协议扁平 {"error":{"message","type","param":None,"code":None}}。
    status 与 TS 签名同序保留，HTTP 状态码由调用方持有（返回值只含信封）。
    """
    if protocol == PROTOCOL_ANTHROPIC:
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "param": None, "code": None}}
