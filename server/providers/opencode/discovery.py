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
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES
from net.proxy import proxies_for
from providers import models_snapshot
from providers.capabilities import as_flag
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
) -> tuple[dict[str, ModelRecord], list[str] | None, list[str], list[dict[str, Any]]]:
    """返回 (records, available, errors, snapshot_models)。available 为 None 表示清单
    从未拉到（无凭证或请求失败），调用方视作"可用性未知"。

    snapshot_models 是能力目录里本网关服务的那部分模型投影成的**统一核心字段条目**
    （见 models_snapshot.core_entry），供 data/models.json 快照使用。
    """
    errors: list[str] = []

    records: dict[str, ModelRecord] = {}
    snapshot_models: list[dict[str, Any]] = []
    try:
        providers = _fetch_capabilities()
        records, snapshot_models = _parse_capabilities(providers)
    except Exception as exc:
        message = f"capability catalog: {exc}"
        errors.append(message)
        logger.warning(f"[opencode] 能力目录拉取失败: {type(exc).__name__}: {exc}")

    available: list[str] | None = None
    if credential is not None:
        try:
            available = _fetch_model_list(upstream, credential)
        except Exception as exc:
            message = f"model list: {exc}"
            errors.append(message)
            logger.warning(f"[opencode] 上游模型清单拉取失败: {type(exc).__name__}: {exc}")

    return records, available, errors, snapshot_models


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


def _parse_capabilities(
    providers: dict[str, Any],
) -> tuple[dict[str, ModelRecord], list[dict[str, Any]]]:
    """返回 (归一化记录, 统一核心字段条目列表)。条目按 model_id 去重后保序。"""
    records: dict[str, ModelRecord] = {}
    snapshot_by_id: dict[str, dict[str, Any]] = {}
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
            snapshot_by_id[model_id] = _snapshot_entry(
                model_id, model, protocol, records[model_id].free
            )
    return records, list(snapshot_by_id.values())


def _snapshot_entry(
    model_id: str, model: dict[str, Any], protocol: str, free: bool
) -> dict[str, Any]:
    """models.dev 条目 → models.json 统一核心字段（见 models_snapshot.core_entry）。

    protocol / free 是路由必需项、max_input / structured_output 是详情弹窗要显示的
    字段：冷启动不再打上游，索引只能由快照重建，缺了它们重启后模型不可路由或字段
    全空（见 record_from_snapshot 的逆向投影）。
    """
    limit = model.get("limit") if is_object(model.get("limit")) else {}
    modalities = model.get("modalities") if is_object(model.get("modalities")) else {}
    inputs = modalities.get("input") if isinstance(modalities.get("input"), list) else None
    outputs = modalities.get("output") if isinstance(modalities.get("output"), list) else None
    name = model.get("name")
    description = model.get("description")
    efforts: list[str] = []
    options = model.get("reasoning_options")
    if isinstance(options, list):
        for option in options:
            if is_object(option) and option.get("type") == "effort":
                values = option.get("values")
                if isinstance(values, list):
                    efforts.extend(str(v) for v in values)
    extras = {
        "max_input": limit.get("input"),
        "structured_output": model.get("structured_output"),
    }
    extras = {key: value for key, value in extras.items() if value is not None}
    return models_snapshot.core_entry(
        id=model_id,
        name=name if isinstance(name, str) and name else model_id,
        description=description if isinstance(description, str) else None,
        context_window=limit.get("context"),
        max_output_tokens=limit.get("output"),
        input_modalities=[str(t) for t in inputs] if inputs is not None else None,
        output_modalities=[str(t) for t in outputs] if outputs is not None else None,
        supports_images=("image" in inputs) if inputs is not None else None,
        supports_reasoning=as_flag(model.get("reasoning")),
        supports_tool_call=as_flag(model.get("tool_call")),
        reasoning_efforts=efforts or None,
        protocol=protocol,
        free=free,
        extras=extras or None,
    )


def record_from_snapshot(entry: dict[str, Any]) -> ModelRecord | None:
    """快照条目 → ModelRecord（冷启动重建索引，语义等价 discovery 的产出）。

    protocol 不在快照里（老版本快照）就重建不了路由 → 返回 None，调用方据此决定
    丢弃还是补一次拉取。metadata 按 models.dev 的驼峰键名重建（list_models 与
    管理台都按那套键读）。
    """
    protocol = str(entry.get("protocol") or "")
    if protocol not in (PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES):
        return None
    metadata = empty_metadata()
    context = entry.get("context_window")
    if isinstance(context, int):
        metadata["contextWindow"] = context
    output = entry.get("max_output_tokens")
    if isinstance(output, int):
        metadata["maxOutput"] = output
    extras = entry.get("extras") if is_object(entry.get("extras")) else {}
    if isinstance(extras.get("max_input"), int):
        metadata["maxInput"] = extras["max_input"]
    if isinstance(entry.get("name"), str):
        metadata["name"] = entry["name"]
    if isinstance(entry.get("description"), str):
        metadata["description"] = entry["description"]
    inputs = entry.get("input_modalities")
    if isinstance(inputs, list):
        metadata["inputModalities"] = inputs
    outputs = entry.get("output_modalities")
    if isinstance(outputs, list):
        metadata["outputModalities"] = outputs
    metadata["reasoning"] = entry.get("supports_reasoning") is True
    metadata["toolCall"] = entry.get("supports_tool_call") is True
    metadata["structuredOutput"] = extras.get("structured_output") is True
    efforts = entry.get("reasoning_efforts")
    if isinstance(efforts, list) and efforts:
        metadata["reasoningOptions"] = efforts
    return ModelRecord(
        protocol=protocol,
        metadata=metadata,
        free=entry.get("free") is True,
    )


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
