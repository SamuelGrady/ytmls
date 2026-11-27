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
    # 2: sync engine
    """
    CREATE TABLE tracks (
        video_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        artists TEXT,                     -- display string, e.g. "A, B"
        album TEXT,
        album_id TEXT,
        album_artist TEXT,
        track_number INTEGER,
        year TEXT,
        duration INTEGER,
        cover_url TEXT,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | downloaded | failed | removed
        file_path TEXT,                   -- relative to the music folder
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        keep INTEGER NOT NULL DEFAULT 0,  -- user chose to keep the file even when nothing references it
        downloaded_at REAL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_tracks_status ON tracks(status);
    CREATE TABLE item_tracks (
        item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL REFERENCES tracks(video_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        added_at REAL NOT NULL,
        removed_at REAL,
        PRIMARY KEY (item_id, video_id)
    );
    CREATE INDEX idx_item_tracks_video ON item_tracks(video_id);
    CREATE TABLE album_cache (
        browse_id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        fetched_at REAL NOT NULL
    );
    CREATE TABLE removals (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,               -- 'track' | 'playlist_file'
        ref TEXT NOT NULL,                -- video_id or playlist item id
        title TEXT NOT NULL,
        detail TEXT,
        reason TEXT NOT NULL,
        created_at REAL NOT NULL,
        decided_at REAL,
        decision TEXT                     -- NULL (pending) | 'deleted' | 'kept' | 'cancelled'
    );
    CREATE INDEX idx_removals_pending ON removals(decision);
    CREATE TABLE sync_runs (
        id INTEGER PRIMARY KEY,
        trigger TEXT NOT NULL,            -- 'schedule' | 'manual'
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL,             -- running | ok | partial | error
        summary TEXT,                     -- JSON
        error TEXT
    );
    ALTER TABLE library_items ADD COLUMN playlist_file TEXT;
    """,
    # 3: push notifications
    """
    CREATE TABLE push_subscriptions (
        id INTEGER PRIMARY KEY,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        label TEXT,
        created_at REAL NOT NULL,
        last_ok REAL,
        failures INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 4: metadata editing
    """
    ALTER TABLE tracks ADD COLUMN meta_locked INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tracks ADD COLUMN custom_cover TEXT;
    """,
    # 5: release type, so singles can be kept out of Jellyfin's albums
    """
    ALTER TABLE tracks ADD COLUMN release_type TEXT;  -- album | ep | single | none (no album at all)
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
