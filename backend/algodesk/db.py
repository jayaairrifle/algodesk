"""SQLite storage. One connection shared behind a lock: the load is one agent
posting every few seconds plus a handful of dashboard users."""

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('admin', 'trader', 'viewer')),
    pw_hash      TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    last_login   REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT PRIMARY KEY,
    secret     TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_seen  REAL
);
CREATE TABLE IF NOT EXISTS telemetry_latest (
    agent_id    TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
    received_at REAL NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_samples (
    agent_id TEXT NOT NULL,
    ts       REAL NOT NULL,
    equity   REAL NOT NULL,
    balance  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS equity_samples_agent_ts ON equity_samples(agent_id, ts);
CREATE TABLE IF NOT EXISTS commands (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    magic        INTEGER,
    value        REAL,
    issued_by    INTEGER REFERENCES users(id),
    issued_at    REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    delivered_at REAL,
    acked_at     REAL,
    result       TEXT,
    entry_state  TEXT
);
CREATE INDEX IF NOT EXISTS commands_agent_status ON commands(agent_id, status);
CREATE TABLE IF NOT EXISTS eas (
    magic INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT ''
);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def close(self) -> None:
        with self.lock:
            self.conn.close()
