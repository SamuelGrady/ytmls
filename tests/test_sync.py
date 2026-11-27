import shutil
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from app import media
from app.ytm import LibraryEntry

from .conftest import COOKIES, FakeDownloader, FakeYTM, csrf_from

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def enable(app, *ids):
    for i in ids:
        app.state.db.execute("UPDATE library_items SET sync_enabled = 1 WHERE id = ?", (i,))


@pytest.fixture
def ready(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES}, follow_redirects=False)
    assert r.status_code == 303
    return app


def music(app) -> Path:
    return app.state.settings.music_dir


def test_nothing_downloads_until_checked(ready):
    s = ready.state.engine.run("manual")
    assert s.downloaded == 0 and FakeDownloader.calls == []


def test_album_sync_layout_and_tags(ready):
    enable(ready, "MPREb_1")
    s = ready.state.engine.run("manual")
    assert s.downloaded == 2 and not s.failed, s
    f1 = music(ready) / "Artist A" / "Album One" / "01 - First Song.mp3"
    f2 = music(ready) / "Artist A" / "Album One" / "02 - Second_Song.mp3"
    assert f1.exists() and f2.exists()
    tags = ID3(f1)
    assert tags["TIT2"].text == ["First Song"]
    assert tags["TPE2"].text == ["Artist A"]
    assert tags["TALB"].text == ["Album One"]
    assert tags["TRCK"].text == ["1"]
    assert str(tags["TDRC"].text[0]) == "2020"
    assert tags.getall("APIC")[0].mime == "image/jpeg"
    assert tags.getall("TXXX:YouTube Video ID")[0].text == ["a1"]
    # The cookies file handed to yt-dlp contained the cookies and is cleaned up afterwards.
    assert all(c[2] == 1 for c in FakeDownloader.calls)
    assert not list((ready.state.settings.data_dir / "tmp").glob("cookies-*"))


def test_mp3_is_really_mp3(ready):
    enable(ready, "MPREb_1")
    ready.state.engine.run("manual")
    from mutagen.mp3 import MP3
    audio = MP3(music(ready) / "Artist A" / "Album One" / "01 - First Song.mp3")
    assert audio.info.bitrate == 320000


def test_playlist_writes_m3u_and_shares_files(ready):
    enable(ready, "MPREb_1", "PL1")
    s = ready.state.engine.run("manual")
    # a2 is in both the album and the playlist, but is downloaded once.
    assert s.downloaded == 3
    assert [c[0] for c in FakeDownloader.calls].count("a2") == 1
    m3u = (music(ready) / "Playlists" / "Road Trip.m3u8").read_text()
    assert "../Artist A/Album One/02 - Second_Song.mp3" in m3u
    assert "../Artist B/Music Video/Music Video.mp3" in m3u
    assert m3u.index("Second_Song") < m3u.index("Music Video.mp3")


