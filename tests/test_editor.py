import io
import json
import shutil
import urllib.error
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from app.jellyfin import Jellyfin, JellyfinError, normalise_url

from .conftest import COOKIES, FakeYTM, csrf_from

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def synced(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
    app.state.db.execute("UPDATE library_items SET sync_enabled = 1")
    app.state.engine.run("manual")
    return app


def music(app) -> Path:
    return app.state.settings.music_dir


def test_edit_track_retags_and_moves(synced, authed):
    csrf = csrf_from(authed.get("/tracks/a1").text)
    r = authed.post("/tracks/a1", data={"csrf_token": csrf, "title": "First Song (Remastered)", "artists": "Artist A",
                                        "album": "Album One", "album_artist": "Artist A", "track_number": "7",
                                        "year": "2021"}, follow_redirects=False)
    assert r.headers["location"] == "/tracks/a1?msg=track_saved"
    old = music(synced) / "Artist A" / "Album One" / "01 - First Song.mp3"
    new = music(synced) / "Artist A" / "Album One" / "07 - First Song (Remastered).mp3"
    assert not old.exists() and new.exists()
    tags = ID3(new)
    assert tags["TIT2"].text == ["First Song (Remastered)"]
    assert tags["TRCK"].text == ["7"] and str(tags["TDRC"].text[0]) == "2021"
    assert tags.getall("APIC")  # existing cover kept
    row = synced.state.db.one("SELECT * FROM tracks WHERE video_id = 'a1'")
    assert row["meta_locked"] == 1 and row["file_path"].endswith("07 - First Song (Remastered).mp3")
    assert "Edited First Song" in authed.get("/history").text


def test_edits_survive_the_next_sync(synced):
    synced.state.editor.edit_track("v9", {"title": "My Name", "artists": "B", "album": "Mine", "album_artist": "B",
                                          "track_number": "", "year": ""})
    # Simulate it being re-downloaded later: metadata from YouTube must not overwrite the edit.
    (music(synced) / "B" / "Mine" / "My Name.mp3").unlink()
    synced.state.db.execute("UPDATE tracks SET status = 'removed', file_path = NULL WHERE video_id = 'v9'")
    synced.state.engine.run("manual")
    row = synced.state.db.one("SELECT title, album, file_path FROM tracks WHERE video_id = 'v9'")
    assert row["title"] == "My Name" and row["file_path"] == "B/Mine/My Name.mp3"


def test_playlist_follows_moved_file(synced):
    synced.state.editor.edit_track("a2", {"title": "Renamed", "artists": "Artist A", "album": "Album One",
                                          "album_artist": "Artist A", "track_number": "2", "year": "2020"})
    m3u = (music(synced) / "Playlists" / "Road Trip.m3u8").read_text()
    assert "02 - Renamed.mp3" in m3u and "Second_Song" not in m3u


def test_cover_upload_and_reset(synced, authed, monkeypatch):
    csrf = csrf_from(authed.get("/tracks/a1").text)
    data = {"csrf_token": csrf, "title": "First Song", "artists": "Artist A", "album": "Album One",
            "album_artist": "Artist A", "track_number": "1", "year": "2020"}
    authed.post("/tracks/a1", data=data, files={"cover": ("c.png", io.BytesIO(PNG), "image/png")})
    f = music(synced) / "Artist A" / "Album One" / "01 - First Song.mp3"
    assert ID3(f).getall("APIC")[0].mime == "image/png"
    img = authed.get("/covers/a1")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    authed.post("/tracks/a1", data={**data, "reset_cover": "1"})
    assert ID3(f).getall("APIC")[0].mime == "image/jpeg"  # back to the (stubbed) YouTube cover


def test_bad_inputs_rejected(synced, authed):
    csrf = csrf_from(authed.get("/tracks/a1").text)
    base = {"csrf_token": csrf, "title": "x", "artists": "", "album": "A", "album_artist": "B", "track_number": "", "year": ""}
    assert "Year must be four digits" in authed.post("/tracks/a1", data={**base, "year": "21"}).text
    assert "Track number" in authed.post("/tracks/a1", data={**base, "track_number": "-1"}).text
    assert "can&#39;t be empty" in authed.post("/tracks/a1", data={**base, "title": " "}).text
    r = authed.post("/tracks/a1", data=base, files={"cover": ("x.gif", io.BytesIO(b"GIF89a..."), "image/gif")})
    assert "JPEG or PNG" in r.text
    assert authed.post("/tracks/a1", data={**base, "csrf_token": "nope"}).status_code == 403


def test_path_fields_cannot_escape_music_folder(synced):
    synced.state.editor.edit_track("a1", {"title": "../../etc/passwd", "artists": "x", "album": "..",
                                          "album_artist": "../..", "track_number": "", "year": ""})
    row = synced.state.db.one("SELECT file_path FROM tracks WHERE video_id = 'a1'")
    full = (music(synced) / row["file_path"]).resolve()
    assert full.is_relative_to(music(synced).resolve()) and full.exists()


def test_album_edit_applies_to_all_tracks(synced, authed):
    csrf = csrf_from(authed.get("/items/MPREb_1").text)
    r = authed.post("/items/MPREb_1/album", data={"csrf_token": csrf, "album": "Album One (Deluxe)",
                                                  "album_artist": "Artist A", "year": "2020"},
                    follow_redirects=False)
    assert r.headers["location"] == "/items/MPREb_1?msg=album_saved"
    folder = music(synced) / "Artist A" / "Album One (Deluxe)"
    assert sorted(p.name for p in folder.iterdir()) == ["01 - First Song.mp3", "02 - Second_Song.mp3"]
    assert not (music(synced) / "Artist A" / "Album One").exists()


def test_edit_refused_while_syncing(synced, authed):
    csrf = csrf_from(authed.get("/tracks/a1").text)
    with synced.state.engine.exclusive():
        r = authed.post("/tracks/a1", data={"csrf_token": csrf, "title": "x", "artists": "", "album": "A",
                                            "album_artist": "B", "track_number": "", "year": ""})
    assert "A sync is running" in r.text


def test_tracks_search(synced, authed):
    page = authed.get("/tracks?q=second").text
    assert "Second/Song" in page and "First Song" not in page


# ------------------------------------------------------------ jellyfin


class FakeResp:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_jellyfin_url_validation():
    assert normalise_url("http://jellyfin:8096/") == "http://jellyfin:8096"
    for bad in ("jellyfin:8096", "ftp://x", "http://user:pw@host"):
        with pytest.raises(JellyfinError):
            normalise_url(bad)


def test_jellyfin_test_and_refresh(app):
    calls = []

    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url, req.headers.get("Authorization")))
        return FakeResp(json.dumps({"ServerName": "Home", "Version": "10.11.0"}).encode())

    jf = Jellyfin(app.state.db, app.state.notifier.vault, opener=opener)
    jf.save("http://jellyfin:8096", "a" * 32)
    assert jf.test() == "Home 10.11.0"
    jf.refresh_now()
    assert calls[-1][:2] == ("POST", "http://jellyfin:8096/Library/Refresh")
    assert 'Token="' + "a" * 32 in calls[-1][2]
    assert "aaaa" not in (app.state.db.get_setting("jellyfin_key_enc") or "")


