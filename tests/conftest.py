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


@pytest.fixture(autouse=True)
def reset_fake():
    FakeYTM.albums = [LibraryEntry("MPREb_1", "album", "Album One", "Artist A", "2020", None, None)]
    FakeYTM.playlists = [LibraryEntry("PL1", "playlist", "Road Trip", "Me", None, 12, None)]
    FakeYTM.fail = False


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("YTMLS_DATA_DIR", str(tmp_path))
    return replace(load_settings(), cookie_secure=False)


@pytest.fixture
def app(settings):
    return create_app(settings, ytm_factory=FakeYTM)


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
