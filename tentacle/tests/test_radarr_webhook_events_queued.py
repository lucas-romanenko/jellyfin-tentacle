"""Radarr webhook: an event for a film that is busy is queued, not dropped;
a quality upgrade does not tell the requester "ready to watch" again (#380).

routers/radarr.py's background pass took a per-film lock and RETURNED when
another event for that film held it. The holder spends its time in
scan_radarr_library(), which since #268 runs one scan at a time library-wide,
so after a burst of adds a film's MovieAdded pass held its lock while it waited
its turn, and the film's Download landing then was skipped: no "ready to
watch", no downloaded_at. And every Download notified, upgrades too.

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_radarr_webhook_events_queued.py"
"""
import logging
import random
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.radarr as radarr  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

TMDB = 999123


class _Webhook(unittest.TestCase):
    def setUp(self):
        tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="requester")
        db.add(user)
        db.commit()
        db.add(mdb.DownloadRequest(tmdb_id=TMDB, media_type="movie", user_id=user.id))
        db.commit()
        self.uid = user.id
        db.close()

        self.gate = threading.Event()
        self.scans = 0
        self.threads = []
        test = self

        def fake_scan(db):
            # Another film's scan is running: this one waits its turn (#268 lock).
            test.scans += 1
            test.gate.wait(10)
            # By the time the scan runs, Radarr has imported the file.
            if not db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == TMDB).first():
                db.add(mdb.Movie(tmdb_id=TMDB, title="Film", year="2001", source="radarr",
                                 radarr_path="/data/movies/Film (2001)/Film.mkv"))
                db.commit()
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
                                          "movie": {"tmdbId": TMDB, "title": "Film",
                                                    "folderPath": "/data/movies/Film (2001)"}}, None, db)
        finally:
            db.close()

    def join(self):
        while any(t.is_alive() for t in self.threads):
            for t in list(self.threads):
                t.join(10)

    def notices(self):
        db = self.Session()
        try:
            return [n.message for n in db.query(mdb.Notification).filter(mdb.Notification.user_id == self.uid)]
        finally:
            db.close()

    def downloaded_at(self):
        db = self.Session()
        try:
            return db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == TMDB).one().downloaded_at
        finally:
            db.close()


class DownloadNotDropped(_Webhook):
    def test_download_arriving_while_movieadded_waits_for_the_scan_is_processed(self):
        self.post("MovieAdded")          # the add: waits for the scan lock
        time.sleep(0.3)
        self.post("Download")            # the import lands meanwhile
        time.sleep(0.3)
        self.gate.set()
        self.join()
        self.assertIsNotNone(self.downloaded_at(), "Download dropped: downloaded_at never stamped")
        self.assertEqual(1, len(self.notices()), "Download dropped: requester never told it is ready")
        self.assertEqual(2, self.scans, "the queued Download gets its own scan after the MovieAdded pass")

    def test_control_download_alone_notifies(self):
        self.gate.set()
        self.post("Download")
        self.join()
        self.assertEqual(["Film has completed and is ready to watch"], self.notices())

    def test_several_events_during_one_pass_are_one_more_pass(self):
        self.post("MovieAdded")
        time.sleep(0.3)
        for ev in ("Download", "MovieAdded", "Download"):
            self.post(ev)
        time.sleep(0.3)
        self.gate.set()
        self.join()
        self.assertEqual(2, self.scans)
        self.assertEqual(1, len(self.notices()))
        self.assertEqual({}, radarr._webhook_pending)
        self.assertFalse(radarr._webhook_locks[TMDB].locked())


class UpgradeNotRenotified(_Webhook):
    def test_quality_upgrade_does_not_tell_the_requester_again(self):
        self.gate.set()
        self.post("Download", isUpgrade=False)
        self.join()
        self.post("Download", isUpgrade=True)      # Radarr replaced the file with a better one
        self.join()
        self.assertEqual(1, len(self.notices()), self.notices())
        self.assertEqual(2, self.scans, "the upgrade is still scanned (new file path)")

    def test_upgrade_queued_behind_the_first_download(self):
        self.post("Download", isUpgrade=False)
        time.sleep(0.3)
        self.post("Download", isUpgrade=True)
        time.sleep(0.3)
        self.gate.set()
        self.join()
        self.assertEqual(1, len(self.notices()))
        self.assertEqual(2, self.scans)

    def test_first_download_and_upgrade_queued_together_still_notify_once(self):
        self.post("MovieAdded")
        time.sleep(0.3)
        self.post("Download", isUpgrade=False)
        self.post("Download", isUpgrade=True)
        time.sleep(0.3)
        self.gate.set()
        self.join()
        self.assertEqual(1, len(self.notices()))

    def test_bad_copy_replacement_still_says_a_new_copy_is_ready(self):
        from services.bad_copy import mark_replacing, is_replacing
        db = self.Session()
        mark_replacing(db, "movie", TMDB)
        db.close()
        self.gate.set()
        self.post("Download", isUpgrade=True)
        self.join()
        self.assertEqual(["A new copy of Film is ready to watch"], self.notices())
        db = self.Session()
        self.assertFalse(is_replacing(db, "movie", TMDB))
        db.close()

    def test_isupgrade_that_is_not_true_counts_as_a_first_download(self):
        self.gate.set()
        self.post("Download", isUpgrade="yes")
        self.join()
        self.assertEqual(1, len(self.notices()))


