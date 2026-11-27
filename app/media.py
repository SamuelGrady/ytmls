"""File layout, keeping YouTube's audio stream as it is, and tagging."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus

# YouTube's streams are already lossy, so YTMLS never re-encodes them. Each codec
# is copied as it is into a container Jellyfin reads and that can hold tags:
# codec name (as ffprobe reports it) -> (file suffix, ffmpeg muxer).
CONTAINERS = {
    "opus": (".opus", "opus"),  # Ogg Opus: the usual stream (251, or 774 with Premium)
    "aac": (".m4a", "ipod"),    # MP4 audio: 140, or 141 with Premium
    "mp3": (".mp3", "mp3"),
}
AUDIO_SUFFIXES = {".opus", ".m4a", ".mp3"}
DEFAULT_SUFFIX = ".opus"
MP4_VIDEO_ID = "----:com.apple.iTunes:YouTube Video ID"
MAX_COVER_BYTES = 10 * 1024 * 1024
PLAYLIST_DIR = "Playlists"
ARTIST_NFO = "artist.nfo"
NFO_MARKER = "<!-- Written by YTMLS so Jellyfin treats this folder as an artist, not an album. -->"

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
    # Kept out of Jellyfin's albums: stored as Artist/Title.opus with no album tag.
    loose: bool = False


def folder_problem(music_dir: Path) -> str | None:
    """Return a human explanation if the music folder can't be written, else None."""
    uid, gid = os.getuid(), os.getgid()
    who = f"user {uid}:{gid}"
    if not music_dir.exists():
        try:
            music_dir.mkdir(parents=True)
        except OSError:
            pass
    if not music_dir.exists():
        return (f"The music folder {music_dir} doesn't exist inside the container. "
                "Check the music volume line in compose.yaml.")
    if not music_dir.is_dir():
        return f"{music_dir} isn't a folder. Check the music volume line in compose.yaml."
    probe = music_dir / f".ytmls-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return None
    except OSError as exc:
        st = music_dir.stat()
        owner = f"{st.st_uid}:{st.st_gid}"
        mode = oct(st.st_mode & 0o777)[2:]
        msg = (f"YTMLS runs as {who} but can't write to {music_dir} "
               f"(folder owner {owner} as seen inside the container, mode {mode}; error: {exc.strerror}).")
        if st.st_uid != uid:
            msg += (f" Set user: \"{st.st_uid}:{st.st_gid}\" in compose.yaml, or chown the folder on the host"
                    " to the user in compose.yaml.")
        else:
            msg += (" The owner matches, so something outside the folder's permissions is blocking writes: "
                    "on SELinux systems (Fedora, RHEL, Unraid with SELinux) add :z to the volume, e.g. "
                    "/srv/music:/music:z; a volume ending in :ro is read-only; a network share (NFS/SMB) may "
                    "map users differently; rootless Docker/Podman remaps user IDs.")
        return msg


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


def track_relpath(meta: TrackMeta, suffix: str = DEFAULT_SUFFIX) -> Path:
    """Jellyfin's preferred layout: Album Artist/Album/NN - Title.opus

    Loose tracks (singles, when that setting is on) go straight into the artist
    folder as Album Artist/Title.opus. ``suffix`` is the file's audio format.
    """
    artist_dir = safe_name(meta.album_artist, "Unknown Artist")
    stem = safe_name(meta.title, meta.video_id)
    if meta.loose:
        return Path(artist_dir) / f"{stem}{suffix}"
    album_dir = safe_name(meta.album, "Unknown Album")
    if meta.track_number:
        stem = f"{meta.track_number:02d} - {stem}"
    return Path(artist_dir) / album_dir / f"{stem}{suffix}"


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


def probe_codec(src: Path) -> str:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
           "-of", "json", str(src)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[-500:]}")
    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams:
        raise RuntimeError("The download has no audio stream")
    return str(streams[0].get("codec_name") or "")


def remux_audio(src: Path, workdir: Path) -> Path:
    """Copy the audio stream, untouched, into a taggable container. Returns the new file.

    YouTube sends Opus inside WebM and AAC inside fragmented MP4, which most taggers
    and some players handle badly. Moving the stream into Ogg Opus or a plain M4A
    keeps every bit of the audio: nothing is decoded or encoded.
    """
    codec = probe_codec(src)
    if codec not in CONTAINERS:
        raise RuntimeError(f"YouTube sent {codec or 'an unknown'} audio, which YTMLS can't store without "
                           "re-encoding")
    suffix, muxer = CONTAINERS[codec]
    dest = workdir / f"out{suffix}"
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-codec:a", "copy"]
    if muxer == "ipod":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", muxer, str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    return dest


def write_tags(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None) -> None:
    """Replace a file's tags with ``meta`` (and ``cover``), in the file's own tag format."""
    suffix = path.suffix.lower()
    if suffix == ".opus":
        _write_vorbis(path, meta, cover)
    elif suffix == ".m4a":
        _write_mp4(path, meta, cover)
    else:
        _write_id3(path, meta, cover)


