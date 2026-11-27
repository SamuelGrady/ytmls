"""Guards the promise that YTMLS never changes the user's YouTube account."""

import re
from pathlib import Path

# Every ytmusicapi method that writes to the account.
MUTATING = {
    "rate_song", "rate_playlist", "edit_song_library_status", "edit_playlist", "create_playlist",
    "delete_playlist", "add_playlist_items", "remove_playlist_items", "subscribe_artists",
    "unsubscribe_artists", "add_history_item", "remove_history_items", "upload_song",
    "delete_upload_entity", "subscribe_artist", "remove_search_suggestions",
}

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_no_mutating_ytmusicapi_calls():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text()
        for name in MUTATING:
            if re.search(rf"\b{name}\b", text):
                offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders


def test_mutating_list_covers_installed_ytmusicapi():
    from ytmusicapi import YTMusic
    suspicious = {
        m for m in dir(YTMusic)
        if re.match(r"(rate|edit|create|delete|add|remove|subscribe|unsubscribe|upload)_", m)
    }
    missing = suspicious - MUTATING
    assert not missing, f"New account-changing methods in ytmusicapi; add them to MUTATING: {missing}"


def test_downloader_never_marks_videos_watched(monkeypatch, tmp_path):
    """yt-dlp can report playback to YouTube; make sure that stays off."""
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            out = tmp_path / "x.webm"
            out.write_bytes(b"x")
            return {"requested_downloads": [{"filepath": str(out)}], "format_id": "251"}

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    from app.downloader import YtDlpDownloader
    YtDlpDownloader().fetch_audio("x", tmp_path, tmp_path / "c.txt", premium=False)
    assert captured["mark_watched"] is False
    assert captured["format"] == "bestaudio/best"
    assert "postprocessors" not in captured  # yt-dlp never re-encodes; the stream is only remuxed
    YtDlpDownloader().fetch_audio("x", tmp_path, tmp_path / "c.txt", premium=True)
    assert captured["format"].startswith("774/141/")
