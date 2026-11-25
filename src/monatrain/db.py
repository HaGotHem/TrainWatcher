"""SQLite connection and schema."""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    avatar     TEXT,
    dm_ok      INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS followed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    from_id         TEXT NOT NULL,
    to_id           TEXT NOT NULL,
    dep_time        TEXT NOT NULL,
    days            INTEGER NOT NULL,
    train_number    TEXT NOT NULL,
    suggestion_sent INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, from_id, to_id, dep_time)
);

CREATE TABLE IF NOT EXISTS daily_state (
    followed_id     INTEGER NOT NULL REFERENCES followed(id) ON DELETE CASCADE,
    service_date    TEXT NOT NULL,
    found           INTEGER,
    last_delay      INTEGER,
    notified_status TEXT,
    notified_delay  INTEGER,
    PRIMARY KEY (followed_id, service_date)
);

CREATE TABLE IF NOT EXISTS quota (
    day      TEXT NOT NULL,
    consumer TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, consumer)
);

CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    body       TEXT NOT NULL,
    family     TEXT
);
CREATE INDEX IF NOT EXISTS api_cache_family ON api_cache (family, fetched_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
