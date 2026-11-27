"""The sync engine.

A run:
1. refreshes the library snapshot,
2. reads the tracks of every checked album and playlist,
3. downloads tracks that are wanted but missing,
4. queues files that nothing references any more for confirmation (never deletes on its own),
5. writes an .m3u8 file for every synced playlist.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import media
from .db import Database
from .downloader import Downloader, produce_track
from .library import refresh_library
from .media import TrackMeta
from .ytm import LibrarySource, parse_cookies, thumb_url

log = logging.getLogger("ytmls.sync")

ALBUM_CACHE_SECONDS = 30 * 86400
MAX_ATTEMPTS = 5
MAX_CONSECUTIVE_FAILURES = 5

DEFAULTS = {
    "sync_interval_hours": "6",
    "mp3_quality": media.DEFAULT_QUALITY,
    "premium_quality": "0",
    "max_downloads_per_run": "200",
    "download_delay_seconds": "3",
}


def get_option(db: Database, key: str) -> str:
    value = db.get_setting(key)
    return value if value is not None else DEFAULTS[key]


@dataclass
class SyncSummary:
    library_new: list[str] = field(default_factory=list)
    library_gone: list[str] = field(default_factory=list)
    tracks_new: dict[str, list[str]] = field(default_factory=dict)       # item title -> track titles
    tracks_dropped: dict[str, list[str]] = field(default_factory=dict)   # item title -> track titles
    downloaded: int = 0
    downloaded_titles: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    deferred: int = 0
    removals_queued: list[str] = field(default_factory=list)
    removals_cancelled: int = 0
    playlists_written: int = 0
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str | None) -> "SyncSummary | None":
        if not raw:
            return None
        data = json.loads(raw)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def detail_lines(self, include_library: bool = True) -> list[str]:
        lines = []
        for title, tracks in self.tracks_new.items():
            lines.append(f"{title}: {len(tracks)} new ({', '.join(tracks[:3])}{'…' if len(tracks) > 3 else ''})")
        if self.library_new and include_library:
            lines.append(f"New in library: {', '.join(self.library_new[:3])}{'…' if len(self.library_new) > 3 else ''}")
        if self.removals_queued:
            lines.append(f"Waiting for your OK to remove: {', '.join(self.removals_queued[:3])}"
                         f"{'…' if len(self.removals_queued) > 3 else ''}")
        if self.failed:
            lines.append(f"Failed: {', '.join(self.failed[:3])}{'…' if len(self.failed) > 3 else ''}")
        return lines

    def headline(self) -> str:
        parts = []
        if self.downloaded:
            parts.append(f"{self.downloaded} downloaded")
        new_tracks = sum(len(v) for v in self.tracks_new.values())
        if new_tracks:
            parts.append(f"{new_tracks} new in synced items")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.deferred:
            parts.append(f"{self.deferred} left for next run")
        if self.removals_queued:
            n = len(self.removals_queued)
            parts.append(f"{n} removal{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your OK")
        if self.library_new:
            n = len(self.library_new)
            parts.append(f"{n} new library item{'s' if n != 1 else ''}")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts) or "No changes"


class SyncBusy(Exception):
    pass


class _Release:
    def __init__(self, lock: threading.Lock):
        self.lock = lock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


class SyncEngine:
    def __init__(
        self,
        db: Database,
        music_dir: Path,
        tmp_root: Path,
        get_cookies: Callable[[], str | None],
        source_factory: Callable[[str], LibrarySource],
        downloader: Downloader,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db = db
        self.music_dir = music_dir
        self.tmp_root = tmp_root
        self.get_cookies = get_cookies
        self.source_factory = source_factory
        self.downloader = downloader
        self.sleep = sleep
        self.covers_dir = tmp_root.parent / "covers"
        self._lock = threading.Lock()
        self.progress: dict[str, Any] = {}
        # Called after every run with (status, summary, error).
        self.on_finished: Callable[[str, SyncSummary, str | None], None] | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def exclusive(self):
        """Hold the sync lock while editing files, so a sync can't run at the same time."""
        if not self._lock.acquire(blocking=False):
            raise SyncBusy()
        return _Release(self._lock)

    # ------------------------------------------------------------ run

    def run(self, trigger: str) -> SyncSummary:
        if not self._lock.acquire(blocking=False):
            raise SyncBusy()
        run_id = self.db.execute(
            "INSERT INTO sync_runs(trigger, started_at, status) VALUES(?, ?, 'running')", (trigger, time.time())
        ).lastrowid
        summary = SyncSummary()
        status, error = "ok", None
        try:
            self.progress = {"phase": "Reading library"}
            netscape = self.get_cookies()
            if not netscape:
                raise RuntimeError("No YouTube Music cookies are saved.")
            source = self.source_factory(parse_cookies(netscape).cookie_header)

            lib = refresh_library(self.db, source)
            summary.library_new = [] if lib.first_import else [e.title for e in lib.added]
            summary.library_gone = list(lib.removed)

            self.progress = {"phase": "Reading synced albums and playlists"}
            self._update_memberships(source, summary)
            summary.removals_cancelled = self._cancel_stale_removals()

            problem = media.folder_problem(self.music_dir)
            if problem:
                raise RuntimeError(problem)
            self._download_missing(netscape, summary)
            self._queue_removals(summary)
            self._write_playlists(summary)
            if summary.failed or summary.errors:
                status = "partial"
        except Exception as exc:  # recorded on the run; never crash the scheduler
            log.exception("Sync run failed")
            status, error = "error", str(exc)[:500]
            summary.errors.append(error)
        finally:
            self.progress = {}
            self.db.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
                (time.time(), status, summary.to_json(), error, run_id),
            )
            self.db.log_event("sync", f"Sync finished ({trigger}): {summary.headline()}", summary.to_json())
            self._lock.release()
        if self.on_finished:
            try:
                self.on_finished(status, summary, error)
            except Exception:
                log.exception("on_finished hook failed")
        return summary

    # ------------------------------------------------------------ tracks

    def _album(self, source: LibrarySource, browse_id: str, force: bool = False) -> dict[str, Any] | None:
        row = self.db.one("SELECT data, fetched_at FROM album_cache WHERE browse_id = ?", (browse_id,))
        if row and not force and time.time() - row["fetched_at"] < ALBUM_CACHE_SECONDS:
            return json.loads(row["data"])
        try:
            data = source.album(browse_id)
        except Exception as exc:
            log.warning("Could not read album %s: %s", browse_id, exc)
            return json.loads(row["data"]) if row else None
        slim = {
            "title": data.get("title"),
            "artists": [a.get("name") for a in data.get("artists") or [] if a.get("name")],
            "year": data.get("year"),
            "cover": thumb_url(data),
            "tracks": [
                {"videoId": t.get("videoId"), "title": t.get("title"), "trackNumber": t.get("trackNumber"),
                 "artists": [a.get("name") for a in t.get("artists") or [] if a.get("name")],
                 "isAvailable": t.get("isAvailable", True), "duration": t.get("duration_seconds")}
                for t in data.get("tracks") or []
            ],
        }
        self.db.execute(
            "INSERT INTO album_cache(browse_id, data, fetched_at) VALUES(?, ?, ?) "
            "ON CONFLICT(browse_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            (browse_id, json.dumps(slim), time.time()),
        )
        return slim

    def _tracks_for_album(self, source: LibrarySource, browse_id: str) -> list[tuple[TrackMeta, int | None]]:
        album = self._album(source, browse_id, force=True)
        if not album:
            raise RuntimeError("album could not be read")
        album_artist = ", ".join(album["artists"]) or "Various Artists"
        out = []
        for t in album["tracks"]:
            if not t.get("videoId") or t.get("isAvailable") is False:
                continue
            out.append((TrackMeta(
                video_id=t["videoId"], title=t.get("title") or t["videoId"],
                artists=", ".join(t.get("artists") or []) or album_artist,
                album=album.get("title") or "Unknown Album", album_artist=album_artist,
                track_number=t.get("trackNumber") or None, year=album.get("year"), cover_url=album.get("cover"),
            ), t.get("duration")))
        return out

    def _tracks_for_playlist(self, source: LibrarySource, playlist_id: str) -> list[tuple[TrackMeta, int | None]]:
        data = source.playlist(playlist_id)
        out = []
        for t in data.get("tracks") or []:
            vid = t.get("videoId")
            if not vid or t.get("isAvailable") is False:
                continue
            title = t.get("title") or vid
            artists = ", ".join(a.get("name") for a in t.get("artists") or [] if a.get("name")) or "Unknown Artist"
            album_ref = t.get("album") or {}
            album = self._album(source, album_ref["id"]) if album_ref.get("id") else None
            if album:
                album_artist = ", ".join(album["artists"]) or artists
                match = next((x for x in album["tracks"] if x.get("videoId") == vid), None) or next(
                    (x for x in album["tracks"] if (x.get("title") or "").casefold() == title.casefold()), None)
                meta = TrackMeta(vid, title, artists, album.get("title") or album_ref.get("name") or title,
                                 album_artist, (match or {}).get("trackNumber") or None, album.get("year"),
                                 album.get("cover") or thumb_url(t))
            else:
                # No album (music video or upload): file it as a single.
                first_artist = (t.get("artists") or [{}])[0].get("name") or artists
                meta = TrackMeta(vid, title, artists, album_ref.get("name") or title, first_artist,
                                 None, None, thumb_url(t))
            out.append((meta, t.get("duration_seconds")))
        return out

    def _upsert_track(self, meta: TrackMeta, duration: int | None) -> None:
        now = time.time()
        row = self.db.one("SELECT status, meta_locked FROM tracks WHERE video_id = ?", (meta.video_id,))
        if row is None:
            self.db.execute(
                "INSERT INTO tracks(video_id, title, artists, album, album_artist, track_number, year, duration, "
                "cover_url, status, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (meta.video_id, meta.title, meta.artists, meta.album, meta.album_artist, meta.track_number,
                 meta.year, duration, meta.cover_url, now),
            )
        elif row["status"] != "downloaded" and not row["meta_locked"]:
            # Downloaded files keep the metadata they were written with, and tracks you
            # edited in the dashboard keep your edits.
            self.db.execute(
                "UPDATE tracks SET title = ?, artists = ?, album = ?, album_artist = ?, track_number = ?, "
                "year = ?, duration = ?, cover_url = ?, updated_at = ? WHERE video_id = ?",
                (meta.title, meta.artists, meta.album, meta.album_artist, meta.track_number, meta.year,
                 duration, meta.cover_url, now, meta.video_id),
            )

    def _update_memberships(self, source: LibrarySource, summary: SyncSummary) -> None:
        items = self.db.all(
            "SELECT id, kind, title FROM library_items WHERE sync_enabled = 1 AND removed_at IS NULL ORDER BY title")
        for item in items:
            try:
                if item["kind"] == "album":
                    tracks = self._tracks_for_album(source, item["id"])
                else:
                    tracks = self._tracks_for_playlist(source, item["id"])
            except Exception as exc:
                log.warning("Could not read %s: %s", item["title"], exc)
                summary.errors.append(f"Could not read {item['title']}: {exc}"[:300])
                continue
            now = time.time()
            first_sync = self.db.one("SELECT 1 FROM item_tracks WHERE item_id = ? LIMIT 1", (item["id"],)) is None
            seen: set[str] = set()
            new_titles: list[str] = []
            with self.db.transaction():
                for pos, (meta, duration) in enumerate(tracks):
                    if meta.video_id in seen:
                        continue
                    seen.add(meta.video_id)
                    self._upsert_track(meta, duration)
                    row = self.db.one("SELECT removed_at FROM item_tracks WHERE item_id = ? AND video_id = ?",
                                      (item["id"], meta.video_id))
                    if row is None:
                        self.db.execute(
                            "INSERT INTO item_tracks(item_id, video_id, position, added_at) VALUES(?, ?, ?, ?)",
                            (item["id"], meta.video_id, pos, now))
                        new_titles.append(meta.title)
                    else:
                        if row["removed_at"] is not None:
                            new_titles.append(meta.title)
                        self.db.execute(
                            "UPDATE item_tracks SET position = ?, removed_at = NULL WHERE item_id = ? AND video_id = ?",
                            (pos, item["id"], meta.video_id))
                dropped = self.db.all(
                    "SELECT it.video_id, t.title FROM item_tracks it JOIN tracks t ON t.video_id = it.video_id "
                    "WHERE it.item_id = ? AND it.removed_at IS NULL", (item["id"],))
                dropped_titles = []
                for row in dropped:
                    if row["video_id"] not in seen:
                        self.db.execute("UPDATE item_tracks SET removed_at = ? WHERE item_id = ? AND video_id = ?",
                                        (now, item["id"], row["video_id"]))
                        dropped_titles.append(row["title"])
                self.db.execute("UPDATE library_items SET track_count = ? WHERE id = ?", (len(seen), item["id"]))
            if new_titles and not first_sync:
                summary.tracks_new[item["title"]] = new_titles
                self.db.log_event("tracks", f"{len(new_titles)} new in {item['title']}", json.dumps(new_titles))
            elif first_sync and new_titles:
                self.db.log_event("tracks", f"Started syncing {item['title']} ({len(new_titles)} tracks)")
            if dropped_titles:
                summary.tracks_dropped[item["title"]] = dropped_titles
                self.db.log_event("tracks", f"{len(dropped_titles)} no longer in {item['title']}",
                                  json.dumps(dropped_titles))

    # ------------------------------------------------------------ wanted / removals

    WANTED_SQL = (
        "SELECT DISTINCT it.video_id FROM item_tracks it JOIN library_items li ON li.id = it.item_id "
        "WHERE li.sync_enabled = 1 AND li.removed_at IS NULL AND it.removed_at IS NULL"
    )

    def wanted(self) -> set[str]:
        return {r["video_id"] for r in self.db.all(self.WANTED_SQL)}

    def _cancel_stale_removals(self) -> int:
        wanted = self.wanted()
        cancelled = 0
        for r in self.db.all("SELECT id, kind, ref FROM removals WHERE decision IS NULL"):
            still_needed = r["ref"] in wanted if r["kind"] == "track" else self._playlist_active(r["ref"])
            if still_needed:
                self.db.execute("UPDATE removals SET decision = 'cancelled', decided_at = ? WHERE id = ?",
                                (time.time(), r["id"]))
                cancelled += 1
        return cancelled

    def _playlist_active(self, item_id: str) -> bool:
        return self.db.one("SELECT 1 FROM library_items WHERE id = ? AND sync_enabled = 1 AND removed_at IS NULL",
                           (item_id,)) is not None

    def _removal_reason(self, video_id: str) -> str:
        reasons = []
        for r in self.db.all(
            "SELECT li.title, li.kind, li.sync_enabled, li.removed_at AS item_removed, it.removed_at AS link_removed "
            "FROM item_tracks it JOIN library_items li ON li.id = it.item_id WHERE it.video_id = ?", (video_id,)
        ):
            if r["link_removed"]:
                reasons.append(f"Removed from {r['kind']} {r['title']}")
            elif r["item_removed"]:
                reasons.append(f"{r['title']} is no longer in your library")
            elif not r["sync_enabled"]:
                reasons.append(f"Sync turned off for {r['title']}")
        return "; ".join(dict.fromkeys(reasons)) or "No synced album or playlist includes it"

    def _queue_removals(self, summary: SyncSummary) -> None:
        wanted = self.wanted()
        now = time.time()
        pending_tracks = {r["ref"] for r in self.db.all(
            "SELECT ref FROM removals WHERE decision IS NULL AND kind = 'track'")}
        for t in self.db.all("SELECT video_id, title, artists, file_path FROM tracks "
                             "WHERE status = 'downloaded' AND keep = 0"):
            if t["video_id"] in wanted or t["video_id"] in pending_tracks:
                continue
            self.db.execute(
                "INSERT INTO removals(kind, ref, title, detail, reason, created_at) VALUES('track', ?, ?, ?, ?, ?)",
                (t["video_id"], t["title"], t["file_path"], self._removal_reason(t["video_id"]), now))
            summary.removals_queued.append(t["title"])

        pending_lists = {r["ref"] for r in self.db.all(
            "SELECT ref FROM removals WHERE decision IS NULL AND kind = 'playlist_file'")}
        for p in self.db.all("SELECT id, title, playlist_file, sync_enabled, removed_at FROM library_items "
                             "WHERE playlist_file IS NOT NULL"):
            if self._playlist_active(p["id"]) or p["id"] in pending_lists:
                continue
            reason = "No longer in your library" if p["removed_at"] else "Sync turned off"
            self.db.execute(
                "INSERT INTO removals(kind, ref, title, detail, reason, created_at) "
                "VALUES('playlist_file', ?, ?, ?, ?, ?)",
                (p["id"], f"Playlist file: {p['title']}", p["playlist_file"], reason, now))
            summary.removals_queued.append(f"Playlist file: {p['title']}")
        if summary.removals_queued:
            n = len(summary.removals_queued)
            self.db.log_event("removal", f"{n} removal{'s' if n != 1 else ''} waiting for confirmation",
                              json.dumps(summary.removals_queued))

    def decide_removal(self, removal_id: int, delete: bool) -> str | None:
        r = self.db.one("SELECT * FROM removals WHERE id = ? AND decision IS NULL", (removal_id,))
        if r is None:
            return None
        now = time.time()
        if r["kind"] == "track":
            track = self.db.one("SELECT * FROM tracks WHERE video_id = ?", (r["ref"],))
            if delete and r["ref"] in self.wanted():
                # It became wanted again since the request was made; don't delete.
                self.db.execute("UPDATE removals SET decision = 'cancelled', decided_at = ? WHERE id = ?",
                                (now, removal_id))
                return "cancelled"
            if delete:
                if track and track["file_path"]:
                    media.remove_file(self.music_dir, track["file_path"])
                self.db.execute("UPDATE tracks SET status = 'removed', file_path = NULL, updated_at = ? "
                                "WHERE video_id = ?", (now, r["ref"]))
                self.db.log_event("removal", f"Deleted: {r['title']}", r["detail"])
            else:
                self.db.execute("UPDATE tracks SET keep = 1 WHERE video_id = ?", (r["ref"],))
                self.db.log_event("removal", f"Kept: {r['title']}", r["detail"])
        else:
            if delete and not self._playlist_active(r["ref"]):
                media.remove_file(self.music_dir, r["detail"])
                self.db.log_event("removal", f"Deleted {r['title']}", r["detail"])
            else:
                self.db.log_event("removal", f"Kept {r['title']} (no longer updated)", r["detail"])
            self.db.execute("UPDATE library_items SET playlist_file = NULL WHERE id = ?", (r["ref"],))
        decision = "deleted" if delete else "kept"
        self.db.execute("UPDATE removals SET decision = ?, decided_at = ? WHERE id = ?", (decision, now, removal_id))
        return decision

    # ------------------------------------------------------------ downloads

    def _custom_cover(self, name: str | None) -> tuple[bytes, str] | None:
        if not name:
            return None
        path = self.covers_dir / name
        try:
            data = path.read_bytes()
        except OSError:
            return None
        mime = media.sniff_image(data)
        return (data, mime) if mime else None

    def target_path(self, meta: TrackMeta) -> Path:
        return self._target_path(meta)

    def _target_path(self, meta: TrackMeta) -> Path:
        rel = media.track_relpath(meta)
        clash = self.db.one("SELECT video_id FROM tracks WHERE file_path = ? AND video_id != ?",
                            (rel.as_posix(), meta.video_id))
        if clash or ((self.music_dir / rel).exists() and not self.db.one(
                "SELECT 1 FROM tracks WHERE file_path = ?", (rel.as_posix(),))):
            rel = rel.with_name(f"{rel.stem} [{meta.video_id}].mp3")
        return rel

    def _download_missing(self, netscape: str, summary: SyncSummary) -> None:
        wanted = self.wanted()
        todo = [r for r in self.db.all(
            "SELECT * FROM tracks WHERE status IN ('pending', 'failed', 'removed') AND attempts < ? "
            "ORDER BY updated_at", (MAX_ATTEMPTS,)) if r["video_id"] in wanted]
        if not todo:
            return
        limit = int(get_option(self.db, "max_downloads_per_run"))
        delay = float(get_option(self.db, "download_delay_seconds"))
        premium = get_option(self.db, "premium_quality") == "1"
        quality = get_option(self.db, "mp3_quality")
        summary.deferred = max(0, len(todo) - limit)
        todo = todo[:limit]

        self.tmp_root.mkdir(parents=True, exist_ok=True)
        cookies_file = self.tmp_root / f"cookies-{os.getpid()}-{threading.get_ident()}.txt"
        fd = os.open(cookies_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(netscape)
        consecutive = 0
        try:
            for n, t in enumerate(todo, start=1):
                self.progress = {"phase": "Downloading", "done": n - 1, "total": len(todo), "current": t["title"]}
                meta = TrackMeta(t["video_id"], t["title"], t["artists"] or "", t["album"] or t["title"],
                                 t["album_artist"] or t["artists"] or "Unknown Artist", t["track_number"],
                                 t["year"], t["cover_url"])
                rel = self._target_path(meta)
                try:
                    produce_track(self.downloader, meta, self.music_dir, rel, cookies_file,
                                  premium=premium, quality=quality, tmp_root=self.tmp_root,
                                  cover_override=self._custom_cover(t["custom_cover"]))
                except Exception as exc:
                    consecutive += 1
                    msg = str(exc).splitlines()[0][:400] if str(exc) else type(exc).__name__
                    self.db.execute("UPDATE tracks SET status = 'failed', attempts = attempts + 1, error = ?, "
                                    "updated_at = ? WHERE video_id = ?", (msg, time.time(), t["video_id"]))
                    summary.failed.append(t["title"])
                    log.warning("Download failed for %s: %s", t["video_id"], msg)
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        summary.errors.append(
                            f"Stopped after {consecutive} failures in a row. Last error: {msg}. "
                            "If this keeps happening, your cookies may need refreshing.")
                        summary.deferred += len(todo) - n
                        break
                else:
                    consecutive = 0
                    self.db.execute(
                        "UPDATE tracks SET status = 'downloaded', file_path = ?, error = NULL, attempts = 0, "
                        "downloaded_at = ?, updated_at = ? WHERE video_id = ?",
                        (rel.as_posix(), time.time(), time.time(), t["video_id"]))
                    summary.downloaded += 1
                    summary.downloaded_titles.append(t["title"])
                if delay and n < len(todo):
                    self.sleep(delay)
        finally:
            cookies_file.unlink(missing_ok=True)
        if summary.downloaded:
            self.db.log_event("download", f"Downloaded {summary.downloaded} tracks")
        if summary.failed:
            self.db.log_event("download", f"{len(summary.failed)} downloads failed", json.dumps(summary.failed))

    # ------------------------------------------------------------ playlists

    def rewrite_playlists(self) -> None:
        self._write_playlists(SyncSummary())

    def _write_playlists(self, summary: SyncSummary) -> None:
        for p in self.db.all("SELECT id, title, playlist_file FROM library_items "
                             "WHERE kind = 'playlist' AND sync_enabled = 1 AND removed_at IS NULL"):
            entries = [
                (r["file_path"], r["duration"] or -1, f"{r['artists']} - {r['title']}" if r["artists"] else r["title"])
                for r in self.db.all(
                    "SELECT t.file_path, t.duration, t.title, t.artists FROM item_tracks it "
                    "JOIN tracks t ON t.video_id = it.video_id "
                    "WHERE it.item_id = ? AND it.removed_at IS NULL AND t.status = 'downloaded' "
                    "ORDER BY it.position", (p["id"],))
            ]
            try:
                rel = media.write_m3u(self.music_dir, p["title"], p["id"], entries)
                if p["playlist_file"] and p["playlist_file"] != rel.as_posix():
                    # Playlist was renamed on YouTube Music; the old file is just the previous name.
                    media.remove_file(self.music_dir, p["playlist_file"])
            except OSError as exc:
                summary.errors.append(f"Could not write playlist {p['title']}: {exc}"[:300])
                continue
            self.db.execute("UPDATE library_items SET playlist_file = ? WHERE id = ?", (rel.as_posix(), p["id"]))
            summary.playlists_written += 1


class Scheduler:
    """Runs the engine every N hours (from settings) and on demand."""

    def __init__(self, engine: SyncEngine, db: Database):
        self.engine = engine
        self.db = db
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._manual = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ytmls-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def queued(self) -> bool:
        return self._manual

    def trigger_now(self) -> bool:
        if self.engine.running:
            return False
        self._manual = True
        self._wake.set()
        return True

    def next_run_at(self) -> float | None:
        if not self.db.get_setting("ytm_cookies_enc"):
            return None
        last = self.db.one("SELECT MAX(started_at) AS t FROM sync_runs WHERE trigger = 'schedule'")["t"]
        last_any = self.db.one("SELECT MAX(started_at) AS t FROM sync_runs")["t"]
        base = max(filter(None, [last, last_any]), default=None)
        hours = float(get_option(self.db, "sync_interval_hours"))
        return (base + hours * 3600) if base else time.time()

    def _loop(self) -> None:
        # Give the web server a moment to come up before the first run.
        self._stop.wait(30)
        while not self._stop.is_set():
            due = self.next_run_at()
            if self._manual or (due is not None and time.time() >= due):
                trigger = "manual" if self._manual else "schedule"
                self._manual = False
                try:
                    self.engine.run(trigger)
                except SyncBusy:
                    pass
                except Exception:
                    log.exception("Scheduler run crashed")
            self._wake.wait(60)
            self._wake.clear()
