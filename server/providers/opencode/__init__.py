"""OpenCode Zen 上游接入（唯一启用的 provider）。

组装路由/发现/上游/身份各模块，实现 gateway.base.Provider 契约。
凭证体系已按项目决策砍掉：单一 public 凭证、恒匿名档；发送只在"响应头之前"的传输
失败上重试一次（见 upstream.send_upstream），其余错误一律交推理层透传。
"""

from __future__ import annotations

import threading
from typing import Any

from core.logger import logger
from gateway.base import ModelInfo, RequestIDs, Route, UpstreamResponse
from providers import models_dev, profiles
from providers.capabilities import as_flag, capability_block
from providers.common import refresh_result, stamp, stamp_now
from providers.identity import derive_request_ids
from providers.opencode.discovery import discover
from providers.opencode.routing import ModelIndex, ModelRecord, Router
from providers.opencode.upstream import (
    INFERENCE_BASE,
    ZEN_BASE,
    send_upstream,
    shape_free_request_body,
)


class OpenCodeProvider:
    name = "opencode"
    upstream_url = INFERENCE_BASE

    def __init__(self) -> None:
        self._index = ModelIndex()
        self._router = Router(self._index)
        self._lock = threading.Lock()
        self._refreshed_at: str | None = None
        # 快照内容的最后变更时间（models.json 的 meta.updated_at_ms）；本进程从快照
        # 冷启动时它是唯一的时间线索，与 refreshed_at（本进程刷新时间）分开。
        self._cached_at_ms = 0

    # ── 目录 ──

    def refresh(self) -> dict[str, Any]:
        """拉两源并合并；失败保留旧目录。

        返回 {added, removed, failed, error}（手动刷新是常规路径下唯一的触发点，
        失败要能回报给界面，不能只写日志；形状与另两家一致）。
        """
        from providers import models_snapshot
        from providers.opencode.upstream import PUBLIC_KEY

        with self._lock:
            # 差异算在**对外可服务的列表**上（router.list 的口径），不是全部协议：
            # 界面看到的、/v1/models 声明的都是前者，两套数字混用会出现
            # "新增 112、共 10 个"这种自相矛盾的提示。
            previous = {entry["id"] for entry in self._router.list()}
        records, available, errors, snapshot_models = discover(ZEN_BASE, PUBLIC_KEY)
        # 一条记录都没拿到 = 能力目录整源失败（文档大、超时或形状变了），不是"上游
        # 删光了模型"：不覆盖旧索引，且不当成刷新成功——否则日志会写"刷新完成
        # protocols=0"掩盖真故障，界面也拿不到失败回报。
        if not records:
            if previous:
                logger.warning(
                    f"[opencode] 能力目录拉取失败, 保留旧目录 models={len(previous)} "
                    f"errors={len(errors)}"
                )
            else:
                logger.warning(
                    f"[opencode] 能力目录拉取失败, 且没有可保留的旧目录 errors={len(errors)}"
                )
            return refresh_result(
                failed=len(errors) or 1, error="; ".join(errors) or "能力目录拉取失败"
            )
        with self._lock:
            self._index.replace(records)
            if available is not None:
                self._index.set_available(available)
            else:
                self._index.set_available_unknown()  # 清单没拉到：不过滤，别用旧清单挡新模型
            self._refreshed_at = stamp_now()
            counts = self._index.counts()
            listed = {entry["id"] for entry in self._router.list()}
        added = sorted(listed - previous)
        removed = sorted(previous - listed)
        # 统一核心字段条目落 data/models.json（能力目录拉失败时为空，不覆盖旧快照）；
        # 凭证可见清单随 meta 落盘：冷启动没有它就只能按"清单未知"兜底，会把凭证
        # 到不了的模型也对外声明。
        if snapshot_models:
            meta = {"available": available} if available is not None else None
            written = models_snapshot.write("opencode", snapshot_models, meta)
            cached_at_ms = int(models_snapshot.read_meta("opencode").get("updated_at_ms") or 0)
            with self._lock:
                self._cached_at_ms = cached_at_ms
            if written:
                logger.info("[opencode] 快照已更新(旧文件轮转为 models.json.bak)")
        detail = []
        if added:
            detail.append(f"新增={len(added)}({', '.join(added[:5])})")
        if removed:
            detail.append(f"移除={len(removed)}({', '.join(removed[:5])})")
        logger.info(
            f"[opencode] 模型目录刷新完成 protocols={counts['protocols']} "
            f"servable={len(listed)}"
            + (f" errors={len(errors)}" if errors else "")
            + f" {' '.join(detail) or '无变化'}"
        )
        return refresh_result(
            added=len(added),
            removed=len(removed),
            failed=len(errors),
            error="; ".join(errors),
        )

    def load_from_snapshot(self) -> int:
        """用上次落盘的快照重建模型索引（冷启动的唯一数据源），返回条目数。

        protocol 是路由必需项：老版本快照没有它就重建不了，此时返回 0，由调用方
        （main 的启动兜底）决定是否补一次拉取。凭证可见清单一并从 meta 恢复，避免
        重启后对外声明凭证到不了的模型。只在索引为空时填充。
        """
        from providers import models_snapshot
        from providers.opencode.discovery import record_from_snapshot

        with self._lock:
            if self._index.counts()["protocols"]:
                return 0
        raw = models_snapshot.read().get("opencode")
        if not isinstance(raw, list) or not raw:
            return 0
        records: dict[str, ModelRecord] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if not model_id:
                continue
            record = record_from_snapshot(entry)
            if record is not None:
                records[model_id] = record
        if not records:
            return 0
        meta = models_snapshot.read_meta("opencode")
        available = meta.get("available")
        with self._lock:
            if not self._index.counts()["protocols"]:
                self._index.replace(records)
                if isinstance(available, list) and available:
                    self._index.set_available([str(item) for item in available])
        with self._lock:
            self._cached_at_ms = int(meta.get("updated_at_ms") or 0)
        logger.info(
            f"[opencode] 目录从快照恢复 protocols={len(records)} "
            f"available={len(available) if isinstance(available, list) else '未知'}"
        )
        return len(records)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "upstream": INFERENCE_BASE,
                "refreshed_at": self._refreshed_at,
                "cached_at": stamp(self._cached_at_ms),
                "models": len(self._router.list()),
            }

    def list_models(self) -> list[ModelInfo]:
        # 索引读在锁内取快照（refresh 会整体替换索引）；构建在锁外做，避免持锁跑
        # models.dev 兜底查找。routing.list() 返回的是记录的副本，锁外读安全。
        with self._lock:
            entries = self._router.list()
        out: list[ModelInfo] = []
        for entry in entries:
            # 目录是 models.dev 的字段名（inputModalities / structuredOutput / …），
            # 只在这一层翻译成标准化能力名；缺省的字段交给上游档案，档案也没有就不输出。
            # metadata 是索引记录的副本（routing.list 拷过），就地补兜底值安全。
            metadata = entry["metadata"]
            filled = models_dev.fill_limits(
                entry["upstream_model"],
                metadata,
                context_key="contextWindow",
                output_key="maxOutput",
            )
            if filled:
                metadata["limits_source"] = {key: "models.dev" for key in filled}
            out.append(
                ModelInfo(
                    id=entry["id"],
                    upstream_model=entry["upstream_model"],
                    protocol=entry["protocol"],
                    free=entry["free"],
                    display_name=str(metadata.get("name") or ""),
                    capabilities=capability_block(
                        profiles.OPENCODE,
                        entry["protocol"],
                        {
                            "input_modalities": metadata.get("inputModalities"),
                            "output_modalities": metadata.get("outputModalities"),
                            "supports_reasoning": as_flag(metadata.get("reasoning")),
                            "supports_function_calling": as_flag(metadata.get("toolCall")),
                            "supports_structured_output": as_flag(metadata.get("structuredOutput")),
                            "context_window": metadata.get("contextWindow"),
                            "max_input_tokens": metadata.get("maxInput"),
                            "max_output_tokens": metadata.get("maxOutput"),
                        },
                    ),
                    metadata=metadata,
                )
            )
        return out

    # ── 路由 ──

    def supported(self, model: str) -> bool:
        with self._lock:
            return self._router.supported(self._bare(model))

    def route(self, model: str) -> Route:
        with self._lock:
            return self._router.route(self._bare(model))

    def _bare(self, model: str) -> str:
        """别名 oc:xxx（及旧式 opencode/xxx）→ 上游裸名。"""
        return model.removeprefix("oc:").removeprefix(f"{self.name}/")

    # ── 请求 ──

    def derive_ids(
        self, headers: dict[str, str], body: dict[str, Any], client_ip: str = ""
    ) -> RequestIDs:
        return derive_request_ids(headers, body, client_ip)

    def shape(self, body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
        return shape_free_request_body(body, protocol)

    def send(
        self,
        route: Route,
        protocol: str,
        body: bytes,
        ids: RequestIDs,
        *,
        stream: bool,
    ) -> UpstreamResponse:
        return send_upstream(protocol, body, ids, stream=stream)
