"""分库建表模板。每个 provider 一个 SQLite 文件（文件即命名空间）。

列顺序约定：主键 → 标识 → 路由 → 请求特征 → 用量 → 来源 → 结果（状态字段收在时间字段之前）
→ 时间字段（统一放最后）。

结构演进由 ProviderStore 启动时的幂等迁移负责：
新库直接按下面 DDL 建，旧库按 RENAMED_COLUMNS 把历史列名改过来。
"""

from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS requests (
    -- 主键与标识
    id                 INTEGER      PRIMARY KEY AUTOINCREMENT,
    request_id         VARCHAR(64)  NOT NULL DEFAULT '',
    session_id         VARCHAR(64)  NOT NULL DEFAULT '',
    -- 路由
    model              VARCHAR(96)  NOT NULL DEFAULT '',
    upstream_model     VARCHAR(96)  NOT NULL DEFAULT '',
    client_protocol    VARCHAR(16)  NOT NULL DEFAULT 'chat',
    upstream_protocol  VARCHAR(16)  NOT NULL DEFAULT 'chat',
    account_uid        VARCHAR(64)  NOT NULL DEFAULT '',
    -- 请求特征
    is_free            INTEGER      NOT NULL DEFAULT 0,
    is_stream          INTEGER      NOT NULL DEFAULT 0,
    is_routed          INTEGER      NOT NULL DEFAULT 0,
    is_shaped          INTEGER      NOT NULL DEFAULT 0,
    reasoning_effort   VARCHAR(32)  NOT NULL DEFAULT '',
    -- 用量
    input_tokens       INTEGER      NOT NULL DEFAULT 0,
    output_tokens      INTEGER      NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER      NOT NULL DEFAULT 0,
    cached_tokens      INTEGER      NOT NULL DEFAULT 0,
    -- 上游计费量（积分；workbuddy 的 usage.credit）。NULL = 未观测（上游没给该字段
    -- 或跨协议时无此概念），0 = 实测免费（限免期/夜间免费）——两者必须分开。
    credit             REAL,
    -- 来源
    client_ip          VARCHAR(45)  NOT NULL DEFAULT '',
    egress_ip          VARCHAR(45)  NOT NULL DEFAULT '',  -- 出口 IP：上游看到的地址（探测缓存回填；空 = 当时还没探到）
    client_ua          VARCHAR(255) NOT NULL DEFAULT '',
    -- 结果：状态字段紧挨时间字段之前
    first_byte_ms      INTEGER      NOT NULL DEFAULT 0,  -- 首字耗时（0 = 未采到/失败请求）
    duration_ms        INTEGER      NOT NULL DEFAULT 0,
    error_message      VARCHAR(255),
    http_status        INTEGER      NOT NULL DEFAULT 0,
    -- 时间字段统一放最后
    created_ms         INTEGER      NOT NULL,
    created_at         VARCHAR(19)  NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_created_ms ON requests(created_ms);
"""

# account_uid 列走"老库补列"演进（ADDED_COLUMNS），不能进上面的 DDL——executescript
# 先跑，老库此刻还没有该列，索引会报 no such column。在补列完成后再单独创建。
ACCOUNT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_requests_account ON requests(account_uid, created_ms)",
)

# 历史列名 -> 规范列名。启动时按"旧名在、新名不在"逐个改名，已迁移过的库上全部跳过。
# 类型长度只是规范声明，SQLite 按亲和性处理。
RENAMED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("at_ms", "created_ms"),
    ("ts", "created_at"),
    ("endpoint", "client_protocol"),
    ("free", "is_free"),
    ("stream", "is_stream"),
    ("routed", "is_routed"),
    ("shaped", "is_shaped"),
    ("status", "http_status"),
    ("error", "error_message"),
    ("session", "session_id"),
    ("client", "client_ua"),
    ("effort", "reasoning_effort"),
)

# 旧索引名（重命名列后 SQLite 会保留旧名，需换成 idx_ 前缀的新名）
LEGACY_INDEXES: tuple[str, ...] = ("requests_at",)

# 历史上按"增量补列"演进过，老库可能整列都缺。改名之后按新名逐个补，
# 顺序必须在 RENAMED_COLUMNS 之后——否则会先补出空的新列，让改名被跳过、旧数据滞留。
ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("request_id", "TEXT NOT NULL DEFAULT ''"),
    ("client_ip", "TEXT NOT NULL DEFAULT ''"),
    ("egress_ip", "TEXT NOT NULL DEFAULT ''"),
    ("client_ua", "TEXT NOT NULL DEFAULT ''"),
    ("reasoning_effort", "TEXT NOT NULL DEFAULT ''"),
    ("account_uid", "TEXT NOT NULL DEFAULT ''"),
    # 首字耗时晚于以上各列加入；老库整列缺失，默认 0 = 无数据（前端显示「—」）
    ("first_byte_ms", "INTEGER NOT NULL DEFAULT 0"),
    # 上游计费量（积分）：可空，老库补出来的行一律 NULL（未观测），不拿 0 冒充免费
    ("credit", "REAL"),
)