class PassQueueFaults(unittest.TestCase):
    """_run_webhook_passes itself: failures never strand a lock or a queued event."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        radarr._webhook_pending.clear()
        radarr._webhook_locks.clear()

    def test_a_failing_pass_still_runs_the_one_queued_behind_it(self):
        ran = []

        def process(tid, title, ev, up):
            ran.append(ev)
            if len(ran) == 1:
                radarr._run_webhook_passes(tid, title, "Download", False, process)   # arrives mid-pass
                raise RuntimeError("Jellyfin down")

        radarr._run_webhook_passes(7, "F", "MovieAdded", False, process)
        self.assertEqual(["MovieAdded", "Download"], ran)
        self.assertFalse(radarr._webhook_locks[7].locked())
        self.assertEqual({}, radarr._webhook_pending)

    def test_baseexception_releases_the_lock(self):
        def process(tid, title, ev, up):
            radarr._run_webhook_passes(tid, title, "Download", False, process)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            radarr._run_webhook_passes(8, "F", "MovieAdded", False, process)
        self.assertFalse(radarr._webhook_locks[8].locked())
        self.assertNotIn(8, radarr._webhook_pending)

    def test_lock_dict_stays_bounded(self):
        for i in range(600):
            radarr._run_webhook_passes(10_000 + i, "F", "Download", False, lambda *a: None)
        radarr._run_webhook_passes(1, "F", "Download", False, lambda *a: None)
        self.assertLessEqual(len(radarr._webhook_locks), 514)


class PassQueueProperty(unittest.TestCase):
    """Random event streams for a few films, arriving before, between and
    DURING passes (re-entrant calls from inside a pass = an event landing while
    the film is busy), with random failing passes:
    - no event is lost: every event is followed by a pass of that film that
      starts after it and is at least as strong (Download >= MovieAdded);
    - a film's passes never overlap;
    - exactly one notice per film that had a first (non-upgrade) download;
    - nothing left locked or queued at the end."""
    SEEDS = 2000

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        radarr._webhook_pending.clear()
        radarr._webhook_locks.clear()

    def test_random_interleavings(self):
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            radarr._webhook_pending.clear()
            radarr._webhook_locks.clear()
            films = list(range(1, rnd.randint(1, 3) + 1))
            first_download = {f: rnd.random() < 0.8 for f in films}
            per_film = {}
            for f in films:
                evs = [("MovieAdded", False)] * rnd.randint(0, 2) + [("Download", True)] * rnd.randint(0, 2)
                rnd.shuffle(evs)
                if first_download[f]:
                    # a first download comes before that film's upgrades
                    ups = [i for i, e in enumerate(evs) if e == ("Download", True)]
                    evs.insert(rnd.randint(0, ups[0] if ups else len(evs)), ("Download", False))
                per_film[f] = [(f, ev, up) for ev, up in evs]
            stream = []      # films interleaved, each film's own order kept
            while any(per_film.values()):
                f = rnd.choice([f for f in films if per_film[f]])
                stream.append(per_film[f].pop(0))
            log = []                 # ("arrive", f, ev, n) / ("start", f, ev, n) / ("end", f)
            running = set()
            notices = {f: 0 for f in films}
            clock = [0]
            remaining = list(stream)

            def tick():
                clock[0] += 1
                return clock[0]

            def process(f, title, ev, up):
                self.assertNotIn(f, running, f"seed {seed}: overlapping passes for film {f}")
                running.add(f)
                log.append(("start", f, ev, tick()))
                # events landing while this film is busy
                while remaining and rnd.random() < 0.5:
                    nf, nev, nup = remaining.pop(0)
                    log.append(("arrive", nf, nev, tick()))
                    radarr._run_webhook_passes(nf, "F", nev, nup, process)
                fail = rnd.random() < 0.15
                if ev == "Download" and not up and not fail:
                    notices[f] += 1
                running.discard(f)
                log.append(("end", f, ev, tick()))
                if fail:
                    raise RuntimeError("injected")

            while remaining:
                nf, nev, nup = remaining.pop(0)
                log.append(("arrive", nf, nev, tick()))
                radarr._run_webhook_passes(nf, "F", nev, nup, process)

            msg = f"seed {seed}: {log}"
            for i, (kind, f, ev, t) in enumerate(log):
                if kind != "arrive":
                    continue
                later = [e for e in log[i + 1:] if e[0] == "start" and e[1] == f
                         and (ev != "Download" or e[2] == "Download")]
                self.assertTrue(later, f"lost {ev} for film {f} at {t}; " + msg)
            # A pass that failed owes its notice again only if Radarr sends it
            # again; injected failures make "at most one" the check there.
            for f in films:
                self.assertLessEqual(notices[f], 1, msg)
            self.assertEqual({}, radarr._webhook_pending, msg)
            self.assertFalse(any(lk.locked() for lk in radarr._webhook_locks.values()), msg)

    def test_random_interleavings_without_failures_notify_exactly_once(self):
        for seed in range(self.SEEDS):
            rnd = random.Random(10_000 + seed)
            radarr._webhook_pending.clear()
            radarr._webhook_locks.clear()
            evs = [("MovieAdded", False)] * rnd.randint(0, 3) + [("Download", True)] * rnd.randint(0, 3)
            rnd.shuffle(evs)
            first = rnd.random() < 0.7
            if first:
                evs.insert(rnd.randint(0, len(evs)), ("Download", False))
            notices = [0]
            remaining = list(evs)

            def process(f, title, ev, up):
                while remaining and rnd.random() < 0.6:
                    nev, nup = remaining.pop(0)
                    radarr._run_webhook_passes(1, "F", nev, nup, process)
                if ev == "Download" and not up:
                    notices[0] += 1

            while remaining:
                nev, nup = remaining.pop(0)
                radarr._run_webhook_passes(1, "F", nev, nup, process)
            # Upgrades that arrive before the first download are not
            # realistic but must not invent or swallow the notice.
            self.assertEqual(1 if first else 0, notices[0], f"seed {10_000 + seed}: {evs}")
            self.assertEqual({}, radarr._webhook_pending)


if __name__ == "__main__":
    unittest.main()
