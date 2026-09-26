"""A burst of Sonarr webhook events is one scan, not one per event (#182).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Every event ran its own full Sonarr scan + Jellyfin listing in its own thread:
five EpisodeFileDelete events for one series in 2 s were five concurrent scans
of 176 series, 25 "listing stopped early", repeated TMDB 404s for one id, and
tag-push and playlist-move timeouts for the next nine minutes.
"""
import logging
import tempfile
import threading
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.sonarr as sonarr


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _event(event_type, tmdb_id, title, episodes=()):
    return {"eventType": event_type, "series": {"tmdbId": tmdb_id, "title": title},
            "episodes": [{"seasonNumber": 1, "episodeNumber": e, "title": f"Ep {e}"} for e in episodes]}


class Coalescing(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.scans, self.after = [], []
        patches = [
            mock.patch.object(sonarr, "_check_webhook_auth", lambda request, db: None),
            mock.patch.object(sonarr, "scan_sonarr_library", lambda db: self.scans.append(time.monotonic())),
            mock.patch.object(sonarr, "_after_scan",
                              lambda db, tmdb_id, title, event_type, first_episode=None, episode_count=1:
                              self.after.append((tmdb_id, event_type, episode_count,
                                                 (first_episode or {}).get("episodeNumber")))),
            mock.patch.object(sonarr, "WEBHOOK_QUIET_SECONDS", 0.2),
            mock.patch.object(sonarr, "WEBHOOK_MAX_WAIT_SECONDS", 2.0),
            mock.patch("models.database.SessionLocal", self.Session),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, payload):
        return sonarr.sonarr_webhook(payload, request=mock.Mock(), db=self.db)

    def _wait(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            worker = sonarr._webhook_state["worker"]
            if worker is None and not sonarr._webhook_pending:
                return
            if worker is not None:
                worker.join(0.1)
        self.fail("the webhook worker never finished")

    def test_a_burst_for_one_series_is_one_scan(self):
        for _ in range(5):
            self.assertEqual("processing", self._post(_event("EpisodeFileDelete", 1399, "Friends"))["status"])
        self._wait()
        self.assertEqual(1, len(self.scans))
        self.assertEqual([(1399, "EpisodeFileDelete", 1, None)], self.after)

    def test_two_series_share_one_scan_and_each_gets_its_follow_up(self):
        self._post(_event("Download", 1399, "Friends", [1]))
        self._post(_event("SeriesAdd", 1668, "Other"))
        self._wait()
        self.assertEqual(1, len(self.scans))
        self.assertEqual({1399, 1668}, {a[0] for a in self.after})

    def test_downloads_are_counted_and_outrank_other_events(self):
        self._post(_event("EpisodeFileDelete", 1399, "Friends"))
        self._post(_event("Download", 1399, "Friends", [3]))
        self._post(_event("Download", 1399, "Friends", [4]))
        self._wait()
        self.assertEqual([(1399, "Download", 2, 4)], self.after)

    def test_events_during_a_scan_make_the_next_batch(self):
        started, release = threading.Event(), threading.Event()

        def slow_scan(db):
            self.scans.append(time.monotonic())
            if len(self.scans) == 1:
                started.set()
                release.wait(5)
        with mock.patch.object(sonarr, "scan_sonarr_library", slow_scan):
            self._post(_event("Download", 1399, "Friends", [1]))
            self.assertTrue(started.wait(5))
            self._post(_event("Download", 1399, "Friends", [2]))
            self._post(_event("Download", 1399, "Friends", [3]))
            release.set()
            self._wait()
        self.assertEqual(2, len(self.scans))
        self.assertEqual([(1399, "Download", 1, 1), (1399, "Download", 2, 3)], self.after)

    def test_a_failed_batch_does_not_strand_later_events(self):
        calls = []

        def broken_session():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return self.Session()
        with mock.patch("models.database.SessionLocal", broken_session):
            self._post(_event("SeriesAdd", 1399, "Friends"))
            self._wait()
            self.assertEqual([], self.scans)
            self._post(_event("SeriesAdd", 1399, "Friends"))
            self._wait()
        self.assertEqual(1, len(self.scans))

    def test_upgrade_deletes_are_still_ignored(self):
        payload = _event("EpisodeFileDelete", 1399, "Friends")
        payload["deleteReason"] = "upgrade"
        self.assertEqual("ignored", self._post(payload)["status"])
        self.assertEqual({}, sonarr._webhook_pending)


class NotificationForSeveralEpisodes(unittest.TestCase):
    def test_one_notification_names_the_count(self):
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        user = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="A")
        db.add(user)
        db.commit()
        db.add(mdb.Series(tmdb_id=1399, title="Friends", source="sonarr", tags=[]))
        db.add(mdb.DownloadRequest(tmdb_id=1399, media_type="series", user_id=user.id))
        db.commit()
        notified = []
        with mock.patch("services.smartlists._notify_jellyfin_plugin", notified.append):
            sonarr._after_scan(db, 1399, "Friends", "Download",
                               {"seasonNumber": 1, "episodeNumber": 4}, episode_count=3)
        messages = [n.message for n in db.query(mdb.Notification).all()]
        self.assertEqual(["Friends - 3 new episodes have completed and are ready to watch"], messages)
        # With Jellyfin not configured, jf_item was never assigned and the
        # NameError skipped the plugin notify.
        self.assertEqual(1, len(notified))


if __name__ == "__main__":
    unittest.main()
