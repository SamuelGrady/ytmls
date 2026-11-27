"""Fetch audio with yt-dlp and place it, tagged but never re-encoded, in the music folder."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from . import media
from .media import TrackMeta

# yt-dlp format selectors. Without Premium the best stream YouTube Music offers is
# itag 251, Opus at about 130-160 kbps. Premium accounts can also get itag 774
# (Opus, about 256 kbps) and 141 (AAC, 256 kbps); either falls back to the best
# stream when the account doesn't get them.
FORMAT_STANDARD = "bestaudio/best"
FORMAT_PREMIUM = "774/141/bestaudio/best"


class Downloader(Protocol):
    def fetch_audio(self, video_id: str, workdir: Path, cookies_file: Path, premium: bool) -> tuple[Path, str]:
        """Download the best audio stream into workdir. Returns (path, description)."""
        ...


class YtDlpDownloader:
    def fetch_audio(self, video_id: str, workdir: Path, cookies_file: Path, premium: bool) -> tuple[Path, str]:
        from yt_dlp import YoutubeDL

        opts = {
            "format": FORMAT_PREMIUM if premium else FORMAT_STANDARD,
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "cookiefile": str(cookies_file),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            # Read-only: never report playback to YouTube (no watch history).
            "mark_watched": False,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "cachedir": str(workdir / ".cache"),
            "overwrites": True,
        }
        url = f"https://music.youtube.com/watch?v={video_id}"
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"]) if info.get("requested_downloads") else None
        if path is None or not path.exists():
            candidates = [p for p in workdir.glob(f"{video_id}.*") if p.is_file()]
            if not candidates:
                raise RuntimeError("yt-dlp finished without producing a file")
            path = candidates[0]
        desc = f"{info.get('format_id')} {info.get('acodec')} {round(info.get('abr') or 0)}kbps"
        return path, desc


def produce_track(
    downloader: Downloader,
    meta: TrackMeta,
    music_dir: Path,
    place: Callable[[str], Path],
    cookies_file: Path,
    *,
    premium: bool,
    tmp_root: Path,
    cover_override: tuple[bytes, str] | None = None,
) -> tuple[Path, str]:
    """Download, tag and atomically place one track.

    The audio stream is kept exactly as YouTube sent it. ``place`` maps the file's
    suffix (which depends on the stream that came back) to its path in the music
    folder. Returns (that path, a source description).
    """
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="dl-") as tmp:
        workdir = Path(tmp)
        src, desc = downloader.fetch_audio(meta.video_id, workdir, cookies_file, premium)
        out = media.remux_audio(src, workdir)
        rel_path = place(out.suffix)
        dest = media.ensure_inside(music_dir, rel_path)
        cover = cover_override
        if cover is None:
            try:
                cover = media.fetch_cover(media.big_cover_url(meta.cover_url))
            except Exception:
                cover = None
        media.write_tags(out, meta, cover)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        # Copy onto the music volume first, then rename, so Jellyfin never sees half a file.
        with open(out, "rb") as fin, open(partial, "wb") as fout:
            while chunk := fin.read(1024 * 1024):
                fout.write(chunk)
        os.replace(partial, dest)
    return rel_path, desc
