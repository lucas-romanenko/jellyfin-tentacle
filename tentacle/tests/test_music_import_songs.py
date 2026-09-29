"""Which songs of a playlist an import keeps (#289, #287).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_discover import A1, R1, R2, _DiscoverBase  # noqa: E402


class TestSameTitleOnTwoAlbums(_DiscoverBase):
    """#289: two songs with one title by one artist ("Intro") on two albums are two songs."""

    def test_both_albums_are_proposed(self):
        from services.music import discover, spotify
        songs = [{"title": "Intro", "artist": "The xx", "album": "xx", "album_artist": "The xx"},
                 {"title": "Intro", "artist": "The xx", "album": "Coexist", "album_artist": "The xx"}]
        self.assertEqual({s["album"] for s in spotify._dedupe(songs)}, {"xx", "Coexist"})

        def resolve(title, artist, album="", album_artist=""):
            mbid = R1 if album == "xx" else R2
            return {"album": {"mbid": mbid, "title": album, "year": "2009", "artist": artist, "artist_mbid": A1}}
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", songs)
        with mock.patch.object(discover.Resolver, "song", side_effect=resolve):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual(sorted(a["title"] for a in spotify.albums_of(imp)[0]), ["Coexist", "xx"])

    def test_one_song_from_its_single_and_its_album_counts_once(self):
        from services.music import discover, spotify
        songs = [{"title": "Crystalised", "artist": "The xx", "album": "Crystalised", "album_artist": "The xx"},
                 {"title": "Crystalised", "artist": "The xx", "album": "xx", "album_artist": "The xx"},
                 {"title": "crystalised", "artist": "the xx", "album": "xx", "album_artist": "The xx"}]
        self.assertEqual(len(spotify._dedupe(songs)), 2)   # the same row twice still merges

        def resolve(title, artist, album="", album_artist=""):   # the single isn't a studio album
            return {"album": {"mbid": R1, "title": "xx", "year": "2009", "artist": artist, "artist_mbid": A1}}
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", songs)
        with mock.patch.object(discover.Resolver, "song", side_effect=resolve):
            self.run_jobs()
        self.db.refresh(imp)
        [album] = spotify.albums_of(imp)[0]
        self.assertEqual(album["songs"], ["Crystalised"])

    def test_a_link_import_is_unchanged(self):
        from services.music import spotify
        songs = [{"title": "Dreams", "artist": "Fleetwood Mac"}, {"title": "dreams", "artist": "fleetwood mac"}]
        self.assertEqual(len(spotify._dedupe(songs)), 1)


class TestLargeExport(_DiscoverBase):
    """#287: songs past the limit were dropped without a word."""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(music.webhook_router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[get_user_from_request] = lambda: self.user
        self.client = TestClient(app)

    def upload(self, rows):
        csv = "Track Name,Artist Name(s),Album Name\n" + "".join(f"Song {i},A,Album {i}\n" for i in range(rows))
        return self.client.post("/api/music/imports", files={"file": ("big.csv", csv.encode(), "text/csv")})

    def test_songs_past_the_limit_are_counted_and_shown(self):
        from services.music import spotify
        r = self.upload(2500)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["total"], body["left_out"]), (spotify.MAX_TRACKS, 500))
        detail = self.client.get(f"/api/music/imports/{body['id']}").json()
        self.assertEqual((detail["total"], detail["left_out"]), (2000, 500))
        [summary] = self.client.get("/api/music/imports").json()["imports"]
        self.assertEqual(summary["left_out"], 500)

    def test_a_file_under_the_limit_leaves_nothing_out(self):
        body = self.upload(1999).json()
        self.assertEqual((body["total"], body["left_out"]), (1999, 0))


if __name__ == "__main__":
    unittest.main()
