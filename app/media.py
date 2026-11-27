"""File layout, MP3 encoding and ID3 tagging."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX

MP3_QUALITIES = {
    "320": ["-b:a", "320k"],
    "256": ["-b:a", "256k"],
    "192": ["-b:a", "192k"],
    "V0": ["-q:a", "0"],
}
DEFAULT_QUALITY = "320"
MAX_COVER_BYTES = 10 * 1024 * 1024
PLAYLIST_DIR = "Playlists"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class TrackMeta:
    video_id: str
    title: str
    artists: str
    album: str
    album_artist: str
    track_number: int | None
    year: str | None
    cover_url: str | None


def safe_name(name: str, fallback: str = "Unknown", max_len: int = 120) -> str:
    """Make a string safe as a single path component on Linux, Windows and SMB shares."""
    name = unicodedata.normalize("NFC", name or "")
    name = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    if name in {"", ".", ".."}:
        name = fallback
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        name = f"_{name}"
    encoded = name.encode("utf-8")
    if len(encoded) > max_len:
        name = encoded[:max_len].decode("utf-8", errors="ignore").rstrip(". ")
    return name


def track_relpath(meta: TrackMeta) -> Path:
    """Jellyfin's preferred layout: Album Artist/Album/NN - Title.mp3"""
    artist_dir = safe_name(meta.album_artist, "Unknown Artist")
    album_dir = safe_name(meta.album, "Unknown Album")
    stem = safe_name(meta.title, meta.video_id)
    if meta.track_number:
        stem = f"{meta.track_number:02d} - {stem}"
    return Path(artist_dir) / album_dir / f"{stem}.mp3"


def ensure_inside(root: Path, rel: Path) -> Path:
    full = (root / rel).resolve()
    if not full.is_relative_to(root.resolve()):
        raise ValueError(f"Refusing to write outside the music folder: {rel}")
    return full


def big_cover_url(url: str | None, size: int = 1200) -> str | None:
    if not url:
        return None
    if "googleusercontent.com" in url or "ggpht.com" in url:
        return re.sub(r"=w\d+-h\d+.*$", f"=w{size}-h{size}-l90-rj", url) if "=w" in url else f"{url}=w{size}-h{size}"
    return url


def fetch_cover(url: str | None, timeout: float = 20) -> tuple[bytes, str] | None:
    if not url or not url.startswith("https://"):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only
        data = resp.read(MAX_COVER_BYTES + 1)
        if len(data) > MAX_COVER_BYTES:
            return None
        mime = resp.headers.get_content_type() or "image/jpeg"
    if not mime.startswith("image/"):
        return None
    return data, mime


def encode_mp3(src: Path, dest: Path, quality: str = DEFAULT_QUALITY) -> None:
    args = MP3_QUALITIES.get(quality, MP3_QUALITIES[DEFAULT_QUALITY])
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-codec:a", "libmp3lame", *args, "-f", "mp3", str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")


def write_tags(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None) -> None:
    tags = ID3()  # saving a fresh tag replaces whatever ffmpeg wrote
    tags.add(TIT2(encoding=3, text=meta.title))
    tags.add(TPE1(encoding=3, text=meta.artists or meta.album_artist))
    tags.add(TPE2(encoding=3, text=meta.album_artist))
    tags.add(TALB(encoding=3, text=meta.album))
    if meta.track_number:
        tags.add(TRCK(encoding=3, text=str(meta.track_number)))
    tags.add(TPOS(encoding=3, text="1"))
    if meta.year:
        tags.add(TDRC(encoding=3, text=str(meta.year)))
    tags.add(TXXX(encoding=3, desc="YouTube Video ID", text=meta.video_id))
    if cover:
        data, mime = cover
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(str(path), v2_version=3)


def write_m3u(music_dir: Path, playlist_title: str, playlist_id: str, entries: list[tuple[str, int, str]]) -> Path:
    """Write an extended M3U8 playlist with paths relative to the playlist file.

    ``entries`` is a list of (relative track path, duration seconds, display title).
    Returns the playlist path relative to the music folder.
    """
    rel = Path(PLAYLIST_DIR) / f"{safe_name(playlist_title, playlist_id)}.m3u8"
    full = ensure_inside(music_dir, rel)
    full.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist_title}"]
    for track_rel, duration, title in entries:
        lines.append(f"#EXTINF:{int(duration or -1)},{title}")
        lines.append(Path("..", track_rel).as_posix())
    tmp = full.with_suffix(".m3u8.partial")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, full)
    return rel


def remove_file(music_dir: Path, rel: str) -> None:
    """Delete a file in the music folder and prune now-empty parent folders."""
    full = ensure_inside(music_dir, Path(rel))
    if full.exists():
        full.unlink()
    parent = full.parent
    root = music_dir.resolve()
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            break
        parent = parent.parent
