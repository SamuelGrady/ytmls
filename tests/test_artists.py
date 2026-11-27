"""Songs with several artists: one artist folder, and one tag value per artist."""

import shutil
import subprocess

import pytest
from mutagen.id3 import ID3, TPE1, TPE2
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from app import media
from app.media import TrackMeta
from app.ytm import LibraryEntry

from .conftest import FakeDownloader, FakeYTM, csrf_from
from .test_sync import enable, music, ready  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def people(*names):
    return [{"name": n, "id": "UC" + n} for n in names]


@pytest.fixture
def collab(ready):  # noqa: F811
    """A joint album, plus a playlist song by two artists with no album."""
    FakeYTM.albums = FakeYTM.albums + [LibraryEntry("MPREb_C", "album", "Together", "Artist A, Artist B", "2024",
                                                    None, None)]
    FakeYTM.album_data["MPREb_C"] = {
        "title": "Together", "type": "Album", "artists": people("Artist A", "Artist B"), "year": "2024",
        "thumbnails": [{"url": "https://lh3.googleusercontent.com/c=w60-h60-l90-rj", "width": 60}],
        "tracks": [
            {"videoId": "c1", "title": "Duet", "artists": people("Artist A", "Artist B"), "trackNumber": 1,
             "isAvailable": True, "duration_seconds": 1},
            {"videoId": "c2", "title": "Feature", "artists": people("Artist A", "Tyler, The Creator"),
             "trackNumber": 2, "isAvailable": True, "duration_seconds": 1},
        ],
    }
    FakeYTM.playlist_tracks["PL1"].append(
        {"videoId": "p1", "title": "Team Up", "artists": people("Artist D", "Artist E"), "album": None,
         "duration_seconds": 1})
    ready.state.engine.run("manual")
    return ready


def test_joint_album_goes_in_the_first_artists_folder(collab):
    enable(collab, "MPREb_C", "PL1")
    s = collab.state.engine.run("manual")
    assert not s.failed, s
    root = music(collab)
    duet = root / "Artist A" / "Together" / "01 - Duet.opus"
    assert duet.exists()
    assert not (root / "Artist A, Artist B").exists()
    tags = OggOpus(duet)
    assert tags["artist"] == ["Artist A", "Artist B"]
    assert tags["albumartist"] == ["Artist A", "Artist B"]
    assert tags["artists"] == ["Artist A", "Artist B"]
    # A comma inside one artist's name doesn't split them.
    feature = OggOpus(root / "Artist A" / "Together" / "02 - Feature.opus")
    assert feature["artist"] == ["Artist A", "Tyler, The Creator"]
    # A song with no album goes under its first artist, but credits both.
    team = root / "Artist D" / "Team Up" / "Team Up.opus"
    assert team.exists() and not (root / "Artist D, Artist E").exists()
    assert OggOpus(team)["artist"] == ["Artist D", "Artist E"]
    assert "../Artist D/Team Up/Team Up.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()


def test_files_from_before_move_and_get_retagged_without_downloading(collab):
    enable(collab, "MPREb_C", "PL1")
    engine, db = collab.state.engine, collab.state.db
    engine.run("manual")
    root = music(collab)
    # Put things back the way older versions left them: a joined "A, B" folder,
    # the artists as one string, and no separate names in the database.
    old_dir = root / "Artist A, Artist B" / "Together"
    old_dir.mkdir(parents=True)
    for vid, name in (("c1", "01 - Duet.opus"), ("c2", "02 - Feature.opus")):
        (root / "Artist A" / "Together" / name).rename(old_dir / name)
        audio = OggOpus(old_dir / name)
        audio["ARTIST"] = ", ".join(audio["ARTIST"])
        audio["ALBUMARTIST"] = "Artist A, Artist B"
        del audio["ARTISTS"]
        audio.save()
        db.execute("UPDATE tracks SET file_path = ?, artist_names = NULL, album_artist_names = NULL, tags_rev = 0 "
                   "WHERE video_id = ?", (f"Artist A, Artist B/Together/{name}", vid))
    old_team = root / "Artist D" / "Team Up" / "Team Up.opus"
    db.execute("UPDATE tracks SET artist_names = NULL, album_artist_names = NULL, tags_rev = 0 WHERE video_id = 'p1'")
    a = OggOpus(old_team)
    a["ARTIST"] = "Artist D, Artist E"
    a.save()
    (root / "Playlists" / "Road Trip.m3u8").write_text("stale")
    downloads = len(FakeDownloader.calls)

    s = engine.run("manual")
    assert not s.failed, s
    assert len(FakeDownloader.calls) == downloads, "nothing is downloaded again"
    assert s.relocated == 2 and s.retagged == 1
    new = root / "Artist A" / "Together" / "01 - Duet.opus"
    assert new.exists() and not (root / "Artist A, Artist B").exists()
    assert OggOpus(new)["artist"] == ["Artist A", "Artist B"]
    assert OggOpus(new)["albumartist"] == ["Artist A", "Artist B"]
    assert OggOpus(new).get("metadata_block_picture"), "cover art kept"
    assert OggOpus(root / "Artist A" / "Together" / "02 - Feature.opus")["artist"] == ["Artist A",
                                                                                     "Tyler, The Creator"]
    assert OggOpus(old_team)["artist"] == ["Artist D", "Artist E"]
    row = db.one("SELECT file_path, tags_rev FROM tracks WHERE video_id = 'c1'")
    assert row["file_path"] == "Artist A/Together/01 - Duet.opus" and row["tags_rev"] == 1
    # The playlist is written again from the database.
    assert "../Artist D/Team Up/Team Up.opus" in (root / "Playlists" / "Road Trip.m3u8").read_text()
    # Settled: the next run touches nothing.
    again = engine.run("manual")
    assert again.relocated == 0 and again.retagged == 0


