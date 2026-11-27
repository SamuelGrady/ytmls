import io

from app.library import refresh_library
from app.security import Vault
from app.ytm import CookieError, LibraryEntry, parse_cookies, playlist_entry, album_entry

from .conftest import COOKIES, FakeYTM, csrf_from

import pytest


def test_parse_netscape_cookies_keeps_only_youtube():
    parsed = parse_cookies(COOKIES)
    assert "__Secure-3PAPISID=abc123" in parsed.cookie_header
    assert "SID=sidvalue" in parsed.cookie_header  # #HttpOnly_ lines are kept
    assert "NID" not in parsed.cookie_header
    assert parsed.count == 2


def test_parse_raw_cookie_header():
    parsed = parse_cookies("Cookie: SID=x; __Secure-3PAPISID=abc123")
    assert parsed.count == 2


def test_parse_cookies_requires_sapisid():
    with pytest.raises(CookieError):
        parse_cookies("SID=x; HSID=y")


def test_vault_roundtrip_and_wrong_key():
    from cryptography.fernet import Fernet
    v = Vault(Fernet.generate_key())
    token = v.encrypt("secret")
    assert "secret" not in token and v.decrypt(token) == "secret"
    with pytest.raises(ValueError):
        Vault(Fernet.generate_key()).decrypt(token)


def test_entry_parsing():
    a = album_entry({"browseId": "MPREb_x", "title": "T", "artists": [{"name": "A"}, {"name": "B"}], "year": 1999,
                     "thumbnails": [{"url": "s", "width": 60}, {"url": "l", "width": 544}]})
    assert (a.subtitle, a.year, a.thumbnail_url) == ("A, B", "1999", "l")
    p = playlist_entry({"playlistId": "LM", "title": "Liked Music", "count": "1,234 songs"})
    assert p.track_count == 1234
    assert album_entry({"title": "no id"}) is None


def test_refresh_tracks_new_removed_and_returned(app):
    db = app.state.db
    src = FakeYTM("__Secure-3PAPISID=abc123")
    first = refresh_library(db, src)
    assert first.total == 2
    # First import does not flag everything as new.
    assert db.one("SELECT COUNT(*) AS n FROM library_items WHERE reviewed = 0")["n"] == 0
    # Sync is off by default.
    assert db.one("SELECT COUNT(*) AS n FROM library_items WHERE sync_enabled = 1")["n"] == 0

    FakeYTM.playlists = [LibraryEntry("PL2", "playlist", "New Mix", "Me", None, 3, None)]
    second = refresh_library(db, src)
    assert [e.title for e in second.added] == ["New Mix"]
    assert second.removed == ["Road Trip"]
    row = db.one("SELECT * FROM library_items WHERE id = 'PL1'")
    assert row["removed_at"] is not None  # kept, not deleted
    assert db.one("SELECT reviewed FROM library_items WHERE id = 'PL2'")["reviewed"] == 0

    FakeYTM.playlists.append(LibraryEntry("PL1", "playlist", "Road Trip", "Me", None, 12, None))
    third = refresh_library(db, src)
    assert third.returned == ["Road Trip"]
    messages = [r["message"] for r in db.all("SELECT message FROM events")]
    assert "New playlist in library: New Mix" in messages
    assert "No longer in library: Road Trip" in messages


def test_save_cookies_imports_library_and_encrypts(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES}, follow_redirects=False)
    assert r.headers["location"] == "/settings?msg=cookies_saved"
    stored = app.state.db.get_setting("ytm_cookies_enc")
    assert stored and "abc123" not in stored
    page = authed.get("/")
    assert "Album One" in page.text and "Road Trip" in page.text


def test_cookie_file_upload(authed):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/youtube", data={"csrf_token": csrf},
                    files={"cookies_file": ("cookies.txt", io.BytesIO(COOKIES.encode()), "text/plain")},
                    follow_redirects=False)
    assert r.status_code == 303


def test_bad_cookies_are_not_saved(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    FakeYTM.fail = True
    r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
    assert r.status_code == 400 and "may have expired" in r.text
    assert app.state.db.get_setting("ytm_cookies_enc") is None


def test_toggle_sync_via_api(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
    r = authed.post("/api/items/PL1/sync", json={"enabled": True}, headers={"X-CSRF-Token": csrf})
    assert r.json() == {"id": "PL1", "enabled": True}
    assert app.state.db.one("SELECT sync_enabled FROM library_items WHERE id='PL1'")["sync_enabled"] == 1
    assert authed.post("/api/items/PL1/sync", json={"enabled": False}).status_code == 403
    assert authed.post("/api/items/nope/sync", json={"enabled": True},
                       headers={"X-CSRF-Token": csrf}).status_code == 404
    assert "Sync turned on: Road Trip" in authed.get("/history").text


def test_api_requires_login(client):
    assert client.post("/api/items/PL1/sync", json={"enabled": True}).status_code == 401
