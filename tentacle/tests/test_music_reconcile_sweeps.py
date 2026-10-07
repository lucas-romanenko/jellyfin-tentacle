"""The daily check removes only what Lidarr really no longer has.

At its end the reconcile drops artists missing from Lidarr's artist list, and
sync_artist drops albums missing from an artist's album list. Three ways that
removed rows Lidarr still had, with who requested them, their review state and
(since #242) a request still owed its pin and search:
- one empty artist list (a proxy's empty 200, Lidarr starting up) removed every
  artist and album;
- an artist that a request added while the check ran (the check reads the list
  once, at its start; big libraries take hours) was removed at its end;
- a new artist's album list can come back empty while Lidarr is still refreshing
  it, which removed the album just requested.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import random
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import ARTIST, RG, FakeLidarr, _Base, lidarr_album, lidarr_release, mb_release  # noqa: E402

OTHER = {"id": 5, "foreignArtistId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "artistName": "Alex Chilton",
         "path": "/data/music/Alex Chilton"}
BIG_STAR = {"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star", "path": "/data/music/Big Star"}


class _Sweeps(_Base):
    def setUp(self):
        super().setUp()
        from services.music import jobs
        self.jobs_mod = jobs
        p = mock.patch.object(jobs, "picture_pass", return_value={})
        p.start()
        self.addCleanup(p.stop)
        real_get = FakeLidarr.do_GET

        def do_get(handler):   # an album Lidarr no longer has: 404, as the real API answers
            path = handler.path.split("?")[0]
            if path.startswith("/api/v1/album/") and path.rsplit("/", 1)[1].isdigit() \
                    and int(path.rsplit("/", 1)[1]) not in FakeLidarr.state.get("albums", {}):
                FakeLidarr.log.append(("GET", path, {}))
                return handler._send(404, {"message": "NotFound"})
            return real_get(handler)
        p = mock.patch.object(FakeLidarr, "do_GET", do_get)
        p.start()
        self.addCleanup(p.stop)
        FakeLidarr.state["lookup"] = [{"foreignAlbumId": RG, "title": "Radio City",
                                       "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}]
        album = lidarr_album(10, RG, [lidarr_release("r13", 13, monitored=True), lidarr_release("r12", 12)],
                             title="Radio City")
        album["artist"] = {"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}
        FakeLidarr.state["after_add"] = album
        FakeLidarr.state["artists"] = [BIG_STAR]
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]

    def reconcile(self):
        self.jobs_mod.progress["running"] = False
        self.jobs_mod.reconcile("daily")(self.db)
        self.run_jobs()

    def rows(self):
        from models.database import MusicAlbum, MusicArtist
        self.db.expire_all()
        return (self.db.query(MusicAlbum).filter_by(mbid=RG).first(),
                self.db.query(MusicArtist).filter_by(mbid=ARTIST).first())

    def searches(self):
        return [c for c in self.calls("POST", "/api/v1/command") if (c[2] or {}).get("name") == "AlbumSearch"]

    def requested_in_the_snapshot(self):
        from services.media_requests import request_album
        request_album(self.db, RG, user_id=1, via="test")
        self.run_jobs()
        self.reconcile()
        album, artist = self.rows()
        self.assertIsNotNone(album)
        self.assertIsNotNone(artist)


class TestEmptyArtistList(_Sweeps):
    def test_one_empty_artist_list_removes_nothing(self):
        from services.music import worker
        self.requested_in_the_snapshot()
        FakeLidarr.state["artists"] = []
        self.reconcile()
        album, artist = self.rows()
        self.assertIsNotNone(album)
        self.assertIsNotNone(artist)
        self.assertEqual(album.requested_by, 1)
        self.assertIn("Lidarr listed no artists", worker.state["last_error"]["message"])

    def test_an_owed_request_survives_an_empty_list_on_a_day_musicbrainz_was_down(self):
        from services.media_requests import request_album
        from services.musicbrainz import MusicBrainzError
        from models.database import MusicArtist
        self.db.add(MusicArtist(mbid=ARTIST, lidarr_artist_id=1, name="Big Star"))
        self.db.commit()
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()                                   # Tentacle restarts before the pin
        self.mb[RG] = MusicBrainzError("MusicBrainz is rate-limiting (HTTP 503)", 503)
        FakeLidarr.state["artists"] = []                    # and Lidarr answers one empty list
        self.reconcile()
        self.assertTrue(self.rows()[0].request_pending)
        FakeLidarr.state["artists"] = [BIG_STAR]            # the next day all is well
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
        self.reconcile()
        album, _ = self.rows()
        self.assertFalse(album.request_pending)
        self.assertEqual(album.requested_by, 1)
        self.assertTrue(self.searches())

    def test_a_second_empty_list_in_a_row_removes_the_artists(self):
        self.requested_in_the_snapshot()
        FakeLidarr.state["artists"] = []
        self.reconcile()
        self.assertIsNotNone(self.rows()[1])
        self.reconcile()                                    # still empty the next day
        self.assertEqual(self.rows(), (None, None))

    def test_an_empty_list_with_nothing_in_the_snapshot_is_quiet(self):
        from services.music import worker
        worker.state["last_error"] = None
        FakeLidarr.state["artists"] = []
        self.reconcile()
        self.assertIsNone(worker.state["last_error"])


class TestRequestDuringTheCheck(_Sweeps):
    def test_an_artist_a_request_adds_while_the_check_runs_is_kept(self):
        from services.media_requests import request_album
        FakeLidarr.state["artists"] = [OTHER]               # the list the check reads at its start
        real_urgent = self.jobs_mod.worker.run_urgent_jobs
        calls = []

        def urgent():
            calls.append(1)
            if len(calls) == 2:                              # while the check is under way
                FakeLidarr.state["artists"] = [OTHER, BIG_STAR]
                request_album(self.db, RG, user_id=1, via="test")
                self.run_jobs()                              # its pin-and-search job runs in between
            return real_urgent()
        with mock.patch.object(self.jobs_mod.worker, "run_urgent_jobs", side_effect=urgent):
            self.reconcile()
        album, artist = self.rows()
        self.assertIsNotNone(album, "the album requested during the check was removed at its end")
        self.assertIsNotNone(artist)
        self.assertEqual(album.requested_by, 1)
        self.assertTrue(self.searches())


class TestGenuineRemovals(_Sweeps):
    def test_an_artist_removed_from_lidarr_is_still_removed(self):
        self.requested_in_the_snapshot()
        FakeLidarr.state["artists"] = [OTHER]
        FakeLidarr.state["albums"] = {}
        self.reconcile()
        self.assertEqual(self.rows(), (None, None))

    def test_an_artist_with_an_owed_album_is_kept_until_that_request_is_settled(self):
        from models.database import MusicArtist
        from services.media_requests import request_album
        self.db.add(MusicArtist(mbid=ARTIST, lidarr_artist_id=1, name="Big Star"))
        self.db.commit()
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()                                   # restart: owed
        FakeLidarr.state["artists"] = [OTHER]               # the artist was removed in Lidarr meanwhile
        FakeLidarr.state["albums"] = {}
        self.reconcile()                                    # the resume finds no album (404): settled
        self.assertEqual(self.rows(), (None, None))

    def test_an_album_removed_from_an_artist_is_still_removed(self):
        self.requested_in_the_snapshot()
        FakeLidarr.state["albums"] = {}                     # the album was deleted in Lidarr
        self.reconcile()
        album, artist = self.rows()
        self.assertIsNone(album)
        self.assertIsNotNone(artist)


class TestEmptyAlbumListOfANewArtist(_Sweeps):
    def test_the_owed_album_isnt_removed_while_lidarr_still_refreshes_the_artist(self):
        from services.media_requests import request_album
        from services.music import library
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        saved = FakeLidarr.state["albums"]
        FakeLidarr.state["albums"] = {}                     # the artist's album list isn't filled in yet
        library.sync_artist(self.db, library.lidarr_client(self.db), BIG_STAR)
        self.assertTrue(self.rows()[0].request_pending)
        FakeLidarr.state["albums"] = saved
        self.jobs_mod.finish_pending_requests(self.db)
        self.assertTrue(self.searches())


class TestSweepsProperty(_Sweeps):
    """Random days: requests, restarts, MusicBrainz outages, empty or partial Lidarr lists,
    artists and albums removed in Lidarr, requests during the check. Invariants after every
    day: an owed request is never lost while Lidarr has the album (M1b); nothing Lidarr has
    is removed because of an empty list or a list read before the row existed (M11); what
    Lidarr really removed leaves the snapshot within two healthy checks."""

    def test_random_days(self):
        from models.database import MusicAlbum, MusicArtist
        from services.media_requests import RequestRefused, request_album
        from services.musicbrainz import MusicBrainzError
        seeds = int(os.environ.get("GM_SEEDS", "1000"))
        base = int(os.environ.get("GM_SEED", "20260929"))
        bad = []
        for n in range(seeds):
            seed = base + n
            rnd = random.Random(seed)
            self.db.query(MusicAlbum).delete()
            self.db.query(MusicArtist).delete()
            self.db.commit()
            self.jobs.clear()
            FakeLidarr.log.clear()
            FakeLidarr.state["albums"] = {}
            FakeLidarr.state["artists"] = [BIG_STAR]
            self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
            in_lidarr = {"artist": True, "album": False}
            requested, glitched = False, False
            try:
                for day in range(rnd.randint(2, 6)):
                    mb_down = rnd.random() < 0.3
                    self.mb[RG] = MusicBrainzError("503", 503) if mb_down else [mb_release("m1", "1974-01-01", 12)]
                    if not requested and rnd.random() < 0.6:
                        try:
                            request_album(self.db, RG, user_id=1, via="test")
                            requested, in_lidarr["album"] = True, True
                        except (RequestRefused, MusicBrainzError):
                            pass
                        if rnd.random() < 0.5:
                            self.jobs.clear()                       # restart before the pin
                        else:
                            try:
                                self.run_jobs()
                            except MusicBrainzError:
                                self.jobs.clear()
                    if requested and rnd.random() < 0.1:            # removed in Lidarr
                        FakeLidarr.state["albums"] = {}
                        in_lidarr["album"] = False
                    listing = [BIG_STAR] if in_lidarr["artist"] else []
                    if not glitched and rnd.random() < 0.2:
                        listing = []                                # one empty answer (a glitch)
                    glitched = not listing
                    FakeLidarr.state["artists"] = listing
                    try:
                        self.reconcile()
                    except MusicBrainzError:
                        self.jobs.clear()
                    FakeLidarr.state["artists"] = [BIG_STAR]
                    album, _ = self.rows()
                    if in_lidarr["album"] and album is None:
                        raise AssertionError(f"day {day}: a row Lidarr still has was removed")
                # two healthy days
                self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
                for _ in range(2):
                    self.reconcile()
                album, _ = self.rows()
                if in_lidarr["album"]:
                    if album is None or album.request_pending:
                        raise AssertionError("an owed request was never finished")
                    if not self.searches():
                        raise AssertionError("never searched")
                elif album is not None:
                    raise AssertionError("an album removed in Lidarr stayed")
            except AssertionError as e:
                bad.append((seed, str(e)))
        print(f"\n[sweeps] {seeds} seeds from {base}: failures={len(bad)} {bad[:3]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
