"""Radarr webhook: an event for a film that is still being processed is queued,
never dropped; a quality upgrade does not tell the requester again (#380).

A film's pass holds its per-film lock while it waits for the Radarr scan, and
scans run one at a time library-wide (#268): after a burst of adds that wait
is the whole queue. A Download that landed meanwhile was skipped ("Skipping
duplicate processing"), so the pass that held the lock ran as MovieAdded: no
"ready to watch", no downloaded_at, and after an upgrade the old file's path
until the next scan. Radarr sends Download with isUpgrade true when it
replaces a file with a better one; that used to send "ready to watch" again.

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_radarr_webhook_queued_events.py"
"""
import logging
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.radarr as radarr  # noqa: E402
from services.bad_copy import is_replacing, mark_replacing  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

TMDB = 999123
FOLDER = "/data/movies/Film (2001)"


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Webhook(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{Path(temp_dir(self))}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="requester")
        db.add(user)
        db.commit()
        db.add(mdb.DownloadRequest(tmdb_id=TMDB, media_type="movie", user_id=user.id))
        db.commit()
        self.uid = user.id
        db.close()

        # The scan waits for the gate: another film's scan is running (#268).
        self.gate = threading.Event()
        self.gate.set()
        self.in_scan = threading.Event()
        self.scans = []
        self.file = "Film (2001) WEBDL-1080p.mkv"
        self.threads = []
        test = self

        def fake_scan(db):
            # Stores the file Radarr has now, then holds the pass.
            test.scans.append(test.file)
            row = db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == TMDB).first()
            if not row:
                row = mdb.Movie(tmdb_id=TMDB, title="Film", year="2001", source="radarr")
                db.add(row)
            row.radarr_path = f"{FOLDER}/{test.file}"
            db.commit()
            test.in_scan.set()
            test.gate.wait(10)
            return {}

        real_thread = threading.Thread

        def tracking_thread(*a, **k):
            t = real_thread(*a, **k)
            test.threads.append(t)
            return t

        for target, value in (
                ("models.database.SessionLocal", self.Session),
                ("models.database.log_activity", lambda *a, **k: None),
                ("services.smartlists._notify_jellyfin_plugin", lambda db: None)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("scan_radarr_library", fake_scan),
                            ("_check_webhook_auth", lambda *a, **k: None),
                            ("emit_library_event", lambda *a, **k: None),
                            ("log_activity", lambda *a, **k: None)):
            p = mock.patch.object(radarr, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(radarr.threading, "Thread", tracking_thread)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.gate.set)

    def post(self, event, **extra):
        db = self.Session()
        try:
            return radarr.radarr_webhook({"eventType": event, **extra,
                                          "movie": {"tmdbId": TMDB, "title": "Film", "folderPath": FOLDER}},
                                         None, db)
        finally:
            db.close()

    def finish(self):
        self.gate.set()
        for t in list(self.threads):
            t.join(10)
            self.assertFalse(t.is_alive(), "a webhook pass never finished")

    def movie(self):
        db = self.Session()
        try:
            row = db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == TMDB).one()
            return row.downloaded_at, row.radarr_path
        finally:
            db.close()

    def notices(self):
        db = self.Session()
        try:
            return [n.message for n in db.query(mdb.Notification).filter(mdb.Notification.user_id == self.uid)]
        finally:
            db.close()


class EventsForABusyFilmAreQueued(_Webhook):
    def hold_in_scan(self, event, **extra):
        """A pass for the film that waits for its turn to scan."""
        self.gate.clear()
        self.post(event, **extra)
        self.assertTrue(self.in_scan.wait(5))

    def test_download_while_movieadded_waits_for_the_scan_is_processed(self):
        self.hold_in_scan("MovieAdded")
        self.assertEqual("processing", self.post("Download", isUpgrade=False)["status"])
        self.finish()
        downloaded_at, _ = self.movie()
        self.assertIsNotNone(downloaded_at, "Download dropped: downloaded_at never stamped")
        self.assertEqual(["Film has completed and is ready to watch"], self.notices())
        self.assertEqual(2, len(self.scans), "the Download runs its own pass after the running one")

    def test_upgrade_during_a_pass_stores_the_new_file_without_a_second_notice(self):
        self.hold_in_scan("Download", isUpgrade=False)
        self.file = "Film (2001) Bluray-1080p.mkv"
        self.post("Download", isUpgrade=True)
        self.finish()
        _, path = self.movie()
        self.assertEqual(f"{FOLDER}/Film (2001) Bluray-1080p.mkv", path)
        self.assertEqual(["Film has completed and is ready to watch"], self.notices())

    def test_a_burst_while_busy_is_one_follow_up_and_download_outranks(self):
        self.hold_in_scan("MovieAdded")
        self.post("Download", isUpgrade=False)
        self.post("MovieAdded")
        self.post("Download", isUpgrade=True)
        self.finish()
        self.assertEqual(2, len(self.scans), "queued events for one film make one follow-up pass")
        self.assertEqual(["Film has completed and is ready to watch"], self.notices(),
                         "a new download queued with an upgrade still tells the requester")

    def test_the_film_is_free_again_after_the_follow_up(self):
        self.hold_in_scan("MovieAdded")
        self.post("Download", isUpgrade=False)
        self.finish()
        self.assertFalse(radarr._webhook_locks[TMDB].locked())
        self.assertNotIn(TMDB, radarr._webhook_followups)
        self.post("MovieAdded")
        self.finish()
        self.assertEqual(3, len(self.scans))


class UpgradeIsNotANewDownload(_Webhook):
    def test_download_alone_notifies(self):
        self.post("Download")
        self.finish()
        self.assertEqual(["Film has completed and is ready to watch"], self.notices())

    def test_quality_upgrade_does_not_tell_the_requester_again(self):
        self.post("Download", isUpgrade=False)
        self.finish()
        self.post("Download", isUpgrade=True)
        self.finish()
        self.assertEqual(["Film has completed and is ready to watch"], self.notices())

    def test_bad_copy_replacement_still_says_a_new_copy_is_ready(self):
        db = self.Session()
        try:
            mark_replacing(db, "movie", TMDB)
        finally:
            db.close()
        self.post("Download", isUpgrade=True)
        self.finish()
        self.assertEqual(["A new copy of Film is ready to watch"], self.notices())
        db = self.Session()
        try:
            self.assertFalse(is_replacing(db, "movie", TMDB))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