def test_your_edits_are_not_split_by_the_migration(collab):
    enable(collab, "MPREb_C")
    engine, db = collab.state.engine, collab.state.db
    engine.run("manual")
    collab.state.editor.edit_track("c1", {"title": "Duet", "artists": "My Own Credit", "album": "Together",
                                          "album_artist": "Artist A; Artist B", "track_number": "1",
                                          "year": "2024"})
    db.execute("UPDATE tracks SET artist_names = NULL, tags_rev = 0 WHERE video_id = 'c1'")
    engine.run("manual")
    row = db.one("SELECT artists, artist_names FROM tracks WHERE video_id = 'c1'")
    assert row["artists"] == "My Own Credit" and row["artist_names"] == '["My Own Credit"]'


def test_editor_takes_artists_split_by_semicolons(collab, authed):
    enable(collab, "MPREb_C")
    collab.state.engine.run("manual")
    page = authed.get("/tracks/c1").text
    assert 'value="Artist A; Artist B"' in page
    csrf = csrf_from(page)
    authed.post("/tracks/c1", data={"csrf_token": csrf, "title": "Duet", "artists": "Artist B; Artist A; Artist C",
                                    "album": "Together", "album_artist": "Artist B; Artist A", "track_number": "1",
                                    "year": "2024"})
    f = music(collab) / "Artist B" / "Together" / "01 - Duet.opus"
    assert f.exists()
    assert OggOpus(f)["artist"] == ["Artist B", "Artist A", "Artist C"]
    assert OggOpus(f)["albumartist"] == ["Artist B", "Artist A"]
    row = collab.state.db.one("SELECT artists FROM tracks WHERE video_id = 'c1'")
    assert row["artists"] == "Artist B, Artist A, Artist C"


def _tone(path, codec):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    *codec, str(path)], check=True)


META = TrackMeta("vid", "Song", "A, B", "Album", "A, C", 3, "2024", None,
                 artist_names=["A", "B"], album_artist_names=["A", "C"])


def test_mp3_holds_each_artist_separately(tmp_path):
    f = tmp_path / "x.mp3"
    _tone(f, ["-c:a", "libmp3lame"])
    media.write_tags(f, META, None)
    tags = ID3(f)
    assert tags.version[:2] == (2, 3)
    assert tags.getall("TPE1")[0].text == ["A", "B"]
    assert tags.getall("TPE2")[0].text == ["A", "C"]
    assert tags.getall("TXXX:ARTISTS")[0].text == ["A", "B"]
    # Null-separated on disk (what Jellyfin splits on), not "A/B".
    assert "A/B".encode("utf-16-le") not in f.read_bytes()
    assert isinstance(tags.getall("TPE1")[0], TPE1) and isinstance(tags.getall("TPE2")[0], TPE2)


def test_m4a_holds_each_artist_separately(tmp_path):
    f = tmp_path / "x.m4a"
    _tone(f, ["-c:a", "aac"])
    media.write_tags(f, META, None)
    tags = MP4(f).tags
    assert tags["\xa9ART"] == ["A", "B"]
    assert tags["aART"] == ["A", "C"]
    assert [bytes(v) for v in tags[media.MP4_ARTISTS]] == [b"A", b"B"]


def test_folder_uses_first_album_artist():
    assert media.track_relpath(META).as_posix() == "A/Album/03 - Song.opus"
    old_style = TrackMeta("vid", "Song", "A, B", "Album", "A, C", 3, "2024", None)
    assert media.track_relpath(old_style).parts[0] == "A, C", "unknown names keep the folder they had"
    assert media.split_names(" A ;B; ;a ") == ["A", "B"]
