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
