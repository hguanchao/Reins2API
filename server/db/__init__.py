"""分库遥测存储：每个 provider 一个 data/<name>.db（文件即命名空间）。

- requests 表：一条请求一行，用量与请求日志同源（数字永不漂移）。
- 无保留期清理（KEEP_DAYS 已去掉），只进不出；涨大了以后再加。
- 落库失败绝不阻塞转发：log_request 吞异常只告警。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR
from db.schema import DDL, SCHEMA_VERSION

_BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def beijing_ts(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, _BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class RequestRecord:
    id: str = ""
    at_ms: int = 0
    ts: str = ""
    model: str = ""            # 规范名 opencode/xxx
    upstream_model: str = ""
    endpoint: str = "chat"     # 客户端协议
    upstream_protocol: str = "chat"
    free: bool = False
    stream: bool = False
    routed: bool = False       # 是否真正到达路由/上游尝试
    shaped: bool = False
    status: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    error: str | None = None
    session: str = ""
    client_ip: str = ""        # 发起端 IP（优先 X-Forwarded-For/X-Real-IP）
    client: str = ""           # 客户端标识（X-Title 或 User-Agent）

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))


# 全字段插入（ts 由 log_request 按北京时间算好）；record.id 是请求 ID 字符串
# （req_xxx），映射到 request_id 列，行主键 id 走自增不参与插入。
_INSERT_COLUMNS = RequestRecord.columns()
_INSERT_SQL_COLUMNS = tuple("request_id" if c == "id" else c for c in _INSERT_COLUMNS)


class ProviderStore:
    """单个 provider 的独立 SQLite 库（WAL）。"""

    def __init__(self, name: str, path: Path | None = None):
        self.name = name
        self._path = path or (DATA_DIR / f"{name}.db")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(DDL)
            # 开发期列演进的轻量前向迁移（正式化后移入 schema_version 迁移链）
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(requests)")}
            if "request_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE requests ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
                )
            # 开发期列演进的轻量前向迁移（正式化后移入 schema_version 迁移链）
            for column in ("client_ip", "client"):
                if column not in cols:
                    self._conn.execute(
                        f"ALTER TABLE requests ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._conn.commit()

    # ── 写入（失败只告警，绝不抛给调用方） ──

    def log_request(self, record: RequestRecord) -> None:
        if not record.ts:
            record.ts = beijing_ts(record.at_ms)
        values = tuple(getattr(record, c) for c in _INSERT_COLUMNS)
        placeholders = ", ".join("?" for _ in _INSERT_SQL_COLUMNS)
        columns = ", ".join(_INSERT_SQL_COLUMNS)
        try:
            with self._lock:
                self._conn.execute(
                    f"INSERT INTO requests ({columns}) VALUES ({placeholders})",
                    values,
                )
                self._conn.commit()
        except Exception as exc:
            logger.warning(f"[遥测] {self.name} 落库失败（不阻塞转发）: {type(exc).__name__}: {exc}")

    # ── 读取 ──

    def window_rows(self, since_ms: int) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM requests WHERE at_ms >= ? ORDER BY at_ms",
                    (since_ms,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[遥测] {self.name} 窗口查询失败: {exc}")
            return []

    def recent(self, limit: int = 20, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        try:
            with self._lock:
                total = self._conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
                rows = self._conn.execute(
                    "SELECT * FROM requests ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return int(total), [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[遥测] {self.name} 明细查询失败: {exc}")
            return 0, []

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass


_stores: dict[str, ProviderStore] = {}


def init_stores(names: Iterable[str]) -> None:
    """为每个已注册 provider 打开其独立数据库（未启用的 provider 不创建文件）。"""
    for name in names:
        if name not in _stores:
            _stores[name] = ProviderStore(name)


def store(name: str) -> ProviderStore:
    return _stores[name]


def stores() -> dict[str, ProviderStore]:
    return dict(_stores)
