"""账号表存储共享内核（codex / workbuddy 两个号池的 SQLite 管道）。

两个 provider 的 accounts 表列不同、Account 数据类不同，但「连接 + PRAGMA +
DDL + 幂等补列 + CRUD + 运行态增量更新 + 状态计数 + 进程内单例」这套管道逐字相同。
此前各自维护一份（改一处漏一处），这里收成一个基类，子类只声明差异：

    db_filename / log_tag / columns / runtime_fields / json_fields
    ddl / added_columns / uid_re / _row_to_account

`update_tokens` 的列集各 provider 不同（codex 有 id_token/plan_type，workbuddy 有
domain→realm），留给子类；它们用基类的 `_write` 落库。

线程模型是 ThreadingHTTPServer（线程/请求），全部读写经一把 RLock 串行化；
sqlite 全程参数化查询；uid 白名单防路径穿越/注入。
"""

from __future__ import annotations

import atexit
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.logger import logger

# 账号库路径来自 settings：测试进程由 settings 分流到临时目录（见 settings._test_scratch）
from core.settings import DATA_DIR

# 账号级生命周期状态（与冷却/熔断正交）：active 可派号；paused 人工暂停；
# disabled 终态（session 失效/refresh 耗尽），需人工 revive。
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DISABLED = "disabled"

_default_lock = threading.Lock()
_defaults: dict[type, Any] = {}


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

    # ── 构造与校验（子类共用一份实现）──

    @classmethod
    def default(cls) -> Any:
        """进程内单例：每个子类一个实例、一条独立连接（面板与 provider 共用）。"""
        with _default_lock:
            instance = _defaults.get(cls)
            if instance is None:
                instance = cls()
                _defaults[cls] = instance
                logger.info(f"[{instance.log_tag}] 账号库就绪 path={instance._path}")
            return instance

    @classmethod
    def valid_uid(cls, uid: str) -> bool:
        """uid 白名单校验（匹配各子类声明的 uid_re 字符集与长度）。"""
        return isinstance(uid, str) and bool(cls.uid_re.match(uid))

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
        """运行态字段增量更新（冷却/熔断/last_error/状态/余额）。

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

    def count_by_status(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) AS n FROM accounts GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in rows}


def write_with_retry(
    write: Callable[[], Any], *, attempts: int = 3, base_delay_s: float = 0.2
) -> Any:
    """执行一次「必须落地」的写：调用方刚把一次性 refresh_token 换成了新票。

    这种写失败与普通写失败完全不同——票已经在上游轮换过了，库里那份是废票，
    静默当成"这次刷新失败"会让下一轮拿旧票再去换（换不出票，还可能被上游判成
    重放）。所以：先重试几次抗瞬时锁竞争/抖动，仍失败就把异常抛给调用方，由它
    明确标状态、要求人工重新授权。
    """
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return write()
        except Exception as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            logger.warning(
                f"[accounts] 凭据落库失败，重试 {attempt + 1}/{attempts - 1}: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(base_delay_s * (attempt + 1))
    assert last is not None  # attempts >= 1 时循环至少跑过一次
    raise last


def _close_defaults() -> None:
    """进程退出时关掉默认单例的连接（atexit，尽力而为）。

    两处都需要：生产进程的排水收尾只关了 db.stores()（遥测库），账号库不在那份名单里
    ——句柄留给解释器收尾虽然不会损坏 WAL，但"排水完成即状态干净"这条对得上更好；
    测试进程则靠它让自己的临时目录能被 settings 的 atexit 删净（打开的 SQLite 文件
    在 Windows 上删不掉）。注册顺序也正好：本模块 import settings 在先，atexit 后进
    先出，本函数先跑，settings 的 rmtree 后跑。
    """
    with _default_lock:
        instances = list(_defaults.values())
        _defaults.clear()
    for instance in instances:
        instance.close()


atexit.register(_close_defaults)
