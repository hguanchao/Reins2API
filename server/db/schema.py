"""分库建表模板。每个 provider 一个 SQLite 文件（文件即命名空间），
每库自带 meta.schema_version，迁移各自独立。"""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL DEFAULT '',
    at_ms INTEGER NOT NULL,
    ts TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    upstream_model TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT 'chat',
    upstream_protocol TEXT NOT NULL DEFAULT 'chat',
    free INTEGER NOT NULL DEFAULT 0,
    stream INTEGER NOT NULL DEFAULT 0,
    routed INTEGER NOT NULL DEFAULT 0,
    shaped INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    session TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_at ON requests(at_ms);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
