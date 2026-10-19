"""An album request is pinned and searched even when its second half is cut off (#242).

request_album adds the album to Lidarr, then a worker job pins the original and
searches. The add's answer can be lost (Lidarr takes longer than the timeout
for a new artist) and a restart loses the queued job; either way the album used
to sit in Lidarr monitored, unpinned and never searched.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import (ARTIST, RG, FakeLidarr, _Base, lidarr_album,  # noqa: E402
                             lidarr_release, mb_release)


class TestUnfinishedRequests(_Base):
    def setUp(self):
        super().setUp()
        FakeLidarr.state["lookup"] = [{"foreignAlbumId": RG, "title": "Radio City",
                                       "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}]
        FakeLidarr.state["after_add"] = lidarr_album(10, RG, [lidarr_release("r13", 13, monitored=True),
                                                              lidarr_release("r12", 12)], title="Radio City")
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star",
                                        "path": "/data/music/Big Star"}]
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]

    def pinned_and_searched(self):
        puts = self.calls("PUT", "/api/v1/album/10")
        pinned = puts and [r["foreignReleaseId"] for r in puts[-1][2]["releases"] if r["monitored"]] == ["r12"]
        searched = {"name": "AlbumSearch", "albumIds": [10]} in [c[2] for c in self.calls("POST", "/api/v1/command")]
        return bool(pinned), searched

    def row(self):
        from models.database import MusicAlbum
        self.db.expire_all()
        return self.db.query(MusicAlbum).filter_by(mbid=RG).one()

    def test_an_add_whose_answer_is_lost_still_counts_and_is_finished(self):
        from services.media_requests import request_album
        FakeLidarr.state["add_answer_delay"] = 0.8
        with mock.patch("services.lidarr.TIMEOUT", 0.3):
            out = request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(out["status"], "requested")
        self.assertEqual(len(self.calls("POST", "/api/v1/album")), 1)   # the add isn't sent twice
        self.run_jobs()
        self.assertEqual(self.pinned_and_searched(), (True, True))
        self.assertFalse(self.row().request_pending)

    def test_a_restart_before_the_pin_is_finished_by_the_daily_check(self):
        from services.media_requests import request_album
        from services.music import jobs
        request_album(self.db, RG, user_id=1, via="test")
        self.assertTrue(self.row().request_pending)
        self.jobs.clear()   # Tentacle restarts: the in-memory queue is gone
        jobs.reconcile("daily")(self.db)
        self.assertEqual(self.pinned_and_searched(), (True, True))
        self.assertFalse(self.row().request_pending)

    def test_a_pin_already_right_still_gets_its_search(self):
        from services.music import jobs
        from services.media_requests import request_album
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        # Lidarr's default release happened to be the original: the pin is right already.
        album = FakeLidarr.state["albums"][10]
        album["anyReleaseOk"] = False
        for r in album["releases"]:
            r["monitored"] = r["foreignReleaseId"] == "r12"
        jobs.reconcile("daily")(self.db)
        self.assertTrue(self.pinned_and_searched()[1])
        self.assertFalse(self.row().request_pending)

    def test_startup_queues_unfinished_requests(self):
        from services.media_requests import request_album
        from services.music import jobs
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        with mock.patch("models.database.SessionLocal", self.Session):
            jobs.resume_requests()
        self.assertEqual(len(self.jobs), 1)
        self.run_jobs()
        self.assertEqual(self.pinned_and_searched(), (True, True))
        with mock.patch("models.database.SessionLocal", self.Session):
            jobs.resume_requests()
        self.assertEqual(self.jobs, [])   # nothing owed any more

    # The add itself lands after Tentacle's one look (#431): Lidarr reads a new
    # artist's metadata for longer than the timeout, then commits the album.

    def request_lost_in_lidarr(self, error="add_lands_late"):
        from services.media_requests import RequestRefused, request_album
        from services.music import jobs
        FakeLidarr.state[error] = 0.6
        with mock.patch("services.lidarr.TIMEOUT", 0.3), \
                mock.patch.object(jobs, "_schedule_once") as later, \
                self.assertRaises(RequestRefused) as refused:
            request_album(self.db, RG, user_id=1, via="test")
        FakeLidarr.state.pop(error)
        return refused.exception, later

    def lands(self):
        import copy
        FakeLidarr.state["albums"][10] = copy.deepcopy(FakeLidarr.state["after_add"])

    def searches(self):
        return [c for c in self.calls("POST", "/api/v1/command") if c[2].get("name") == "AlbumSearch"]

    def test_an_add_that_lands_after_the_look_is_finished_by_the_daily_check(self):
        from services.music import jobs
        refused, _ = self.request_lost_in_lidarr()
        self.assertEqual(refused.status, 502)
        self.assertIn("Tentacle pins the original and searches for it", refused.message)
        self.assertTrue(self.row().request_pending)
        self.assertFalse(self.row().monitored)   # still shown as requestable until it lands
        self.lands()
        for _ in range(3):
            jobs.reconcile("daily")(self.db)
        self.assertEqual(self.pinned_and_searched(), (True, True))
        self.assertEqual(len(self.searches()), 1)
        row = self.row()
        self.assertFalse(row.request_pending)
        self.assertEqual((row.lidarr_album_id, row.requested_by), (10, 1))

    def test_a_5xx_answer_is_owed_too_and_looked_for_again_soon(self):
        from services.music import jobs
        _, later = self.request_lost_in_lidarr("add_fails_5xx")
        self.assertTrue(later.called)   # looks again within minutes, not only at the daily check
        look = later.call_args_list[0][0][0]
        self.lands()
        look()   # the scheduled look queues the worker job
        self.run_jobs()
        self.assertEqual(self.pinned_and_searched(), (True, True))
        self.assertFalse(self.row().request_pending)

    def test_startup_looks_for_an_owed_add(self):
        from services.music import jobs
        self.request_lost_in_lidarr()
        self.lands()
        with mock.patch("models.database.SessionLocal", self.Session):
            jobs.resume_requests()
        self.run_jobs()
        self.assertEqual(self.pinned_and_searched(), (True, True))

    def test_an_owed_add_that_never_lands_is_dropped_after_some_hours(self):
        from datetime import datetime, timedelta
        from services.music import jobs
        self.request_lost_in_lidarr()
        jobs.reconcile("daily")(self.db)
        self.assertTrue(self.row().request_pending)   # not yet: it may still land
        row = self.row()
        row.requested_at = datetime.utcnow() - timedelta(hours=jobs.OWED_ADD_HOURS, minutes=1)
        self.db.commit()
        jobs.reconcile("daily")(self.db)
        from models.database import MusicAlbum
        self.assertEqual(self.db.query(MusicAlbum).filter_by(mbid=RG).count(), 0)
        self.assertEqual(self.searches(), [])

    def test_an_add_lidarr_refused_owes_nothing(self):
        from models.database import MusicAlbum
        from services.media_requests import RequestRefused, request_album
        FakeLidarr.state["add_refused"] = True
        with self.assertRaises(RequestRefused):
            request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(self.db.query(MusicAlbum).filter_by(mbid=RG).count(), 0)

    def test_an_album_with_files_by_then_is_left_to_the_daily_check(self):
        from services.media_requests import request_album
        from services.music import jobs
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        FakeLidarr.state["albums"][10]["statistics"]["trackFileCount"] = 13
        jobs.finish_pending_requests(self.db)
        self.assertEqual(self.pinned_and_searched(), (False, False))   # a pin could change files now
        self.assertFalse(self.row().request_pending)


if __name__ == "__main__":
    unittest.main()