def test_jellyfin_bad_key_reported(app):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    jf = Jellyfin(app.state.db, app.state.notifier.vault, opener=opener)
    jf.save("http://jellyfin:8096", "b" * 32)
    with pytest.raises(JellyfinError, match="rejected the API key"):
        jf.test()
    jf.refresh_now()
    assert json.loads(app.state.db.get_setting("jellyfin_last_refresh"))["ok"] is False


def test_jellyfin_settings_form_reports_errors(authed):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/jellyfin", data={"csrf_token": csrf, "url": "not a url", "api_key": "x"})
    assert r.status_code == 400 and "http://" in r.text


# ------------------------------------------------------------ checklist


def test_security_checklist(authed):
    page = authed.get("/settings").text
    assert "Security checklist" in page
    assert "Two-factor authentication is on" in page and "Turn it on in the Two-factor section" in page
    tunnel = authed.get("/settings", headers={"X-Forwarded-Proto": "https",
                                              "Cf-Access-Authenticated-User-Email": "me@example.com"}).text
    assert "Use your Cloudflare Tunnel address" not in tunnel
    assert "Add a Cloudflare Access application" not in tunnel


def test_large_cover_upload_accepted(synced, authed):
    """Regression: the global request-size cap used to reject covers over ~576 KB."""
    big = PNG + b"\x00" * (3 * 1024 * 1024)
    csrf = csrf_from(authed.get("/tracks/a1").text)
    data = {"csrf_token": csrf, "title": "First Song", "artists": "Artist A", "album": "Album One",
            "album_artist": "Artist A", "track_number": "1", "year": "2020"}
    r = authed.post("/tracks/a1", data=data, files={"cover": ("c.png", io.BytesIO(big), "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303
    f = music(synced) / "Artist A" / "Album One" / "01 - First Song.mp3"
    assert len(ID3(f).getall("APIC")[0].data) == len(big)
    csrf = csrf_from(authed.get("/items/MPREb_1").text)
    r = authed.post("/items/MPREb_1/album", data={"csrf_token": csrf, "album": "Album One",
                                                  "album_artist": "Artist A", "year": "2020"},
                    files={"cover": ("c.png", io.BytesIO(big), "image/png")}, follow_redirects=False)
    assert r.status_code == 303


def test_oversized_requests_still_rejected(synced, authed):
    csrf = csrf_from(authed.get("/tracks/a1").text)
    huge = PNG + b"\x00" * (11 * 1024 * 1024)
    r = authed.post("/tracks/a1", data={"csrf_token": csrf}, files={"cover": ("c.png", io.BytesIO(huge), "image/png")})
    assert r.status_code == 413
    r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": "x" * (2 * 1024 * 1024)})
    assert r.status_code == 413
