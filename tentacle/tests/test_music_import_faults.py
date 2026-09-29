"""Spotify imports survive MusicBrainz failures and changes made while a turn runs
(#248, #249, #250).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_discover import A1, R1, _DiscoverBase  # noqa: E402


def found(title, artist, album="", album_artist=""):
    return {"album": {"mbid": R1, "title": "Rumours", "year": "1977", "artist": artist, "artist_mbid": A1}}


SONGS = [{"title": f"Song {i}", "artist": "Band"} for i in range(3)]


class TestImportFaults(_DiscoverBase):
    def setUp(self):
        super().setUp()
        from services.music import spotify
        self.spotify = spotify
        self.later = []
        p = mock.patch.object(spotify, "_later", side_effect=lambda delay, fn: self.later.append(fn), create=True)
        p.start()
        self.addCleanup(p.stop)

    def reload(self, imp):
        from models.database import MusicImport
        import_id = imp.id if isinstance(imp, MusicImport) else imp
        self.db.expire_all()
        return self.db.get(MusicImport, import_id)

    def test_a_503_in_the_batch_step_is_reported_and_not_rerun_on_every_poll(self):
        # #250: the batch step failed outside the error handling: "resolving" with no
        # reason, and every Discover poll ran the failing batch again.
        from services.music import discover
        from services.musicbrainz import MusicBrainzError
        batch = mock.Mock(side_effect=MusicBrainzError("MusicBrainz is rate-limiting (HTTP 503)", 503))
        imp = self.spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "",
                                        [dict(s, album="Rumours", album_artist="Band") for s in SONGS])
        with mock.patch.object(discover.Resolver, "prepare_songs", batch):
            self.run_jobs()
            for _ in range(3):   # the page polls
                self.spotify.summaries(self.db, self.user)
                self.run_jobs()
        self.assertEqual(batch.call_count, 1)
        imp = self.reload(imp)
        self.assertEqual(imp.status, "resolving")
        self.assertIn("rate-limiting", imp.error)
        self.later.pop()()   # the back-off is over, MusicBrainz is back
        with mock.patch.object(discover.Resolver, "song", side_effect=found):
            self.run_jobs()
        imp = self.reload(imp)
        self.assertEqual((imp.status, imp.done, imp.error), ("ready", 3, None))

    def test_a_refresh_while_a_turn_runs_is_not_lost(self):
        from services.music import discover
        imp = self.spotify.start_import(self.db, self.user.id, "P", "spotify_url", "u", SONGS)
        calls = []

        def song_and_refresh(title, artist, album="", album_artist=""):
            if not calls:   # someone presses Refresh meanwhile: one more song
                other = self.Session()
                with mock.patch.object(self.spotify, "fetch_playlist",
                                       return_value=("P v2", SONGS + [{"title": "Brand New", "artist": "Band"}])):
                    self.spotify.refresh_import(other, other.get(type(imp), imp.id))
                other.close()
            calls.append(title)
            return found(title, artist)
        with mock.patch.object(discover.Resolver, "song", side_effect=song_and_refresh):
            self.run_jobs()
        imp = self.reload(imp)
        self.assertEqual((imp.name, imp.total, imp.status, imp.done), ("P v2", 4, "ready", 4))
        self.assertIn("Brand New", [t["title"] for t in imp.tracks])

    def test_a_remove_while_a_turn_runs_ends_the_job_quietly(self):
        from services.music import discover
        imp = self.spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", SONGS)
        import_id = imp.id
        calls = []

        def song_and_remove(title, artist, album="", album_artist=""):
            if not calls:
                other = self.Session()
                other.delete(other.get(type(imp), import_id))
                other.commit()
                other.close()
            calls.append(title)
            return found(title, artist)
        with mock.patch.object(discover.Resolver, "song", side_effect=song_and_remove):
            self.run_jobs()   # no StaleDataError
        self.assertIsNone(self.reload(import_id))
        self.assertNotIn(import_id, self.spotify._active)

    def test_an_unexpected_error_stops_the_import_instead_of_looping(self):
        from services.music import discover
        imp = self.spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", SONGS)
        with mock.patch.object(discover.Resolver, "song", side_effect=KeyError("title")):
            with self.assertRaises(KeyError):
                self.run_jobs()
        for _ in range(3):
            self.spotify.summaries(self.db, self.user)
        self.assertEqual(self.jobs, [])
        imp = self.reload(imp)
        self.assertEqual(imp.status, "error")
        self.assertIn("KeyError", imp.error)


class TestImportRetryEndpoint(_DiscoverBase):
    def test_retry_carries_on_an_import_stopped_at_an_error(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import MusicImport, get_db
        from routers.auth import get_user_from_request
        from services.music import discover, spotify
        app = FastAPI()
        app.include_router(music.webhook_router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[get_user_from_request] = lambda: self.user
        client = TestClient(app)
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", SONGS)
        self.jobs.clear()
        spotify._active.clear()
        imp.status, imp.error = "error", "MusicBrainz: Set a contact email"
        self.db.commit()
        r = client.post(f"/api/music/imports/{imp.id}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        with mock.patch.object(discover.Resolver, "song", side_effect=found):
            self.run_jobs()
        self.db.expire_all()
        self.assertEqual(self.db.get(MusicImport, imp.id).status, "ready")
        self.assertEqual(client.post(f"/api/music/imports/{imp.id}/retry").status_code, 409)


if __name__ == "__main__":
    unittest.main()
