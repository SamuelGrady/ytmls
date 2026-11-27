"""The stored YouTube cookies are kept fresh and their expiry is noticed."""

import http.cookiejar
import shutil

import pytest

from app.ytm import merge_cookies, netscape_from_jar, parse_cookies

from .conftest import COOKIES, FakeDownloader, FakePush, FakeYTM, csrf_from
from .test_notify import SUB

NOW = 1_800_000_000


def line(name, value, exp=1893456000, domain=".youtube.com"):
    return f"{domain}\tTRUE\t/\tTRUE\t{exp}\t{name}\t{value}\n"


def stored(app):
    return app.state.engine.cookies.netscape()


@pytest.fixture
def ready(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES}, follow_redirects=False)
    assert r.status_code == 303
    authed.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-CSRF-Token": csrf})
    FakePush.sent.clear()
    return app


# ------------------------------------------------------------ merging


def test_merge_updates_and_adds_values():
    merged = merge_cookies(COOKIES, line("SID", "newsid") + line("SIDCC", "cc1"), now=NOW)
    names = {c.split("\t")[5]: c.split("\t")[6] for c in merged.splitlines() if "\t" in c}
    assert names == {"__Secure-3PAPISID": "abc123", "SID": "newsid", "SIDCC": "cc1"}


def test_merge_keeps_expiry_for_session_values():
    merged = merge_cookies(COOKIES, line("SID", "newsid", exp=0), now=NOW)
    assert "\t1893456000\tSID\tnewsid" in merged


def test_merge_ignores_deletions_and_no_change():
    assert merge_cookies(COOKIES, line("SID", "sidvalue"), now=NOW) is None
    assert merge_cookies(COOKIES, line("SID", "", exp=0), now=NOW) is None
    assert merge_cookies(COOKIES, line("SID", "gone", exp=NOW - 10), now=NOW) is None
    # Other sites are never stored.
    assert merge_cookies(COOKIES, line("NID", "x", domain=".google.com"), now=NOW) is None


def test_netscape_from_jar_keeps_only_youtube():
    jar = http.cookiejar.CookieJar()
    for domain, name in ((".youtube.com", "SIDCC"), (".google.com", "NID")):
        jar.set_cookie(http.cookiejar.Cookie(
            0, name, "v", None, False, domain, True, True, "/", True, True, 1893456000, False, None, None, {}))
    text = netscape_from_jar(jar)
    assert "SIDCC" in text and "NID" not in text


# ------------------------------------------------------------ saving refreshed values


def test_library_read_saves_refreshed_cookies(ready, monkeypatch):
    monkeypatch.setattr(FakeYTM, "refreshed_cookies", lambda self: line("SIDCC", "fromread"), raising=False)
    ready.state.engine.run("manual")
    assert "fromread" in stored(ready)
    parse_cookies(stored(ready))  # still a valid, complete set


def test_ytdlp_rotated_cookies_are_saved(ready, monkeypatch):
    from .test_sync import enable

    def fetch(self, video_id, workdir, cookies_file, premium):
        # yt-dlp writes the cookie jar back to the file when it finishes.
        cookies_file.write_text(cookies_file.read_text() + line("__Secure-3PSIDCC", "rotated"))
        return original(self, video_id, workdir, cookies_file, premium)

    original = FakeDownloader.fetch_audio
    monkeypatch.setattr(FakeDownloader, "fetch_audio", fetch)
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    enable(ready, "MPREb_1")
    ready.state.engine.run("manual")
    assert "rotated" in stored(ready)
    assert not list((ready.state.settings.data_dir / "tmp").glob("cookies-*"))


def test_refresh_never_overwrites_a_new_upload(ready):
    store = ready.state.engine.cookies
    _, old_gen = store.load()
    store.replace(COOKIES.replace("sidvalue", "uploadedlater"))
    assert store.save_refreshed(line("SID", "stale"), old_gen) is False
    assert "uploadedlater" in stored(ready) and "stale" not in stored(ready)


# ------------------------------------------------------------ keep-alive and alerts


def test_keep_alive_uses_a_read_only_check(ready, monkeypatch):
    calls = []
    monkeypatch.setattr(FakeYTM, "ping", lambda self: calls.append(1), raising=False)
    monkeypatch.setattr(FakeYTM, "refreshed_cookies", lambda self: line("SIDCC", "alive"), raising=False)
    store = ready.state.engine.cookies
    ready.state.db.set_setting("ytm_last_check", "0")
    assert store.keepalive_due()
    assert ready.state.engine.keep_alive() is True
    assert calls == [1] and "alive" in stored(ready)
    assert not store.keepalive_due()


def test_one_alert_after_two_failed_checks_then_recovery(ready, authed):
    engine = ready.state.engine
    FakeYTM.fail = True
    assert engine.keep_alive() is False
    assert FakePush.sent == []  # one failure could be a network blip
    assert ready.state.db.get_setting("ytm_status") == "ok"
    assert engine.keep_alive() is False
    engine.run("schedule")
    assert [m["title"] for m in FakePush.sent] == ["YouTube Music sign-in expired", "Sync failed"]
    assert FakePush.sent[0]["url"] == "/settings#youtube"
    assert "Needs new cookies" in authed.get("/settings").text

    FakeYTM.fail = False
    FakePush.sent.clear()
    assert engine.keep_alive() is True
    assert ready.state.db.get_setting("ytm_status") == "ok"
    assert "Connected" in authed.get("/settings").text
    FakeYTM.fail = True
    engine.keep_alive()
    engine.keep_alive()
    assert [m["title"] for m in FakePush.sent] == ["YouTube Music sign-in expired"]  # alerts again next time


def test_rejected_upload_does_not_count_against_saved_cookies(ready, authed):
    FakeYTM.fail = True
    csrf = csrf_from(authed.get("/settings").text)
    for _ in range(3):
        r = authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
        assert r.status_code == 400
    assert FakePush.sent == [] and ready.state.db.get_setting("ytm_fail_streak") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_downloads_stop_cleanly_when_youtube_rejects_the_sign_in(ready, monkeypatch):
    from .test_sync import enable

    def fetch(self, video_id, workdir, cookies_file, premium):
        FakeDownloader.calls.append(video_id)
        raise RuntimeError("ERROR: [youtube] a1: Sign in to confirm you're not a bot. Use --cookies ...")

    monkeypatch.setattr(FakeDownloader, "fetch_audio", fetch)
    enable(ready, "MPREb_1")
    s = ready.state.engine.run("manual")
    assert FakeDownloader.calls == ["a1"]  # stopped after the first, didn't hammer YouTube
    assert s.deferred == 1
    assert any("upload fresh ones" in e for e in s.errors)
    assert ready.state.db.get_setting("ytm_fail_streak") == "1"


def test_removing_cookies_clears_keepalive_state(ready, authed):
    ready.state.db.set_setting("ytm_fail_streak", "1")
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/youtube/remove", data={"csrf_token": csrf})
    assert ready.state.db.get_setting("ytm_fail_streak") is None
    assert not ready.state.engine.cookies.keepalive_due()
    assert ready.state.engine.keep_alive() is None
