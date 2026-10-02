"""Radarr "missingFromDisk" file deletes go through the storage-outage guard (#381).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_radarr_missing_from_disk_guard.py"

When Radarr's storage goes away while its root folder still has entries (a
share mounted below the root, a pool with a disk gone), its refresh deletes
each film's file record with reason MissingFromDisk and sends one
MovieFileDelete webhook per film. The scan refuses such a loss (#106,
services/radarr.file_loss_looks_like_an_outage); the webhook deleted every
row, its download request and its duplicate tombstones at once. Now these
deletes are collected until the burst is over and judged together, like the
scan's lost files. Other delete reasons act at once, as before.
"""
import logging
import random
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.radarr as radarr  # noqa: E402
from services.radarr import file_loss_looks_like_an_outage  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


class _Thread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.call = (getattr(target, "__name__", str(target)), args, kwargs or {})

    def start(self):
        _Thread.started.append(self.call)


class _Timer:
    armed = []

    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args, self.cancelled = delay, fn, args, False
        self.daemon = False

    def start(self):
        _Timer.armed.append(self)

    def cancel(self):
        self.cancelled = True


class MissingFromDiskGuard(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{Path(temp_dir(self))}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="requester")
        self.db.add(self.user)
        self.db.commit()
        _Thread.started, _Timer.armed = [], []
        for name, value in (("_check_webhook_auth", lambda *a, **k: None),
                            ("emit_library_event", lambda *a, **k: None),
                            ("log_activity", lambda *a, **k: None)):
            p = mock.patch.object(radarr, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("Thread", _Thread), ("Timer", _Timer)):
            p = mock.patch.object(radarr.threading, name, value)
            p.start()
            self.addCleanup(p.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        if hasattr(radarr, "_missing_pending"):     # (absent before #381)
            radarr._missing_pending.clear()
            self.addCleanup(radarr._missing_pending.clear)

    def films(self, ids, duplicate=False):
        for i in ids:
            self.db.add(mdb.Movie(tmdb_id=i, title=f"Film {i}", year="2001", source="radarr",
                                  radarr_path=f"/data/movies/Film {i} (2001)/Film.mkv"))
            self.db.add(mdb.DownloadRequest(tmdb_id=i, media_type="movie", user_id=self.user.id))
            if duplicate:
                self.db.add(mdb.Duplicate(tmdb_id=i, media_type="movie"))
        self.db.commit()

    def event(self, tmdb_id, event="MovieFileDelete", reason="missingFromDisk"):
        payload = {"eventType": event,
                   "movie": {"tmdbId": tmdb_id, "title": f"Film {tmdb_id}",
                             "folderPath": f"/data/movies/Film {tmdb_id} (2001)"},
                   "movieFile": {"path": f"/data/movies/Film {tmdb_id} (2001)/Film.mkv"}}
        if reason is not None:
            payload["deleteReason"] = reason
        return radarr.radarr_webhook(payload, None, self.db)

    def flush(self):
        """What the armed timer does once the burst is over."""
        live = [t for t in _Timer.armed if not t.cancelled]
        self.assertTrue(live, "no timer armed")
        return radarr._flush_missing_from_disk(*live[-1].args, db=self.db)

    def counts(self):
        return (self.db.query(mdb.Movie).count(), self.db.query(mdb.DownloadRequest).count(),
                self.db.query(mdb.Duplicate).count())

    def test_a_burst_that_looks_like_an_outage_keeps_everything(self):
        ids = list(range(1001, 1007))
        self.films(ids, duplicate=True)
        for i in ids:
            self.assertEqual(self.event(i)["status"], "queued")
        self.assertEqual(self.counts(), (6, 6, 6), "removed before the burst was judged")
        self.assertEqual(self.flush(), {"status": "refused", "kept": 6})
        self.assertEqual(self.counts(), (6, 6, 6))
        self.assertEqual(_Thread.started, [], "no playlist clean-up for kept films")
        self.assertTrue(file_loss_looks_like_an_outage(6, 6), "the scan refuses the same loss")

    def test_a_single_missing_file_is_removed_as_before(self):
        self.films(range(2001, 2011), duplicate=True)
        self.assertEqual(self.event(2003)["status"], "queued")
        self.assertEqual(self.flush(), {"status": "removed", "removed": 1})
        self.assertEqual(self.counts(), (9, 9, 9))
        self.assertIsNone(self.db.query(mdb.Movie).filter_by(tmdb_id=2003).first())
        self.assertEqual(_Thread.started, [("_cleanup_playlists_all_users", (2003, "movie"),
                                            {"arr_folder": "/data/movies/Film 2003 (2001)"})])

    def test_two_of_ten_are_removed_three_of_four_are_kept(self):
        self.films(range(3001, 3011))
        self.event(3001), self.event(3002)
        self.assertEqual(self.flush()["removed"], 2)
        self.db.query(mdb.Movie).delete()
        self.db.commit()
        self.films(range(3101, 3105))
        for i in (3101, 3102, 3103):
            self.event(i)
        self.assertEqual(self.flush()["status"], "refused")

    def test_other_reasons_act_at_once(self):
        self.films(range(4001, 4007), duplicate=True)
        for i in range(4001, 4007):
            self.assertEqual(self.event(i, reason="manual")["status"], "deleted")
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(_Timer.armed, [])
        self.assertEqual(self.event(4100, reason="upgrade")["status"], "ignored")
        self.assertEqual(self.event(4101, reason=None)["status"], "deleted")

    def test_reimported_before_the_check_is_not_removed(self):
        self.films(range(5001, 5011))
        self.event(5001), self.event(5002)
        with mock.patch.object(radarr, "scan_radarr_library", lambda db: {}):
            self.event(5001, event="Download", reason=None)    # storage back, Radarr imported it
        self.assertEqual(self.flush()["removed"], 1)
        self.assertIsNotNone(self.db.query(mdb.Movie).filter_by(tmdb_id=5001).first())
        self.assertIsNone(self.db.query(mdb.Movie).filter_by(tmdb_id=5002).first())

    def test_the_timer_waits_for_quiet_and_never_longer_than_the_cap(self):
        self.films(range(6001, 6020))
        now = [1000.0]
        with mock.patch("time.monotonic", lambda: now[0]):
            self.event(6001)
            self.assertEqual(_Timer.armed[-1].delay, radarr.MISSING_QUIET_SECONDS)
            for k in range(2, 7):
                now[0] += 59          # events keep coming: each re-arms
                self.event(6000 + k)
                self.assertTrue(_Timer.armed[-2].cancelled)
            self.assertEqual(_Timer.armed[-1].delay, radarr.MISSING_QUIET_SECONDS)
            now[0] = 1000.0 + radarr.MISSING_MAX_WAIT_SECONDS - 5
            self.event(6015)
            self.assertEqual(_Timer.armed[-1].delay, 5)
            now[0] += 30
            self.event(6016)
            self.assertEqual(_Timer.armed[-1].delay, 0)

    def test_a_superseded_timer_does_nothing(self):
        self.films(range(7001, 7011))
        self.event(7001)
        first = _Timer.armed[-1]
        self.event(7002)
        self.assertEqual(radarr._flush_missing_from_disk(*first.args, db=self.db), {"status": "superseded"})
        self.assertEqual(self.db.query(mdb.Movie).count(), 10)
        self.assertEqual(self.flush()["removed"], 2)

    def test_one_failing_delete_does_not_stop_the_others(self):
        self.films(range(8001, 8011))
        self.event(8001), self.event(8002)
        real = radarr._remove_downloaded_movie

        def flaky(db, tmdb_id, title, folder):
            if tmdb_id == 8001:
                raise RuntimeError("database is locked")
            return real(db, tmdb_id, title, folder)
        with mock.patch.object(radarr, "_remove_downloaded_movie", flaky):
            self.assertEqual(self.flush()["removed"], 1)
        self.assertIsNotNone(self.db.query(mdb.Movie).filter_by(tmdb_id=8001).first())

    def test_property_a_batch_is_removed_only_when_the_scan_would_remove_it(self):
        """1,000 seeds: random libraries and random event streams (missing,
        manual deletes, upgrades, re-imports, Radarr deletes), checked at every
        flush. Invariants: nothing reported missing is removed before its
        burst is judged; a burst is kept whole exactly when the scan's guard
        would refuse that loss, else exactly its still-present films go; other
        reasons act at once."""
        for seed in range(1000):
            rng = random.Random(seed)
            self.db.query(mdb.Movie).delete()
            self.db.query(mdb.DownloadRequest).delete()
            self.db.commit()
            radarr._missing_pending.clear()
            _Timer.armed = []
            ids = list(range(10000, 10000 + rng.randint(1, 15)))
            self.films(ids)
            pending = set()
            with mock.patch.object(radarr, "scan_radarr_library", lambda db: {}):
                for step in range(rng.randint(1, 25)):
                    i = rng.choice(ids)
                    present = {m.tmdb_id for m in self.db.query(mdb.Movie)}
                    a = rng.choice(["missing"] * 5 + ["manual", "upgrade", "download", "moviedelete", "flush"])
                    msg = f"seed {seed} step {step} {a} {i}"
                    if a == "missing":
                        self.event(i)
                        pending.add(i)
                        self.assertEqual({m.tmdb_id for m in self.db.query(mdb.Movie)}, present, msg)
                    elif a == "manual":
                        self.event(i, reason="manual")
                        pending.discard(i)
                        self.assertNotIn(i, {m.tmdb_id for m in self.db.query(mdb.Movie)}, msg)
                    elif a == "upgrade":
                        self.event(i, reason="upgrade")
                        self.assertEqual({m.tmdb_id for m in self.db.query(mdb.Movie)}, present, msg)
                    elif a == "download":
                        self.event(i, event="Download", reason=None)
                        pending.discard(i)
                    elif a == "moviedelete":
                        self.event(i, event="MovieDelete", reason=None)
                        pending.discard(i)
                    elif pending:
                        lost = pending & present
                        out = self.flush()
                        after = {m.tmdb_id for m in self.db.query(mdb.Movie)}
                        if file_loss_looks_like_an_outage(len(lost), len(present)):
                            self.assertEqual(out["status"], "refused", msg)
                            self.assertEqual(after, present, msg)
                        else:
                            self.assertEqual(after, present - lost, msg)
                        pending = set()
                    self.assertEqual(set(radarr._missing_pending), pending, msg)


if __name__ == "__main__":
    unittest.main()
