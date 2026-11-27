import shutil

import pytest

from app.notify import valid_endpoint
from app.ytm import LibraryEntry

from .conftest import COOKIES, FakePush, FakeYTM, csrf_from

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk" + "a" * 80, "auth": "x" * 22}}

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


@pytest.fixture
def ready(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
    authed.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-CSRF-Token": csrf})
    return app


def test_endpoint_allowlist():
    assert valid_endpoint("https://fcm.googleapis.com/fcm/send/x")
    assert valid_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert valid_endpoint("https://web.push.apple.com/x")
    assert not valid_endpoint("http://fcm.googleapis.com/x")
    assert not valid_endpoint("https://169.254.169.254/latest")
    assert not valid_endpoint("https://evilgoogleapis.com/x")
    assert not valid_endpoint("https://googleapis.com.evil.net/x")


def test_public_key_is_stable_and_encrypted(app):
    n = app.state.notifier
    k1 = n.public_key()
    assert len(k1) == 87  # 65-byte uncompressed P-256 point, base64url
    assert n.public_key() == k1
    assert "PRIVATE KEY" not in app.state.db.get_setting("vapid_private_enc")


def test_subscribe_requires_csrf_and_valid_endpoint(authed, app):
    assert authed.post("/api/push/subscribe", json={"subscription": SUB}).status_code == 403
    csrf = csrf_from(authed.get("/settings").text)
    bad = {"endpoint": "https://example.com/x", "keys": SUB["keys"]}
    r = authed.post("/api/push/subscribe", json={"subscription": bad}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    r = authed.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-CSRF-Token": csrf,
                    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/131"})
    assert r.json() == {"ok": True}
    assert "Chrome on Android" in authed.get("/settings").text


def test_test_notification(ready, authed):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/push/test", data={"csrf_token": csrf})
    assert FakePush.sent[-1]["title"] == "YTMLS test"
    assert FakePush.sent[-1]["sub"] == "mailto:ytmls@example.com"


def test_new_library_item_notifies_on_refresh(ready, authed):
    FakePush.sent.clear()
    FakeYTM.playlists.append(LibraryEntry("PL9", "playlist", "Fresh Finds", "Me", None, 5, None))
    csrf = csrf_from(authed.get("/").text)
    authed.post("/library/refresh", data={"csrf_token": csrf})
    assert len(FakePush.sent) == 1
    msg = FakePush.sent[0]
    assert msg["title"] == "1 new item in your library"
    assert "Fresh Finds" in msg["body"] and msg["url"] == "/?filter=new"


def test_first_import_does_not_notify(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-CSRF-Token": csrf})
    authed.post("/settings/youtube", data={"csrf_token": csrf, "cookies_text": COOKIES})
    assert FakePush.sent == []


@needs_ffmpeg
def test_sync_summary_notification(ready):
    ready.state.db.execute("UPDATE library_items SET sync_enabled = 1 WHERE id = 'PL1'")
    FakePush.sent.clear()
    ready.state.engine.run("schedule")
    assert len(FakePush.sent) == 1
    msg = FakePush.sent[0]
    assert msg["title"] == "Sync finished"
    assert msg["body"].startswith("2 downloaded")

    # A song leaves the playlist: the summary says a removal needs attention and links there.
    FakeYTM.playlist_tracks["PL1"].pop()
    FakePush.sent.clear()
    ready.state.engine.run("schedule")
    msg = FakePush.sent[0]
    assert "1 removal needs your OK" in msg["body"] and msg["url"] == "/removals"


def test_new_item_found_during_sync_gets_its_own_notification(ready):
    FakePush.sent.clear()
    FakeYTM.albums.append(LibraryEntry("MPREb_2", "album", "Second Album", "Artist C", "2024", None, None))
    ready.state.engine.run("schedule")
    titles = [m["title"] for m in FakePush.sent]
    assert "1 new item in your library" in titles and "Sync finished" in titles
    sync_msg = next(m for m in FakePush.sent if m["title"] == "Sync finished")
    assert "New in library" not in sync_msg["body"]


def test_quiet_when_unchanged(ready, authed):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/settings/notifications", data={"csrf_token": csrf, "quiet": "1"})
    FakePush.sent.clear()
    ready.state.engine.run("schedule")
    assert FakePush.sent == []


def test_every_sync_notifies_by_default(ready):
    FakePush.sent.clear()
    ready.state.engine.run("schedule")
    assert FakePush.sent[0]["body"] == "No changes"


def test_sync_error_notifies(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    authed.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-CSRF-Token": csrf})
    app.state.engine.run("schedule")  # no cookies saved
    assert FakePush.sent[-1]["title"] == "Sync failed"


def test_expired_subscription_is_removed(ready):
    FakePush.fail_status = 410
    ready.state.notifier.send("x", "y")
    assert ready.state.notifier.subscriptions() == []


def test_other_push_failure_is_counted(ready):
    FakePush.fail_status = 500
    ready.state.notifier.send("x", "y")
    assert ready.state.notifier.subscriptions()[0]["failures"] == 1


def test_service_worker_served_at_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["service-worker-allowed"] == "/"
    assert "showNotification" in r.text


@needs_ffmpeg
def test_history_groups_and_details(ready, authed):
    ready.state.db.execute("UPDATE library_items SET sync_enabled = 1 WHERE id = 'MPREb_1'")
    ready.state.engine.run("manual")
    page = authed.get("/history").text
    assert "Today" in page
    assert "Downloaded" in page and "First Song" in page  # sync run details are listed
    syncs = authed.get("/history?kind=syncs").text
    assert "Sync finished" in syncs and "YouTube Music cookies updated" not in syncs
    account = authed.get("/history?kind=account").text
    assert "YouTube Music cookies updated" in account
