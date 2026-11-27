import shutil
from pathlib import Path

import pytest
from mutagen.oggopus import OggOpus

from app import media
from app.ytm import LibraryEntry

from .conftest import FakeYTM, _artist, csrf_from
from .test_sync import enable, music, ready  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def add_release(browse_id, title, kind, artist, tracks):
    FakeYTM.albums = FakeYTM.albums + [LibraryEntry(browse_id, "album", title, artist, "2024", None, None)]
    FakeYTM.album_data[browse_id] = {
        "title": title, "type": kind, "artists": _artist(artist), "year": "2024",
        "thumbnails": [{"url": "https://lh3.googleusercontent.com/s=w60-h60-l90-rj", "width": 60}],
        "tracks": [{"videoId": vid, "title": t, "artists": _artist(artist), "trackNumber": n,
                    "isAvailable": True, "duration_seconds": 1} for n, (vid, t) in enumerate(tracks, start=1)],
    }


def set_singles(app, client, on: bool, incomplete: bool = False):
    csrf = csrf_from(client.get("/settings").text)
    data = {"csrf_token": csrf, "interval": "6", "max_per_run": "200"}
    if on:
        data["singles"] = "1"
    if incomplete:
        data["incomplete"] = "1"
    r = client.post("/settings/sync", data=data, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


@pytest.fixture
def releases(ready):  # noqa: F811
    FakeYTM.album_data["MPREb_1"]["type"] = "Album"
    add_release("MPREb_S", "Hit Song", "Single", "Artist C", [("s1", "Hit Song")])
    add_release("MPREb_E", "Little EP", "EP", "Artist C", [("e1", "EP One"), ("e2", "EP Two")])
    ready.state.engine.run("manual")  # picks the new releases up into the library
    return ready


def test_singles_use_album_folders_by_default(releases):
    enable(releases, "MPREb_S")
    releases.state.engine.run("manual")
    f = music(releases) / "Artist C" / "Hit Song" / "01 - Hit Song.opus"
    assert f.exists()
    assert OggOpus(f)["album"] == ["Hit Song"]
    assert not (music(releases) / "Artist C" / "artist.nfo").exists()


def test_singles_go_loose_when_on(releases, authed):
    set_singles(releases, authed, True)
    enable(releases, "MPREb_S", "MPREb_E", "MPREb_1", "PL1")
    s = releases.state.engine.run("manual")
    assert not s.failed, s
    root = music(releases)
    single = root / "Artist C" / "Hit Song.opus"
    assert single.exists()
    tags = OggOpus(single)
    assert "album" not in tags and "tracknumber" not in tags and "discnumber" not in tags
    assert tags["albumartist"] == ["Artist C"] and tags["title"] == ["Hit Song"]
    assert tags.get("metadata_block_picture"), "cover art stays embedded"
    # EPs and albums keep their folders.
    assert (root / "Artist C" / "Little EP" / "01 - EP One.opus").exists()
    assert (root / "Artist A" / "Album One" / "01 - First Song.opus").exists()
    # A playlist song with no album at all is loose too, and the playlist points at it.
    assert (root / "Artist B" / "Music Video.opus").exists()
    m3u = (root / "Playlists" / "Road Trip.m3u8").read_text()
    assert "../Artist B/Music Video.opus" in m3u
    # Artists you only have singles from still read as artists in Jellyfin.
    nfo = (root / "Artist B" / "artist.nfo").read_text()
    assert "<name>Artist B</name>" in nfo and media.NFO_MARKER in nfo
    assert (root / "Artist C" / "artist.nfo").exists()


def test_turning_on_and_off_moves_existing_files(releases, authed):
    enable(releases, "MPREb_S", "PL1")
    engine = releases.state.engine
    engine.run("manual")
    root = music(releases)
    old = root / "Artist C" / "Hit Song" / "01 - Hit Song.opus"
    assert old.exists()

    assert "layout_moved" in set_singles(releases, authed, True)
    new = root / "Artist C" / "Hit Song.opus"
    assert new.exists() and not old.exists()
    assert not (root / "Artist C" / "Hit Song").exists(), "empty album folder is removed"
    assert "album" not in OggOpus(new)
    assert "../Artist B/Music Video.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()
    row = releases.state.db.one("SELECT file_path FROM tracks WHERE video_id = 's1'")
    assert row["file_path"] == "Artist C/Hit Song.opus"

    assert "layout_moved" in set_singles(releases, authed, False)
    assert old.exists() and not new.exists()
    assert OggOpus(old)["album"] == ["Hit Song"] and OggOpus(old)["tracknumber"] == ["1"]
    assert not (root / "Artist C" / "artist.nfo").exists(), "YTMLS removes its own artist.nfo"
    assert "../Artist B/Music Video/Music Video.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()
    # A second sync leaves everything where it is.
    assert engine.run("manual").relocated == 0


def test_album_release_wins_over_single(releases, authed):
    # The same song is both a single and track 1 of an album you sync.
    FakeYTM.album_data["MPREb_S"]["tracks"][0]["videoId"] = "a1"
    set_singles(releases, authed, True)
    enable(releases, "MPREb_S", "MPREb_1")
    releases.state.engine.run("manual")
    row = releases.state.db.one("SELECT release_type FROM tracks WHERE video_id = 'a1'")
    assert row["release_type"] == "album"
    assert not (music(releases) / "Artist A" / "First Song.opus").exists()


def test_old_cache_entries_learn_their_release_type(releases, authed):
    # Cached before release types were kept: no "type" key, so it's read again.
    import json, time
    releases.state.db.execute("INSERT INTO album_cache(browse_id, data, fetched_at) VALUES(?, ?, ?)",
                              ("MPREb_S", json.dumps({"title": "Hit Song", "artists": ["Artist C"], "tracks": []}),
                               time.time()))
    FakeYTM.playlist_tracks["PL1"].append({"videoId": "s1", "title": "Hit Song", "artists": _artist("Artist C"),
                                           "album": {"name": "Hit Song", "id": "MPREb_S"}, "duration_seconds": 1})
    set_singles(releases, authed, True)
    enable(releases, "PL1")
    releases.state.engine.run("manual")
    assert (music(releases) / "Artist C" / "Hit Song.opus").exists()


def test_editing_a_loose_single_keeps_it_loose(releases, authed):
    set_singles(releases, authed, True)
    enable(releases, "MPREb_S")
    releases.state.engine.run("manual")
    releases.state.editor.edit_track("s1", {"title": "Hit Song (Radio Edit)", "artists": "Artist C",
                                           "album": "Hit Song", "album_artist": "Artist C",
                                           "track_number": "1", "year": "2024"})
    f = music(releases) / "Artist C" / "Hit Song (Radio Edit).opus"
    assert f.exists()
    assert "album" not in OggOpus(f)


def test_artist_nfo_escapes_names(tmp_path):
    media.ensure_artist_nfo(tmp_path, Path("A & B"), "A & B <live>")
    text = (tmp_path / "A & B" / "artist.nfo").read_text()
    assert "<name>A &amp; B &lt;live&gt;</name>" in text


def test_incomplete_albums_go_loose_until_the_album_is_synced(releases, authed):
    set_singles(releases, authed, False, incomplete=True)
    enable(releases, "PL1")
    engine = releases.state.engine
    engine.run("manual")
    root = music(releases)
    # a2 is track 2 of Album One, but only the playlist is synced: no one-song album folder.
    loose = root / "Artist A" / "Second_Song.opus"
    assert loose.exists() and not (root / "Artist A" / "Album One").exists()
    assert "album" not in OggOpus(loose)
    assert "../Artist A/Second_Song.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()
    assert (root / "Artist B" / "Music Video.opus").exists()
    assert (root / "Artist A" / "artist.nfo").exists()

    # Sync the whole album: its songs come together in a full album folder.
    enable(releases, "MPREb_1")
    s = engine.run("manual")
    assert s.relocated == 1
    assert not loose.exists()
    assert (root / "Artist A" / "Album One" / "01 - First Song.opus").exists()
    moved = root / "Artist A" / "Album One" / "02 - Second_Song.opus"
    assert moved.exists() and OggOpus(moved)["album"] == ["Album One"]
    assert not (root / "Artist A" / "artist.nfo").exists()
    assert "../Artist A/Album One/02 - Second_Song.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()


def test_turning_incomplete_on_moves_playlist_only_songs(releases, authed):
    enable(releases, "PL1", "MPREb_S")
    releases.state.engine.run("manual")
    root = music(releases)
    assert (root / "Artist A" / "Album One" / "02 - Second_Song.opus").exists()
    assert "layout_moved" in set_singles(releases, authed, False, incomplete=True)
    assert (root / "Artist A" / "Second_Song.opus").exists()
    # The single is synced as its own album item, so this setting leaves it in its folder.
    assert (root / "Artist C" / "Hit Song" / "01 - Hit Song.opus").exists()
    assert "layout_moved" in set_singles(releases, authed, False, incomplete=False)
    assert (root / "Artist A" / "Album One" / "02 - Second_Song.opus").exists()


def test_files_waiting_for_removal_are_not_moved(releases, authed):
    enable(releases, "PL1")
    engine = releases.state.engine
    engine.run("manual")
    FakeYTM.playlist_tracks["PL1"] = FakeYTM.playlist_tracks["PL1"][1:]  # a2 dropped from the playlist
    engine.run("manual")
    set_singles(releases, authed, False, incomplete=True)
    root = music(releases)
    assert (root / "Artist A" / "Album One" / "02 - Second_Song.opus").exists()
    assert (root / "Artist B" / "Music Video.opus").exists()
