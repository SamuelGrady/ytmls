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
