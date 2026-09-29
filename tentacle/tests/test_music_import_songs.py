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


if __name__ == "__main__":
    unittest.main()
