"""Editing track and album metadata from the dashboard.

Edits are saved in the database (and locked, so a later sync never undoes
them), written into the MP3's tags, and the file is renamed/moved when the
fields that make up its path change. Playlists are rewritten to match.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import media
from .db import Database
from .sync import SyncEngine

MAX_COVER_BYTES = 10 * 1024 * 1024  # keep in step with main.MAX_COVER_UPLOAD
FIELDS = ("title", "artists", "album", "album_artist", "track_number", "year")
LIMITS = {"title": 300, "artists": 300, "album": 300, "album_artist": 300, "year": 10}


class EditError(ValueError):
    pass


@dataclass
class EditResult:
    changed: list[str]
    moved_to: str | None = None


def clean_fields(raw: dict[str, str], only: tuple[str, ...] = FIELDS) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in only:
        if key not in raw:
            continue
        value = (raw[key] or "").strip()
        if key == "track_number":
            if not value:
                out[key] = None
                continue
            if not value.isdigit() or not 0 < int(value) < 1000:
                raise EditError("Track number must be a whole number from 1 to 999.")
            out[key] = int(value)
        elif key == "year":
            if value and not (value.isdigit() and len(value) == 4):
                raise EditError("Year must be four digits, like 2019.")
            out[key] = value or None
        else:
            if key in ("title", "album", "album_artist") and not value:
                raise EditError(f"{key.replace('_', ' ').capitalize()} can't be empty.")
            if len(value) > LIMITS[key]:
                raise EditError(f"{key.replace('_', ' ').capitalize()} is too long.")
            if any(ord(c) < 32 for c in value):
                raise EditError("Text can't contain control characters.")
            out[key] = value
    return out


def check_cover(data: bytes | None) -> str | None:
    if not data:
        return None
    if len(data) > MAX_COVER_BYTES:
        raise EditError("Cover image must be 10 MB or smaller.")
    mime = media.sniff_image(data)
    if not mime:
        raise EditError("Cover image must be a JPEG or PNG.")
    return mime


class MetadataEditor:
    def __init__(self, db: Database, engine: SyncEngine, music_dir: Path):
        self.db = db
        self.engine = engine
        self.music_dir = music_dir
        self.covers_dir = engine.covers_dir

    def _save_cover(self, video_id: str, data: bytes, mime: str) -> str:
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        ext = ".png" if mime == "image/png" else ".jpg"
        name = hashlib.sha256(video_id.encode()).hexdigest()[:24] + ext
        tmp = self.covers_dir / (name + ".partial")
        tmp.write_bytes(data)
        os.replace(tmp, self.covers_dir / name)
        return name

    def _apply(self, video_id: str, fields: dict[str, object], cover: tuple[bytes, str] | None,
               reset_cover: bool) -> EditResult:
        row = self.db.one("SELECT * FROM tracks WHERE video_id = ?", (video_id,))
        if row is None:
            raise EditError("Track not found.")
        changed = [k for k, v in fields.items() if row[k] != v]
        updates = dict(fields)
        custom_cover = row["custom_cover"]
        if cover is not None:
            custom_cover = self._save_cover(video_id, *cover)
            changed.append("cover")
        elif reset_cover and custom_cover:
            (self.covers_dir / custom_cover).unlink(missing_ok=True)
            custom_cover = None
            changed.append("cover")
        if not changed:
            return EditResult([])
        updates["custom_cover"] = custom_cover
        sets = ", ".join(f"{k} = ?" for k in updates)
        self.db.execute(f"UPDATE tracks SET {sets}, meta_locked = 1, updated_at = ? WHERE video_id = ?",
                        (*updates.values(), time.time(), video_id))

        result = EditResult(changed)
        new = self.db.one("SELECT * FROM tracks WHERE video_id = ?", (video_id,))
        if new["status"] == "downloaded" and new["file_path"]:
            meta = self.engine.meta_for(new)
            path = media.ensure_inside(self.music_dir, Path(new["file_path"]))
            if cover is not None:
                file_cover = cover
            elif reset_cover:
                try:
                    file_cover = media.fetch_cover(media.big_cover_url(new["cover_url"]))
                except Exception:
                    file_cover = None
            else:
                file_cover = None  # keep whatever the file has
            media.retag(path, meta, file_cover)
            target = media.track_relpath(meta)
            if target.as_posix() != new["file_path"]:
                target = self.engine.target_path(meta)
                media.move_track(self.music_dir, new["file_path"], target)
                self.engine.place_artist_nfo(target, meta)
                self.db.execute("UPDATE tracks SET file_path = ? WHERE video_id = ?", (target.as_posix(), video_id))
                result.moved_to = target.as_posix()
        return result

    def edit_track(self, video_id: str, raw: dict[str, str], cover_data: bytes | None = None,
                   reset_cover: bool = False) -> EditResult:
        fields = clean_fields(raw)
        mime = check_cover(cover_data)
        with self.engine.exclusive():
            title = self.db.one("SELECT title FROM tracks WHERE video_id = ?", (video_id,))
            result = self._apply(video_id, fields, (cover_data, mime) if mime else None, reset_cover)
            if result.changed:
                self.engine.rewrite_playlists()
                self.db.log_event("metadata", f"Edited {title['title'] if title else video_id}: "
                                              f"{', '.join(result.changed)}",
                                  json.dumps({"moved_to": result.moved_to} if result.moved_to else {}))
        return result

    def edit_album(self, item_id: str, raw: dict[str, str], cover_data: bytes | None = None) -> int:
        """Apply album-wide fields (album, album artist, year, cover) to every track of an album."""
        item = self.db.one("SELECT * FROM library_items WHERE id = ? AND kind = 'album'", (item_id,))
        if item is None:
            raise EditError("Album not found.")
        fields = clean_fields(raw, only=("album", "album_artist", "year"))
        mime = check_cover(cover_data)
        tracks = [r["video_id"] for r in self.db.all(
            "SELECT video_id FROM item_tracks WHERE item_id = ? AND removed_at IS NULL", (item_id,))]
        changed = 0
        with self.engine.exclusive():
            for vid in tracks:
                if self._apply(vid, fields, (cover_data, mime) if mime else None, False).changed:
                    changed += 1
            if changed:
                self.engine.rewrite_playlists()
                self.db.log_event("metadata", f"Edited album {item['title']}: {changed} tracks updated",
                                  json.dumps(fields))
        return changed
