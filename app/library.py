"""Library snapshot: pulls albums and playlists and records what changed."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .db import Database
from .ytm import LibraryEntry, LibrarySource


@dataclass
class RefreshResult:
    added: list[LibraryEntry] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # titles
    returned: list[str] = field(default_factory=list)  # titles
    total: int = 0

    def summary(self) -> str:
        parts = [f"{self.total} items in library"]
        if self.added:
            parts.append(f"{len(self.added)} new")
        if self.removed:
            parts.append(f"{len(self.removed)} no longer in library")
        if self.returned:
            parts.append(f"{len(self.returned)} back in library")
        return ", ".join(parts)


def refresh_library(db: Database, source: LibrarySource) -> RefreshResult:
    """Fetch the library and upsert it. Never deletes rows: items that vanish
    are marked ``removed_at`` so the removal can be confirmed later."""
    entries = source.library_albums() + source.library_playlists()
    now = time.time()
    result = RefreshResult(total=len(entries))
    first_refresh = db.get_setting("library_last_refresh") is None
    seen: set[str] = set()

    with db.transaction():
        for e in entries:
            if e.id in seen:
                continue
            seen.add(e.id)
            row = db.one("SELECT removed_at, title FROM library_items WHERE id = ?", (e.id,))
            if row is None:
                db.execute(
                    "INSERT INTO library_items(id, kind, title, subtitle, year, track_count, thumbnail_url, "
                    "sync_enabled, reviewed, first_seen, last_seen) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (e.id, e.kind, e.title, e.subtitle, e.year, e.track_count, e.thumbnail_url,
                     int(first_refresh), now, now),
                )
                result.added.append(e)
            else:
                if row["removed_at"] is not None:
                    result.returned.append(e.title)
                db.execute(
                    "UPDATE library_items SET title = ?, subtitle = ?, year = ?, track_count = ?, "
                    "thumbnail_url = ?, last_seen = ?, removed_at = NULL WHERE id = ?",
                    (e.title, e.subtitle, e.year, e.track_count, e.thumbnail_url, now, e.id),
                )

        for row in db.all("SELECT id, title FROM library_items WHERE removed_at IS NULL"):
            if row["id"] not in seen:
                db.execute("UPDATE library_items SET removed_at = ? WHERE id = ?", (now, row["id"]))
                result.removed.append(row["title"])

        db.set_setting("library_last_refresh", str(now))

    if first_refresh:
        db.log_event("library", f"Imported library: {result.total} albums and playlists")
    else:
        for e in result.added:
            db.log_event("library", f"New {e.kind} in library: {e.title}", json.dumps({"id": e.id}))
        for title in result.removed:
            db.log_event("library", f"No longer in library: {title}")
        for title in result.returned:
            db.log_event("library", f"Back in library: {title}")
    return result
