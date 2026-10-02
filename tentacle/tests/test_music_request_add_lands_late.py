"""An album add whose answer fails, and that lands in Lidarr only after Tentacle
looked for it, is still finished (follow-up to #242).

Lidarr adds a new artist's album synchronously: it reads the artist's metadata
first, so the add can take longer than the 15 s timeout and commit after
request_album's look (album_by_mbid) found nothing. The request was refused with
a 502, nothing was owed, and the album sat in Lidarr monitored and never
searched; the daily check even filed it "right" when Lidarr's default release
was the original.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import (ARTIST, RG, FakeLidarr, _Base, lidarr_album,  # noqa: E402
                             lidarr_release, mb_release)

SEARCH = {"name": "AlbumSearch", "albumIds": [10]}


def _wait():
    from services.music import jobs
    return jobs.UNCONFIRMED_ADD_WAIT


class TestAddLandsLate(_Base):
    def setUp(self):
        super().setUp()
        FakeLidarr.state["lookup"] = [{"foreignAlbumId": RG, "title": "Radio City",
                                       "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}]
        # Lidarr's default release is the original already.
        FakeLidarr.state["after_add"] = lidarr_album(10, RG, [lidarr_release("r12", 12, monitored=True),
                                                              lidarr_release("r13", 13)], title="Radio City")
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star",
                                        "path": "/data/music/Big Star"}]
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]

    def request(self, land=True):
        """The add commits 0.8 s after it was sent; the client gives up after 0.3 s."""
        from services.media_requests import RequestRefused, request_album
        orig = FakeLidarr.do_POST

        def slow_add(handler):
            if handler.path.startswith("/api/v1/album"):
                time.sleep(0.8)
                if not land:
                    FakeLidarr.log.append(("POST", "/api/v1/album", None))
                    return None   # the connection drops, nothing was added
            return orig(handler)
        with mock.patch.object(FakeLidarr, "do_POST", slow_add), mock.patch("services.lidarr.TIMEOUT", 0.3):
            with self.assertRaises(RequestRefused) as cm:
                request_album(self.db, RG, user_id=1, via="test")
            time.sleep(1.0)
        return cm.exception

    def row(self):
        from models.database import MusicAlbum
        self.db.expire_all()
        return self.db.query(MusicAlbum).filter_by(mbid=RG).first()

    def searches(self):
        return [c[2] for c in self.calls("POST", "/api/v1/command")]

    def test_the_request_is_owed_and_says_so(self):
        e = self.request()
        self.assertEqual(e.status, 502)
        self.assertIn("finishes the request", e.message)
        row = self.row()
        self.assertTrue(row.request_pending)
        self.assertEqual((row.lidarr_album_id, row.monitored, row.requested_by), (None, False, 1))

    def test_the_look_a_few_minutes_later_pins_and_searches(self):
        from services.music import jobs
        self.request()
        jobs.finish_pending_requests(self.db)
        self.assertEqual(self.searches(), [SEARCH])
        row = self.row()
        self.assertEqual((row.lidarr_album_id, row.request_pending, row.category), (10, False, "right"))
        self.assertEqual(len(self.calls("POST", "/api/v1/album")), 1)   # never added twice

    def test_the_daily_check_finishes_it(self):
        from services.music import jobs
        self.request()
        jobs.reconcile("daily")(self.db)
        self.run_jobs()
        self.assertEqual(self.searches(), [SEARCH])
        self.assertFalse(self.row().request_pending)

    def test_startup_finishes_it(self):
        from services.music import jobs
        self.request()
        self.jobs.clear()
        with mock.patch("models.database.SessionLocal", self.Session):
            jobs.resume_requests()
        self.run_jobs()
        self.assertEqual(self.searches(), [SEARCH])

    def test_an_add_that_never_lands_is_owed_for_a_while_then_dropped(self):
        from services.music import jobs
        self.request(land=False)
        self.assertNotIn(10, FakeLidarr.state.get("albums", {}))
        jobs.finish_pending_requests(self.db)
        self.assertTrue(self.row().request_pending)          # may still land
        row = self.row()
        row.requested_at = datetime.utcnow() - _wait() - timedelta(minutes=1)
        self.db.commit()
        jobs.finish_pending_requests(self.db)
        self.assertFalse(self.row().request_pending)
        self.assertEqual(self.searches(), [])

    def test_the_owed_row_survives_the_artist_sync_until_found(self):
        from services.music import jobs
        self.request(land=False)
        jobs.reconcile("daily")(self.db)
        self.assertTrue(self.row().request_pending)

    def test_a_restart_before_it_lands_keeps_looking_until_it_does(self):
        from services.music import jobs
        from services.media_requests import RequestRefused, request_album
        orig = FakeLidarr.do_POST
        gate = []

        def held_add(handler):   # the add commits only when the test says so
            if handler.path.startswith("/api/v1/album"):
                import time as t
                while not gate:
                    t.sleep(0.05)
            return orig(handler)
        with mock.patch.object(FakeLidarr, "do_POST", held_add), mock.patch("services.lidarr.TIMEOUT", 0.3):
            with self.assertRaises(RequestRefused):
                request_album(self.db, RG, user_id=1, via="test")
            looks = []
            with mock.patch.object(jobs, "look_again_later", side_effect=lambda *a: looks.append(1)):
                # Tentacle restarts: the scheduled look is gone; startup runs before the add lands.
                jobs.finish_pending_requests(self.db)
                self.assertEqual(looks, [1])            # the look is armed again
                self.assertTrue(self.row().request_pending)
                gate.append(1)
                time.sleep(0.5)
                jobs.finish_pending_requests(self.db, unconfirmed_only=True)   # the re-armed look
                self.assertEqual(looks, [1])            # nothing left to look for
        self.assertEqual(self.searches(), [SEARCH])
        self.assertFalse(self.row().request_pending)

    def test_lidarr_down_during_the_look_keeps_it_armed(self):
        from services.music import jobs
        self.request(land=False)
        looks = []
        def down(handler):
            return handler._send(503, {"message": "down"})
        with mock.patch.object(FakeLidarr, "do_GET", down), mock.patch("services.lidarr.RETRY_DELAYS", (0, 0)), \
                mock.patch.object(jobs, "look_again_later", side_effect=lambda *a: looks.append(1)):
            errors = []
            jobs.finish_pending_requests(self.db, errors)
        self.assertEqual(looks, [1])
        self.assertEqual(len(errors), 1)
        self.assertTrue(self.row().request_pending)

    def test_lidarr_down_after_the_wait_stops_looking(self):
        from services.music import jobs
        self.request(land=False)
        row = self.row()
        row.requested_at = datetime.utcnow() - timedelta(days=7)
        self.db.commit()
        looks = []

        def down(handler):
            return handler._send(503, {"message": "down"})
        with mock.patch.object(FakeLidarr, "do_GET", down), mock.patch("services.lidarr.RETRY_DELAYS", (0, 0)), \
                mock.patch.object(jobs, "look_again_later", side_effect=lambda *a: looks.append(1)):
            for _ in range(3):
                jobs.finish_pending_requests(self.db, unconfirmed_only=True)
        self.assertEqual(looks, [])          # no 5-minute looks past the wait
        self.assertTrue(self.row().request_pending)   # the next successful look (startup, daily check) settles it
        jobs.finish_pending_requests(self.db)
        self.assertFalse(self.row().request_pending)

    def test_an_add_that_never_landed_leaves_no_waiting_note(self):
        from services.music import jobs
        self.request(land=False)
        self.assertIn("waiting for Lidarr", self.row().verdict["state"])
        row = self.row()
        row.requested_at = datetime.utcnow() - timedelta(days=1)
        self.db.commit()
        jobs.finish_pending_requests(self.db)
        row = self.row()
        self.assertFalse(row.request_pending)
        self.assertFalse((row.verdict or {}).get("state"))

    def test_the_look_only_touches_unconfirmed_adds(self):
        from models.database import MusicAlbum
        from services.music import jobs
        FakeLidarr.state["albums"] = {7: lidarr_album(7, "33333333-3333-3333-3333-333333333333",
                                                      [lidarr_release("r1", 12, monitored=True)])}
        self.db.add(MusicAlbum(mbid="33333333-3333-3333-3333-333333333333", lidarr_album_id=7, title="Other",
                               request_pending=True, requested_at=datetime.utcnow()))
        self.db.commit()
        jobs.finish_pending_requests(self.db, unconfirmed_only=True)
        self.assertEqual(self.calls("GET", "/api/v1/album/7"), [])

    def test_a_refused_add_is_not_owed(self):
        from services.media_requests import RequestRefused, request_album
        orig = FakeLidarr.do_POST

        def refuse(handler):
            if handler.path.startswith("/api/v1/album"):
                handler._body()
                return handler._send(400, [{"errorMessage": "Invalid root folder"}])
            return orig(handler)
        with mock.patch.object(FakeLidarr, "do_POST", refuse):
            with self.assertRaises(RequestRefused):
                request_album(self.db, RG, user_id=1, via="test")
        self.assertIsNone(self.row())


if __name__ == "__main__":
    unittest.main()


class _Lidarr:
    """In-process Lidarr for the property: the add's answer may be lost, and the
    add may commit before or after Tentacle's look, or never."""

    def __init__(self, rnd, mode):
        self.rnd, self.mode = rnd, mode
        self.albums, self.adds, self.searches = {}, 0, []
        self.pending_add = None   # an add that commits later

    def land(self):
        if self.pending_add:
            self.albums[10] = self.pending_add
            self.pending_add = None

    def album_by_mbid(self, rgid):
        a = self.albums.get(10)
        return dict(a) if a and a["foreignAlbumId"] == rgid else None

    def lookup_album(self, rgid):
        return {"foreignAlbumId": rgid, "title": "Radio City",
                "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}

    def add_album(self, resource):
        from services.lidarr import LidarrError
        self.adds += 1
        added = lidarr_album(10, RG, [lidarr_release("r12", 12, monitored=True), lidarr_release("r13", 13)])
        if self.mode == "ok":
            self.albums[10] = added
            return dict(added)
        if self.mode == "lost_landed":
            self.albums[10] = added
        elif self.mode == "lost_late":
            self.pending_add = added
        raise LidarrError("Lidarr did not answer within 15s")   # lost_never too

    def album(self, album_id):
        from services.lidarr import LidarrError
        if album_id not in self.albums:
            raise LidarrError("Lidarr refused it (HTTP 404).", 404)
        return dict(self.albums[album_id])

    def set_monitored(self, ids, monitored):
        for i in ids:
            self.albums[i]["monitored"] = monitored

    def pin_release(self, album, rid):
        self.albums[album["id"]]["anyReleaseOk"] = False

    def search_albums(self, ids):
        self.searches += list(ids)

    def artists(self):
        return [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]

    def albums_by_artist(self, artist_id):
        return [dict(a) for a in self.albums.values()]

    def queue(self, max_pages=20):
        return []


class TestAddOutcomesProperty(_Base):
    """Every add outcome, with Tentacle's later looks at random times: an album that
    lands is searched exactly once, nothing is added twice by Tentacle, and an add
    that never lands stops being owed. SEEDS env (default 300); the seed is in the
    failure message."""

    def test_random_add_outcomes(self):
        import os
        import random
        from models.database import MusicAlbum
        from services.media_requests import RequestRefused, request_album
        from services.music import jobs, library
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
        seeds = int(os.environ.get("SEEDS", "300"))
        clock = [datetime(2026, 10, 2, 12, 0)]

        class _Clock(datetime):
            @classmethod
            def utcnow(cls):
                return clock[0]
        for seed in range(seeds):
            rnd = random.Random(seed)
            mode = rnd.choice(["ok", "lost_landed", "lost_late", "lost_never"])
            fake = _Lidarr(rnd, mode)
            self.db.query(MusicAlbum).delete()
            self.db.commit()
            self.jobs.clear()
            clock[0] = start = datetime(2026, 10, 2, 12, 0)
            msg = f"seed {seed} mode {mode}"
            with mock.patch.object(library, "lidarr_client", return_value=fake), \
                    mock.patch("services.media_requests.datetime", _Clock), \
                    mock.patch("services.music.jobs.datetime", _Clock), \
                    mock.patch("services.music.jobs.time.sleep"):
                try:
                    request_album(self.db, RG, user_id=1, via="property")
                    refused = False
                except RequestRefused:
                    refused = True
                self.assertEqual(refused, mode in ("lost_late", "lost_never"), msg)
                self.run_jobs()
                for _ in range(rnd.randint(1, 6)):
                    step = rnd.choice(["land", "look", "daily", "hours"])
                    if step == "land" and rnd.random() < 0.7 \
                            and clock[0] - start < _wait():   # a late add lands within minutes
                        fake.land()
                    elif step == "look":
                        jobs.finish_pending_requests(self.db)
                    elif step == "daily":
                        jobs.reconcile("daily")(self.db)
                    else:
                        clock[0] += timedelta(hours=rnd.choice([1, 3, 7, 30]))
                    self.run_jobs()
                    self.assertLessEqual(fake.searches.count(10), 1, msg)
                    self.assertEqual(fake.adds, 1, msg)
                    self.db.expire_all()
                    rows = self.db.query(MusicAlbum).filter_by(mbid=RG).all()
                    self.assertLessEqual(len(rows), 1, msg)
                # Liveness: once in Lidarr, the next look finishes it.
                if 10 in fake.albums:
                    jobs.finish_pending_requests(self.db)
                    self.assertEqual(fake.searches.count(10), 1, msg)
                # An add that never lands stops being owed after the wait.
                clock[0] += _wait() + timedelta(minutes=1)
                if mode == "lost_never" or fake.pending_add:
                    fake.pending_add = None
                    jobs.finish_pending_requests(self.db)
                    self.db.expire_all()
                    self.assertEqual(self.db.query(MusicAlbum).filter_by(request_pending=True).count(), 0, msg)
