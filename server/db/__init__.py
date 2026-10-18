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
from datetime import datetime
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import BEIJING_TZ as _BEIJING_TZ
from core.settings import DATA_DIR
from db.schema import ACCOUNT_INDEX_SQL, ADDED_COLUMNS, DDL, LEGACY_INDEXES, RENAMED_COLUMNS


def beijing_now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def beijing_ts(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, _BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


# 明细排序白名单：外部只传键，SQL 片段在这里映射——排序方向也由参数决定，
# 不把调用方的字符串拼进 SQL。键集与前端可点击的表头一一对应（不留够不到的键）。
# 同值过多（比如一堆 0 耗时）时统一用 id 兜底排序，否则分页边界上会出现
# "同一条重复出现 / 漏掉"。
_SORT_COLUMNS: dict[str, str] = {
    "time": "id",
    "tokens": "input_tokens + output_tokens",
    "duration": "duration_ms",
    "status": "http_status",
}
_STATUS_RANGES: dict[str, tuple[int, int]] = {"2xx": (200, 299), "4xx": (400, 499), "5xx": (500, 599)}


def _escape_like(term: str) -> str:
    """LIKE 的 % / _ 是通配符：用户输入里出现它们要当字面量（搜 "a_b" 不该命中 "axb"）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def request_where(status: str = "", model: str = "", account: str = "") -> tuple[str, list[Any]]:
    """明细筛选 → (WHERE 片段, 参数)。

    status 取值：空 / all（不筛）· failed（非 2xx）· 2xx / 4xx / 5xx · 具体码（429）。
    无法识别的取值一律当"不筛"——筛选条件宁可放宽也不能把日志筛成空。
    账号按**前缀**匹配（uid 是几十字符的长串，子串匹配既没意义又扫全表）。
    """
    clauses: list[str] = []
    values: list[Any] = []
    key = (status or "").strip().lower()
    if key and key != "all":
        if key == "failed":
            clauses.append("(http_status < 200 OR http_status >= 300)")
        elif key in _STATUS_RANGES:
            low, high = _STATUS_RANGES[key]
            clauses.append("http_status BETWEEN ? AND ?")
            values.extend([low, high])
        elif key.isdigit():
            clauses.append("http_status = ?")
            values.append(int(key))
    term = (model or "").strip()
    if term:
        pattern = f"%{_escape_like(term)}%"
        clauses.append("(model LIKE ? ESCAPE '\\' OR upstream_model LIKE ? ESCAPE '\\')")
        values.extend([pattern, pattern])
    term = (account or "").strip()
    if term:
        clauses.append("account_uid LIKE ? ESCAPE '\\'")
        values.append(f"{_escape_like(term)}%")
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), values


@dataclass
class RequestRecord:
    """字段顺序与建表 DDL 保持一致（主键 → 标识 → 路由 → 特征 → 用量 → 来源 → 结果 → 时间）。"""

    # 主键与标识（id 是请求 ID，落到 request_id 列）
    id: str = ""
    session_id: str = ""
    # 路由
    model: str = ""                # 规范名 opencode/xxx
    upstream_model: str = ""       # 实际打到上游的裸模型名
    client_protocol: str = "chat"  # 客户端协议
    upstream_protocol: str = "chat"
    account_uid: str = ""          # 服务本请求的账号（号池 provider 填写；遥测分账用）
    # 请求特征
    is_free: bool = False
    is_stream: bool = False        # 客户端是否要流式
    is_routed: bool = False        # 是否真正到达路由/上游尝试
    is_shaped: bool = False        # 请求体是否被免费档塑形改动
    reasoning_effort: str = ""     # 客户端声明的思考强度档位
    # 用量
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    # 来源
    client_ip: str = ""            # 发起端 IP（优先 X-Forwarded-For/X-Real-IP）
    egress_ip: str = ""            # 出口 IP：上游看到的地址（探测缓存回填；空 = 当时还没探到）
    client_ua: str = ""            # 客户端 User-Agent 原文
    # 结果
    first_byte_ms: int = 0         # 首字耗时（上游首个非空字节，0 = 未采到/失败请求）
    duration_ms: int = 0
    error_message: str | None = None
    http_status: int = 0           # 上游 HTTP 状态码（0 = 未到达上游）
    # 时间
    created_ms: int = 0            # 创建时刻 epoch 毫秒（索引/范围查询用）
    created_at: str = ""           # 创建时刻北京时间字符串（展示用）

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))


# 全字段插入（created_at 由 log_request 按北京时间算好）；record.id 是请求 ID 字符串
# （msg_xxx），映射到 request_id 列，行主键 id 走自增不参与插入。
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
            # ① 列改名（旧库）。必须排在 executescript(DDL) 之前：DDL 里的
            #    CREATE INDEX 指着新列名，旧库此刻还没有这个列。
            cols = self._table_columns()
            for old, new in RENAMED_COLUMNS:
                if old in cols and new not in cols:
                    self._conn.execute(
                        f'ALTER TABLE requests RENAME COLUMN "{old}" TO "{new}"'
                    )
                    cols.discard(old)
                    cols.add(new)
            for index in LEGACY_INDEXES:
                self._conn.execute(f"DROP INDEX IF EXISTS {index}")
            # ② 建表（新库）+ 建索引（新库与迁移后的旧库，均幂等）
            self._conn.executescript(DDL)
            # ③ 补齐历史上按增量演进、老库可能整列缺失的字段
            cols = self._table_columns()
            for column, definition in ADDED_COLUMNS:
                if column not in cols:
                    self._conn.execute(
                        f"ALTER TABLE requests ADD COLUMN {column} {definition}"
                    )
            # ④ 依赖补列的索引（account_uid）在补列之后创建
            for statement in ACCOUNT_INDEX_SQL:
                self._conn.execute(statement)
            # ⑤ meta 表已废弃：只记过一个从没人读的 schema_version
            self._conn.execute("DROP TABLE IF EXISTS meta")
            self._conn.commit()

    def _table_columns(self) -> set[str]:
        """当前 requests 表的列名集合；表还不存在时返回空集。"""
        return {row[1] for row in self._conn.execute("PRAGMA table_info(requests)")}

    # ── 写入（失败只告警，绝不抛给调用方） ──

    def log_request(self, record: RequestRecord) -> None:
        if not record.created_at:
            record.created_at = beijing_ts(record.created_ms)
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
            logger.warning(f"[遥测] provider={self.name} 请求落库失败(不阻塞转发): {type(exc).__name__}: {exc}")

    # ── 读取 ──

    def window_rows(self, since_ms: int) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM requests WHERE created_ms >= ? ORDER BY created_ms",
                    (since_ms,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[遥测] provider={self.name} 窗口查询失败: {exc}")
            return []

    def recent(
        self,
        limit: int = 20,
        offset: int = 0,
        *,
        status: str = "",
        model: str = "",
        account: str = "",
        sort: str = "time",
        desc: bool = True,
    ) -> tuple[int, list[dict[str, Any]]]:
        """明细分页（可筛选、可排序）。

        筛选放在 SQL 侧而不是让前端过滤当前页：日志是分页的，只筛当前页会给出
        "这批里就这几条 429"的假象（真实数量可能在别的页里）。total 用同一套
        条件计数，所以分页条数跟着筛选结果走。
        """
        where, values = request_where(status, model, account)
        column = _SORT_COLUMNS.get(sort or "time", _SORT_COLUMNS["time"])
        direction = "ASC" if not desc else "DESC"
        # 主键本来就是 id：再叠一个 id 兜底会拼出 "ORDER BY id DESC, id DESC"。
        tiebreak = "" if column == "id" else ", id DESC"
        try:
            with self._lock:
                total = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM requests{where}", tuple(values)
                ).fetchone()["n"]
                rows = self._conn.execute(
                    f"SELECT * FROM requests{where} ORDER BY {column} {direction}{tiebreak}"
                    " LIMIT ? OFFSET ?",
                    (*values, limit, offset),
                ).fetchall()
            return int(total), [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[遥测] provider={self.name} 明细查询失败: {exc}")
            return 0, []

    def account_totals(self) -> dict[str, dict[str, int]]:
        """按账号的**全时段**聚合（历史用量列）。

        在 SQL 侧 GROUP BY，不把整表读进内存——遥测只进不出，表会一直长。
        返回 {uid: {requests, failed, input_tokens, output_tokens, cached_tokens,
        last_used_ms}}。
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT account_uid,"
                    " COUNT(*) AS requests,"
                    " SUM(CASE WHEN http_status < 200 OR http_status >= 300 THEN 1 ELSE 0 END) AS failed,"
                    " SUM(input_tokens) AS input_tokens,"
                    " SUM(output_tokens) AS output_tokens,"
                    " SUM(cached_tokens) AS cached_tokens,"
                    " MAX(created_ms) AS last_used_ms"
                    " FROM requests WHERE account_uid != '' GROUP BY account_uid"
                ).fetchall()
        except Exception as exc:
            logger.warning(f"[遥测] provider={self.name} 全时段聚合失败: {exc}")
            return {}
        return {
            str(row["account_uid"]): {
                "requests": int(row["requests"] or 0),
                "failed": int(row["failed"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cached_tokens": int(row["cached_tokens"] or 0),
                "last_used_ms": int(row["last_used_ms"] or 0),
            }
            for row in rows
        }

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