def _write_id3(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None) -> None:
    tags = ID3()  # saving a fresh tag replaces whatever was there
    tags.add(TIT2(encoding=3, text=meta.title))
    tags.add(TPE1(encoding=3, text=meta.artists or meta.album_artist))
    tags.add(TPE2(encoding=3, text=meta.album_artist))
    if not meta.loose:
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


def _write_vorbis(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None) -> None:
    audio = OggOpus(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    audio["TITLE"] = meta.title
    audio["ARTIST"] = meta.artists or meta.album_artist
    audio["ALBUMARTIST"] = meta.album_artist
    if not meta.loose:
        audio["ALBUM"] = meta.album
        if meta.track_number:
            audio["TRACKNUMBER"] = str(meta.track_number)
        audio["DISCNUMBER"] = "1"
    if meta.year:
        audio["DATE"] = str(meta.year)
    audio["YOUTUBE_VIDEO_ID"] = meta.video_id
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = cover[1]
        pic.desc = "Cover"
        pic.data = cover[0]
        audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


def _write_mp4(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None) -> None:
    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    audio["\xa9nam"] = [meta.title]
    audio["\xa9ART"] = [meta.artists or meta.album_artist]
    audio["aART"] = [meta.album_artist]
    if not meta.loose:
        audio["\xa9alb"] = [meta.album]
        if meta.track_number:
            audio["trkn"] = [(meta.track_number, 0)]
        audio["disk"] = [(1, 0)]
    if meta.year:
        audio["\xa9day"] = [str(meta.year)]
    audio[MP4_VIDEO_ID] = [MP4FreeForm(meta.video_id.encode("utf-8"))]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if cover[1] == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover[0], imageformat=fmt)]
    audio.save()


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def read_cover(path: Path) -> tuple[bytes, str] | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".opus":
            for value in OggOpus(str(path)).get("METADATA_BLOCK_PICTURE", []):
                pic = Picture(base64.b64decode(value))
                return pic.data, pic.mime or sniff_image(pic.data) or "image/jpeg"
            return None
        if suffix == ".m4a":
            covers = (MP4(str(path)).tags or {}).get("covr") or []
            if not covers:
                return None
            data = bytes(covers[0])
            return data, "image/png" if covers[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        pics = ID3(str(path)).getall("APIC")
    except Exception:
        return None
    return (pics[0].data, pics[0].mime) if pics else None


def retag(path: Path, meta: TrackMeta, cover: tuple[bytes, str] | None = None) -> None:
    """Rewrite a file's tags, keeping its current cover unless a new one is given."""
    write_tags(path, meta, cover if cover is not None else read_cover(path))


def move_track(music_dir: Path, old_rel: str, new_rel: Path) -> None:
    """Move a track within the music folder and prune folders left empty."""
    src = ensure_inside(music_dir, Path(old_rel))
    dest = ensure_inside(music_dir, new_rel)
    if src == dest:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    prune_empty(music_dir, src.parent)


def _is_our_nfo(path: Path) -> bool:
    try:
        return NFO_MARKER in path.read_text(encoding="utf-8", errors="ignore")[:2000]
    except OSError:
        return False


def prune_empty(music_dir: Path, folder: Path) -> None:
    """Remove folders left empty, walking up towards the music folder.

    A folder holding nothing but the artist.nfo YTMLS wrote counts as empty.
    """
    root = music_dir.resolve()
    parent = folder
    while parent != root and parent.is_relative_to(root):
        nfo = parent / ARTIST_NFO
        try:
            if [p.name for p in parent.iterdir()] == [ARTIST_NFO] and _is_our_nfo(nfo):
                nfo.unlink()
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def ensure_artist_nfo(music_dir: Path, artist_dir: Path, artist: str) -> None:
    """Make sure Jellyfin reads this folder as an artist.

    Jellyfin turns any folder holding songs into an album, unless it also holds an
    album folder or an artist.nfo. Without this, an artist you only have singles
    from would show up in Albums as one album named after the artist.
    """
    folder = ensure_inside(music_dir, artist_dir)
    nfo = folder / ARTIST_NFO
    if nfo.exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    name = artist.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    nfo.write_text('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
                   f"{NFO_MARKER}\n<artist>\n  <name>{name}</name>\n</artist>\n", encoding="utf-8")


def remove_our_nfo_if_unneeded(music_dir: Path, artist_dir: Path) -> None:
    """Drop the artist.nfo YTMLS wrote once no loose songs are left in the folder."""
    folder = ensure_inside(music_dir, artist_dir)
    nfo = folder / ARTIST_NFO
    if not nfo.exists() or not _is_our_nfo(nfo):
        return
    if any(p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES for p in folder.iterdir()):
        return
    nfo.unlink()
    prune_empty(music_dir, folder)


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
    prune_empty(music_dir, full.parent)