def test_new_playlist_tracks_sync_automatically(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    FakeYTM.playlist_tracks["PL1"].append(
        {"videoId": "a1", "title": "First Song", "artists": [{"name": "Artist A"}],
         "album": {"name": "Album One", "id": "MPREb_1"}, "duration_seconds": 1})
    s = ready.state.engine.run("schedule")
    assert s.tracks_new == {"Road Trip": ["First Song"]}
    assert s.downloaded == 1
    assert (music(ready) / "Artist A" / "Album One" / "01 - First Song.mp3").exists()


def test_removed_track_waits_for_confirmation(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    path = music(ready) / "Artist B" / "Music Video" / "Music Video.mp3"
    assert path.exists()
    FakeYTM.playlist_tracks["PL1"].pop()  # drop the music video
    s = ready.state.engine.run("manual")
    assert s.removals_queued == ["Music Video"]
    assert path.exists()  # nothing deleted yet
    assert "Music Video" not in (music(ready) / "Playlists" / "Road Trip.m3u8").read_text()

    # Running again does not queue it twice.
    assert ready.state.engine.run("manual").removals_queued == []

    rid = ready.state.db.one("SELECT id, reason FROM removals WHERE decision IS NULL")
    assert rid["reason"] == "Removed from playlist Road Trip"
    assert ready.state.engine.decide_removal(rid["id"], delete=True) == "deleted"
    assert not path.exists()
    assert not path.parent.exists()  # empty folders are pruned


def test_keep_decision_stops_asking(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    FakeYTM.playlist_tracks["PL1"].pop()
    ready.state.engine.run("manual")
    rid = ready.state.db.one("SELECT id FROM removals WHERE decision IS NULL")["id"]
    ready.state.engine.decide_removal(rid, delete=False)
    assert (music(ready) / "Artist B" / "Music Video" / "Music Video.mp3").exists()
    assert ready.state.engine.run("manual").removals_queued == []


def test_track_returning_cancels_removal(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    gone = FakeYTM.playlist_tracks["PL1"].pop()
    ready.state.engine.run("manual")
    FakeYTM.playlist_tracks["PL1"].append(gone)
    s = ready.state.engine.run("manual")
    assert s.removals_cancelled == 1
    assert ready.state.db.one("SELECT COUNT(*) AS n FROM removals WHERE decision IS NULL")["n"] == 0


def test_unchecking_item_queues_tracks_and_playlist_file(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    ready.state.db.execute("UPDATE library_items SET sync_enabled = 0 WHERE id = 'PL1'")
    s = ready.state.engine.run("manual")
    assert sorted(s.removals_queued) == ["Music Video", "Playlist file: Road Trip", "Second/Song"]
    reasons = {r["reason"] for r in ready.state.db.all("SELECT reason FROM removals")}
    assert "Sync turned off for Road Trip" in reasons


def test_playlist_leaving_library_queues_removal(ready):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    FakeYTM.playlists = []
    s = ready.state.engine.run("manual")
    assert "Playlist file: Road Trip" in s.removals_queued
    assert "Road Trip is no longer in your library" in {
        r["reason"] for r in ready.state.db.all("SELECT reason FROM removals")}


def test_failed_download_is_retried_and_reported(ready):
    enable(ready, "MPREb_1")
    FakeDownloader.fail_ids = {"a2"}
    s = ready.state.engine.run("manual")
    assert s.failed == ["Second/Song"] and s.downloaded == 1
    row = ready.state.db.one("SELECT status, attempts, error FROM tracks WHERE video_id = 'a2'")
    assert (row["status"], row["attempts"]) == ("failed", 1) and "unavailable" in row["error"]
    FakeDownloader.fail_ids = set()
    assert ready.state.engine.run("manual").downloaded == 1


def test_per_run_limit_defers_the_rest(ready):
    ready.state.db.set_setting("max_downloads_per_run", "1")
    enable(ready, "MPREb_1")
    s = ready.state.engine.run("manual")
    assert s.downloaded == 1 and s.deferred == 1
    assert ready.state.engine.run("manual").downloaded == 1


def test_premium_toggle_is_passed_to_downloader(ready):
    ready.state.db.set_setting("premium_quality", "1")
    enable(ready, "MPREb_1")
    ready.state.engine.run("manual")
    assert all(c[1] is True for c in FakeDownloader.calls)


def test_existing_unrelated_file_is_not_overwritten(ready):
    target = music(ready) / "Artist A" / "Album One" / "01 - First Song.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user's own file")
    enable(ready, "MPREb_1")
    ready.state.engine.run("manual")
    assert target.read_bytes() == b"user's own file"
    assert (target.parent / "01 - First Song [a1].mp3").exists()


def test_run_without_cookies_records_error(app):
    s = app.state.engine.run("schedule")
    assert s.errors and "cookies" in s.errors[0]
    assert app.state.db.one("SELECT status FROM sync_runs")["status"] == "error"


def test_safe_name_and_path_guard(tmp_path):
    assert media.safe_name('AC/DC: "Live"?') == "AC_DC_ _Live__"
    assert media.safe_name("..") == "Unknown"
    assert media.safe_name("CON") == "_CON"
    with pytest.raises(ValueError):
        media.ensure_inside(tmp_path, Path("../escape.mp3"))


def test_big_cover_url():
    assert media.big_cover_url("https://lh3.googleusercontent.com/abc=w60-h60-l90-rj") == \
        "https://lh3.googleusercontent.com/abc=w1200-h1200-l90-rj"


# ------------------------------------------------------------ web


def test_removals_page_and_decide(ready, authed):
    enable(ready, "PL1")
    ready.state.engine.run("manual")
    FakeYTM.playlist_tracks["PL1"].pop()
    ready.state.engine.run("manual")
    page = authed.get("/removals")
    assert "Music Video" in page.text and "Removed from playlist Road Trip" in page.text
    rid = ready.state.db.one("SELECT id FROM removals WHERE decision IS NULL")["id"]
    csrf = csrf_from(page.text)
    r = authed.post("/removals/decide", data={"csrf_token": csrf, "action": "delete", "ids": [str(rid)]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert not (music(ready) / "Artist B").exists()
    assert "Deleted: Music Video" in authed.get("/history").text


def test_library_shows_status_and_counts(ready, authed):
    enable(ready, "MPREb_1")
    ready.state.engine.run("manual")
    page = authed.get("/").text
    assert "2/2 downloaded" in page
    assert "Last sync" in page and "2 downloaded" in page
    item = authed.get("/items/MPREb_1").text
    assert "Album One/01 - First Song.mp3" in item


def test_sync_settings_saved_and_clamped(ready, authed):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/sync", data={"csrf_token": csrf, "interval": "0.1", "quality": "V0",
                                        "premium": "1", "max_per_run": "99999"})
    db = ready.state.db
    assert db.get_setting("sync_interval_hours") == "1"
    assert db.get_setting("mp3_quality") == "V0"
    assert db.get_setting("premium_quality") == "1"
    assert db.get_setting("max_downloads_per_run") == "2000"


def test_sync_now_requires_csrf(ready, authed):
    assert authed.post("/sync/now", data={"csrf_token": "x"}).status_code == 403
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/sync/now", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/?msg=sync_started"


def test_status_api(ready, authed):
    st = authed.get("/api/status").json()
    assert st["running"] is False and st["next_run_in"] == "any moment"
