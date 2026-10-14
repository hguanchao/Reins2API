"""账号表存储共享内核（codex / workbuddy 两个号池的 SQLite 管道）。

两个 provider 的 accounts 表列不同、Account 数据类不同，但「连接 + PRAGMA +
DDL + 幂等补列 + CRUD + 运行态增量更新 + 单调序号 + 状态计数」这套管道逐字相同。
此前各自维护一份（改一处漏一处），这里收成一个基类，子类只声明差异：

    db_filename / log_tag / columns / runtime_fields / json_fields
    ddl / added_columns / uid_re / _row_to_account

`update_tokens` 的列集各 provider 不同（codex 有 id_token/plan_type，workbuddy 有
domain→realm），留给子类；它们用基类的 `_write` 落库。

线程模型是 ThreadingHTTPServer（线程/请求），全部读写经一把 RLock 串行化；
sqlite 全程参数化查询；uid 白名单防路径穿越/注入。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

# 账号级生命周期状态（与冷却/熔断正交）：active 可派号；paused 人工暂停；
# disabled 终态（session 失效/refresh 耗尽），需人工 revive。
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DISABLED = "disabled"


class AccountStore:
    """accounts 表的读写入口（独立连接，与 requests 的 ProviderStore 并存）。"""

    db_filename: str = ""
    log_tag: str = "accounts"
    columns: tuple[str, ...] = ()
    runtime_fields: frozenset[str] = frozenset()
    json_fields: frozenset[str] = frozenset()
    ddl: str = ""
    added_columns: tuple[tuple[str, str], ...] = ()
    uid_re: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, path: Path | None = None):
        self._path = path or (DATA_DIR / self.db_filename)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(self.ddl)
            self._migrate()
            self._conn.commit()

    # ── 子类实现 ──

    def _row_to_account(self, row: sqlite3.Row) -> Any:
        raise NotImplementedError

    # ── 低层读写（全部持锁）──

    def _fetchall(self, sql: str, values: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, values).fetchall()

    def _fetchone(self, sql: str, values: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, values).fetchone()

    def _write(self, sql: str, values: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            return cursor

    def _encode(self, key: str, value: Any) -> Any:
        """JSON 字段落库前序列化（dict → 紧凑 JSON；None 归一成 {}）。"""
        if key in self.json_fields:
            return json.dumps(value or {}, ensure_ascii=False)
        return value

    def _migrate(self) -> None:
        """给老库补 added_columns（调用方持锁）。"""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(accounts)")}
        for name, decl in self.added_columns:
            if name in existing:
                continue
            self._conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")
            logger.info(f"[{self.log_tag}] accounts 表补列 column={name}")

    def valid_uid(self, uid: str) -> bool:
        return isinstance(uid, str) and bool(self.uid_re.match(uid))

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # ── 读 ──

    def list_accounts(self) -> list[Any]:
        rows = self._fetchall(
            f"SELECT {', '.join(self.columns)} FROM accounts ORDER BY created_ms, uid"
        )
        return [self._row_to_account(row) for row in rows]

    def get_account(self, uid: str) -> Any | None:
        if not self.valid_uid(uid):
            return None
        row = self._fetchone(
            f"SELECT {', '.join(self.columns)} FROM accounts WHERE uid = ?", (uid,)
        )
        return self._row_to_account(row) if row is not None else None

    # ── 写 ──

    def upsert_account(self, account: Any) -> None:
        """插入或覆盖凭证/基础字段（面板导入、OAuth 登录）。运行态字段随行写入。"""
        now_ms = int(time.time() * 1000)
        if not account.created_ms:
            account.created_ms = now_ms
        account.updated_ms = now_ms
        values = tuple(self._encode(c, getattr(account, c)) for c in self.columns)
        self._write(
            f"INSERT OR REPLACE INTO accounts ({', '.join(self.columns)}) "
            f"VALUES ({', '.join('?' for _ in self.columns)})",
            values,
        )

    def delete_account(self, uid: str) -> bool:
        if not self.valid_uid(uid):
            return False
        cursor = self._write("DELETE FROM accounts WHERE uid = ?", (uid,))
        return cursor.rowcount > 0

    def update_runtime(self, uid: str, **fields: Any) -> None:
        """运行态字段增量更新（冷却/熔断/权重/used_seq/last_error/状态）。

        JSON 字段传 dict 自动序列化；白名单外字段直接忽略（防注入面）。
        """
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in self.runtime_fields:
                continue
            sets.append(f"{key} = ?")
            values.append(self._encode(key, value))
        if not sets:
            return
        sets.append("updated_ms = ?")
        values.extend([int(time.time() * 1000), uid])
        self._write(f"UPDATE accounts SET {', '.join(sets)} WHERE uid = ?", tuple(values))

    def next_used_seq(self) -> int:
        """全局单调计数器：LRU 兜底 + 防惊群。"""
        row = self._fetchone("SELECT COALESCE(MAX(used_seq), 0) AS n FROM accounts")
        return int(row["n"]) + 1

    def count_by_status(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) AS n FROM accounts GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in rows}
