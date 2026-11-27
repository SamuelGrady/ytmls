"""SQLite storage with simple versioned migrations."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        totp_secret_enc TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0,
        totp_last_step INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        stage TEXT NOT NULL,              -- 'full' or 'pending_2fa'
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        ip TEXT,
        user_agent TEXT
    );
    CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY,
        ip TEXT NOT NULL,
        ts REAL NOT NULL,
        success INTEGER NOT NULL
    );
    CREATE INDEX idx_login_attempts_ts ON login_attempts(ts);
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE library_items (
        id TEXT PRIMARY KEY,              -- album browseId or playlistId
        kind TEXT NOT NULL,               -- 'album' | 'playlist'
        title TEXT NOT NULL,
        subtitle TEXT,
        year TEXT,
        track_count INTEGER,
        thumbnail_url TEXT,
        sync_enabled INTEGER NOT NULL DEFAULT 0,
        reviewed INTEGER NOT NULL DEFAULT 0,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        removed_at REAL
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        details TEXT
    );
    CREATE INDEX idx_events_ts ON events(ts);
    """,
]


class Database:
    """Thin wrapper around a single SQLite connection guarded by a lock."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for idx, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {idx};\nCOMMIT;")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def transaction(self):
        return _Transaction(self)

    # settings helpers
    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def log_event(self, kind: str, message: str, details: str | None = None) -> None:
        self.execute(
            "INSERT INTO events(ts, kind, message, details) VALUES(?, ?, ?, ?)",
            (time.time(), kind, message, details),
        )

    def close(self) -> None:
        self._conn.close()


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
