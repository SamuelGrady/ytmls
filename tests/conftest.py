import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.ytm import LibraryEntry

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PAPISID\tabc123\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsidvalue\n"
    ".google.com\tTRUE\t/\tTRUE\t1893456000\tNID\tignored\n"
)
PASSWORD = "correct horse battery"


class FakeYTM:
    album_data: dict = {}
    playlist_tracks: dict = {}
    albums = [LibraryEntry("MPREb_1", "album", "Album One", "Artist A", "2020", None, None)]
    playlists = [LibraryEntry("PL1", "playlist", "Road Trip", "Me", None, 12, None)]
    fail = False

    def __init__(self, cookie_header: str):
        assert "__Secure-3PAPISID=abc123" in cookie_header
        if FakeYTM.fail:
            raise RuntimeError("401")

    def library_albums(self):
        return list(FakeYTM.albums)

    def library_playlists(self):
        return list(FakeYTM.playlists)

    def album(self, browse_id):
        return FakeYTM.album_data[browse_id]

    def playlist(self, playlist_id):
        return {"tracks": list(FakeYTM.playlist_tracks.get(playlist_id, []))}


def _artist(name):
    return [{"name": name, "id": "UC" + name}]


def default_album_data():
    return {
        "MPREb_1": {
            "title": "Album One", "artists": _artist("Artist A"), "year": "2020",
            "thumbnails": [{"url": "https://lh3.googleusercontent.com/x=w60-h60-l90-rj", "width": 60}],
            "tracks": [
                {"videoId": "a1", "title": "First Song", "artists": _artist("Artist A"), "trackNumber": 1,
                 "isAvailable": True, "duration_seconds": 1},
                {"videoId": "a2", "title": "Second/Song", "artists": _artist("Artist A"), "trackNumber": 2,
                 "isAvailable": True, "duration_seconds": 1},
                {"videoId": None, "title": "Unavailable", "isAvailable": False},
            ],
        },
    }


def default_playlist_tracks():
    return {
        "PL1": [
            {"videoId": "a2", "title": "Second/Song", "artists": _artist("Artist A"),
             "album": {"name": "Album One", "id": "MPREb_1"}, "duration_seconds": 1},
            {"videoId": "v9", "title": "Music Video", "artists": _artist("Artist B"), "album": None,
             "duration_seconds": 1, "thumbnails": [{"url": "https://i.ytimg.com/vi/v9/hq.jpg", "width": 480}]},
        ],
    }


@pytest.fixture(autouse=True)
def reset_fake():
    FakeYTM.albums = [LibraryEntry("MPREb_1", "album", "Album One", "Artist A", "2020", None, None)]
    FakeYTM.playlists = [LibraryEntry("PL1", "playlist", "Road Trip", "Me", None, 12, None)]
    FakeYTM.fail = False
    FakeYTM.album_data = default_album_data()
    FakeYTM.playlist_tracks = default_playlist_tracks()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("YTMLS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YTMLS_MUSIC_DIR", str(tmp_path / "music"))
    return replace(load_settings(), cookie_secure=False)


class FakeDownloader:
    """Produces a short generated tone instead of contacting YouTube."""

    calls: list = []
    fail_ids: set = set()

    def fetch_audio(self, video_id, workdir, cookies_file, premium):
        import subprocess
        FakeDownloader.calls.append((video_id, premium, cookies_file.read_text().count("abc123")))
        if video_id in FakeDownloader.fail_ids:
            raise RuntimeError("ERROR: Video unavailable")
        out = workdir / f"{video_id}.opus"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "libopus", str(out)], check=True)
        return out, "251 opus 160kbps"


@pytest.fixture(autouse=True)
def reset_downloader():
    FakeDownloader.calls = []
    FakeDownloader.fail_ids = set()


@pytest.fixture
def app(settings, monkeypatch):
    # Covers are fetched from the internet in production; stub it out.
    monkeypatch.setattr("app.media.fetch_cover", lambda url, timeout=20: (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"))
    app = create_app(settings, ytm_factory=FakeYTM, downloader=FakeDownloader(), start_scheduler=False)
    app.state.engine.sleep = lambda s: None
    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://testserver")


def pre_token(client, path):
    r = client.get(path)
    return re.search(r'name="pre" value="([^"]+)"', r.text).group(1)


def csrf_from(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def do_setup(client, app):
    pre = pre_token(client, "/setup")
    return client.post("/setup", data={
        "pre": pre, "setup_token": app.state.setup_token, "username": "admin",
        "password": PASSWORD, "confirm": PASSWORD,
    }, follow_redirects=False)


@pytest.fixture
def authed(client, app):
    r = do_setup(client, app)
    assert r.status_code == 303
    return client
