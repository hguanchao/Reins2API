"""模型发现（移植自 zen2api routing/discovery.ts）：拉两个来源并解析成索引记录。

  * {upstream}/v1/models —— 凭证可见清单，可用性的权威来源：缺在这里的模型
    上游会拒绝 "not supported"，绝不能对外广告。
  * 能力目录（models.opencode.ai/api.json）—— 每个模型背后的 SDK 就是协议声明，
    外加上下文限额与 reasoning/tool 标志。

任一来源失败都上报但不致命：调用方保留已有目录。
"""

from __future__ import annotations

import re
from typing import Any

from curl_cffi import requests as crequests

from core.logger import logger
from gateway.jsonutil import is_object
from net.proxy import proxies_for
from providers.opencode.routing import (
    CAPABILITIES_URL,
    ModelRecord,
    empty_metadata,
    is_zen_provider,
    protocol_for_sdk,
)

DISCOVERY_TIMEOUT = 30


def discover(
    upstream: str,
    credential: str | None,
) -> tuple[dict[str, ModelRecord], list[str] | None, list[str]]:
    """返回 (records, available, errors)。available 为 None 表示清单从未拉到
    （无凭证或请求失败），调用方视作"可用性未知"。"""
    errors: list[str] = []

    records: dict[str, ModelRecord] = {}
    try:
        providers = _fetch_capabilities()
        records = _parse_capabilities(providers)
    except Exception as exc:
        message = f"capability catalog: {exc}"
        errors.append(message)
        logger.warning(f"[目录] 能力目录拉取失败: {message}")

    available: list[str] | None = None
    if credential is not None:
        try:
            available = _fetch_model_list(upstream, credential)
        except Exception as exc:
            message = f"model list: {exc}"
            errors.append(message)
            logger.warning(f"[目录] 上游模型清单拉取失败: {message}")

    return records, available, errors


def _fetch_capabilities() -> dict[str, Any]:
    response = crequests.get(
        CAPABILITIES_URL,
        headers={"accept": "application/json"},
        timeout=DISCOVERY_TIMEOUT,
        proxies=proxies_for(CAPABILITIES_URL),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    if not is_object(payload):
        raise RuntimeError("invalid capability catalog payload")
    return payload


def _fetch_model_list(upstream: str, credential: str) -> list[str]:
    url = f"{upstream.rstrip('/')}/v1/models"
    response = crequests.get(
        url,
        headers={
            "authorization": f"Bearer {credential}",
            "accept": "application/json",
            "x-opencode-client": "cli",
        },
        timeout=DISCOVERY_TIMEOUT,
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    data = payload.get("data") if is_object(payload) else None
    ids = [str(item.get("id") or "") for item in (data or []) if is_object(item)]
    ids = [i for i in ids if i]
    if not ids:
        raise RuntimeError("empty model list")
    return ids


def _parse_capabilities(providers: dict[str, Any]) -> dict[str, ModelRecord]:
    records: dict[str, ModelRecord] = {}
    for provider_id, provider in providers.items():
        if not is_object(provider):
            continue
        if not is_zen_provider(str(provider_id), str(provider.get("api") or "")):
            continue
        models = provider.get("models")
        if not is_object(models):
            continue
        for model_key, model in models.items():
            if not is_object(model):
                continue
            model_id = str(model.get("id") or "") or str(model_key)
            npm = str(model.get("provider", {}).get("npm") or "") if is_object(model.get("provider")) else ""
            protocol = protocol_for_sdk(npm or str(provider.get("npm") or ""))
            if protocol is None:
                continue
            records[model_id] = ModelRecord(
                protocol=protocol,
                metadata=_metadata_for(model),
                free=_is_free(model, model_id),
            )
    return records


def _metadata_for(model: dict[str, Any]) -> dict[str, Any]:
    metadata = empty_metadata()
    limit = model.get("limit")
    if is_object(limit):
        metadata["contextWindow"] = int(limit.get("context") or 0)
        metadata["maxInput"] = int(limit.get("input") or 0)
        metadata["maxOutput"] = int(limit.get("output") or 0)
    metadata["reasoning"] = model.get("reasoning") is True
    # reasoning_options 归一化为字符串档位:effort 取 values(如 low/medium/high),toggle 等记类型名
    options = model.get("reasoning_options")
    if isinstance(options, list):
        levels: list[str] = []
        for option in options:
            if is_object(option):
                values = option.get("values")
                if option.get("type") == "effort" and isinstance(values, list) and values:
                    levels.extend(str(v) for v in values)
                else:
                    levels.append(str(option.get("type") or "on"))
            elif option:
                levels.append(str(option))
        metadata["reasoningOptions"] = levels
    metadata["toolCall"] = model.get("tool_call") is True
    metadata["structuredOutput"] = model.get("structured_output") is True
    modalities = model.get("modalities")
    if is_object(modalities):
        if isinstance(modalities.get("input"), list):
            metadata["inputModalities"] = modalities["input"]
        if isinstance(modalities.get("output"), list):
            metadata["outputModalities"] = modalities["output"]
    # 透传展示用字段（TS 版丢弃；本项目管理台要显示，属增强偏差）
    if isinstance(model.get("name"), str):
        metadata["name"] = model["name"]
    if isinstance(model.get("description"), str):
        metadata["description"] = model["description"]
    return metadata


def _is_free(model: dict[str, Any], model_id: str) -> bool:
    """优先已发布价格：调用不要钱就是免费档，与名字无关；
    目录未标价时才回退 -free 命名惯例。"""
    cost = model.get("cost")
    if is_object(cost):
        return float(cost.get("input") or 0) == 0 and float(cost.get("output") or 0) == 0
    return bool(re.search("free", model_id, re.IGNORECASE))
