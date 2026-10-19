"""Radarr "missingFromDisk" file deletes go through the storage-outage guard (#381).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_radarr_missing_from_disk_guard.py"

When Radarr's storage goes away while its root folder still has entries (a
share mounted below the root, a pool with a disk gone), its refresh deletes
each film's file record with reason MissingFromDisk and sends one
MovieFileDelete webhook per film. The scan refuses such a loss (#106,
services/radarr.file_loss_looks_like_an_outage); the webhook deleted every
row, its download request and its duplicate tombstones at once. Now these
deletes are collected until the burst is over and judged together, like the
scan's lost files, with the reports of the last hours (a burst that a pause
splits is judged as a whole). Other delete reasons act at once, as before.
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
        import time
        self.delay, self.fn, self.args, self.cancelled = delay, fn, args, False
        self.daemon = False
        self.due = time.monotonic() + delay

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
        if hasattr(radarr, "_missing_recent"):
            radarr._missing_recent.clear()
            self.addCleanup(radarr._missing_recent.clear)
        self.now = [1000.0]
        p = mock.patch("time.monotonic", lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)

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

    def advance(self, seconds):
        """Let time pass: a timer that comes due fires (as threading.Timer would)."""
        end = self.now[0] + seconds
        while True:
            due = [t for t in _Timer.armed if not t.cancelled and not getattr(t, "fired", False) and t.due <= end]
            if not due:
                break
            t = min(due, key=lambda t: t.due)
            self.now[0] = max(self.now[0], t.due)
            t.fired = True
            radarr._flush_missing_from_disk(*t.args, db=self.db)
        self.now[0] = end

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

    def test_the_timer_waits_for_ten_quiet_minutes_however_long_the_burst(self):
        self.films(range(6001, 6020))
        self.event(6001)
        self.assertEqual(_Timer.armed[-1].delay, radarr.MISSING_SETTLE_SECONDS)
        for k in range(2, 19):
            self.now[0] += 59          # events keep coming: each re-arms, no forced judgement
            self.event(6000 + k)
            self.assertTrue(_Timer.armed[-2].cancelled)
            self.assertEqual(_Timer.armed[-1].delay, radarr.MISSING_SETTLE_SECONDS)

    def test_probe_a_a_pause_inside_the_burst_loses_nothing(self):
        """Review F381-1 probe A: 20 of 20 films missing, a 70 s pause after the 8th."""
        self.films(range(1, 21))
        for i in range(1, 9):
            self.event(i)
            self.advance(1)
        self.advance(70)
        for i in range(9, 21):
            self.event(i)
            self.advance(1)
        self.advance(3600)
        self.assertEqual(self.counts()[:2], (20, 20), "a pause split the outage and a slice was removed")

    def test_probe_a_slices_judged_as_one_outage(self):
        """A pause longer than the settle time: the first slice cannot be told
        from a clean-up, but the rest of the outage is refused."""
        self.films(range(1, 21))
        for i in range(1, 9):
            self.event(i)
        self.assertEqual(self.flush(), {"status": "removed", "removed": 8})
        self.now[0] += radarr.MISSING_SETTLE_SECONDS + 300
        for i in range(9, 21):
            self.event(i)
        self.assertEqual(self.flush()["status"], "refused")
        self.assertEqual(self.counts()[:2], (12, 12))

    def test_probe_b_a_burst_longer_than_ten_minutes_loses_nothing(self):
        """Review F381-1 probe B: a large library reported at one film a second
        (1,500 s, longer than any fixed cap): judged once, at the end."""
        self.films(range(1, 1501))
        for i in range(1, 1501):
            self.event(i)
            self.advance(1)
        self.advance(3600)
        self.assertEqual(self.db.query(mdb.Movie).count(), 1500, "a long burst was removed slice by slice")

    def test_probe_c_a_scan_removed_the_row_first_the_playlists_are_still_cleaned(self):
        """Review F381-2 probe C: a scan inside the wait removes the row (it does
        no playlist clean-up); the judged report still starts it, by folder."""
        self.films(range(2001, 2011))
        self.event(2003)
        self.db.query(mdb.Movie).filter_by(tmdb_id=2003).delete()    # what the scan does
        self.db.commit()
        self.assertEqual(self.flush(), {"status": "removed", "removed": 0})
        self.assertEqual(_Thread.started, [("_cleanup_playlists_all_users", (2003, "movie"),
                                            {"arr_folder": "/data/movies/Film 2003 (2001)"})])
        self.assertEqual(self.db.query(mdb.DownloadRequest).filter_by(tmdb_id=2003).count(), 0)

    def test_a_report_older_than_the_window_no_longer_counts(self):
        self.films(range(1, 11))
        for i in (1, 2):
            self.event(i)
        self.assertEqual(self.flush()["removed"], 2)
        self.now[0] += radarr.MISSING_WINDOW_SECONDS + 1
        for i in (3, 4, 5):
            self.event(i)
        self.assertEqual(self.flush()["removed"], 3, "3 of 8: removed when the earlier 2 are hours old")

    def test_reports_inside_the_window_add_up(self):
        self.films(range(1, 11))
        for i in (1, 2):
            self.event(i)
        self.assertEqual(self.flush()["removed"], 2)
        self.now[0] += 3600
        for i in (3, 4, 5, 6):
            self.event(i)
        self.assertEqual(self.flush()["status"], "refused", "6 of 10 in an hour, 4 of them now: refused")

    def test_a_reimport_takes_a_film_out_of_the_window(self):
        self.films(range(1, 11))
        self.event(1), self.event(2), self.event(3)
        self.assertEqual(self.flush()["removed"], 3)
        with mock.patch.object(radarr, "scan_radarr_library", lambda db: {}):
            for i in (1, 2, 3):
                self.event(i, event="Download", reason=None)
        self.films([1, 2, 3])
        self.event(4), self.event(5)
        self.assertEqual(self.flush()["removed"], 2)

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

        def flaky(db, tmdb_id, title, folder, **kw):
            if tmdb_id == 8001:
                raise RuntimeError("database is locked")
            return real(db, tmdb_id, title, folder, **kw)
        with mock.patch.object(radarr, "_remove_downloaded_movie", flaky):
            self.assertEqual(self.flush()["removed"], 1)
        self.assertIsNotNone(self.db.query(mdb.Movie).filter_by(tmdb_id=8001).first())

    def test_property_a_batch_is_removed_only_when_the_scan_would_remove_it(self):
        """1,000 seeds: random libraries and random event streams (missing,
        manual deletes, upgrades, re-imports, Radarr deletes, scans removing
        a row, time passing), checked at every flush. Invariants: nothing
        reported missing is removed before its burst is judged; a burst is
        kept whole exactly when the scan's guard refuses the loss counted with
        the window's earlier reports, else exactly its still-present films go
        (with the playlist clean-up, also for a film a scan removed first);
        other reasons act at once."""
        for seed in range(1000):
            rng = random.Random(seed)
            self.db.query(mdb.Movie).delete()
            self.db.query(mdb.DownloadRequest).delete()
            self.db.commit()
            radarr._missing_pending.clear()
            radarr._missing_recent.clear()
            _Timer.armed = []
            ids = list(range(10000, 10000 + rng.randint(1, 15)))
            self.films(ids)
            pending = set()
            window = {}          # model: tmdb_id -> (time judged, was a downloaded row)
            with mock.patch.object(radarr, "scan_radarr_library", lambda db: {}):
                for step in range(rng.randint(1, 30)):
                    i = rng.choice(ids)
                    present = {m.tmdb_id for m in self.db.query(mdb.Movie)}
                    a = rng.choice(["missing"] * 5 + ["manual", "upgrade", "download", "moviedelete", "flush",
                                                      "flush", "scanremoves", "time"])
                    msg = f"seed {seed} step {step} {a} {i}"
                    if a == "missing":
                        self.event(i)
                        pending.add(i)
                        self.assertEqual({m.tmdb_id for m in self.db.query(mdb.Movie)}, present, msg)
                    elif a == "manual":
                        self.event(i, reason="manual")
                        pending.discard(i)
                        window.pop(i, None)
                        self.assertNotIn(i, {m.tmdb_id for m in self.db.query(mdb.Movie)}, msg)
                    elif a == "upgrade":
                        self.event(i, reason="upgrade")
                        self.assertEqual({m.tmdb_id for m in self.db.query(mdb.Movie)}, present, msg)
                    elif a == "download":
                        self.event(i, event="Download", reason=None)
                        pending.discard(i)
                        window.pop(i, None)
                    elif a == "moviedelete":
                        self.event(i, event="MovieDelete", reason=None)
                        pending.discard(i)
                        window.pop(i, None)
                    elif a == "scanremoves":
                        self.db.query(mdb.Movie).filter_by(tmdb_id=i).delete()
                        self.db.commit()
                    elif a == "time":
                        self.now[0] += rng.choice((30, 600, 3600, 4 * 3600, 7 * 3600))
                    elif pending:
                        window = {k: v for k, v in window.items()
                                  if self.now[0] - v[0] <= radarr.MISSING_WINDOW_SECONDS}
                        recent = {k: v for k, v in window.items() if k not in pending}
                        gone = sum(1 for k, (_, was) in recent.items() if was and k not in present)
                        lost_now = pending & present
                        lost = len(lost_now) + len(present & set(recent)) + gone
                        _Thread.started = []
                        out = self.flush()
                        after = {m.tmdb_id for m in self.db.query(mdb.Movie)}
                        if file_loss_looks_like_an_outage(lost, len(present) + gone):
                            self.assertEqual(out["status"], "refused", msg)
                            self.assertEqual(after, present, msg)
                            self.assertEqual(_Thread.started, [], msg)
                        else:
                            self.assertEqual(out["status"], "removed", msg)
                            self.assertEqual(after, present - pending, msg)
                            self.assertEqual(sorted(c[1][0] for c in _Thread.started), sorted(pending), msg)
                        for k in pending:
                            window[k] = (self.now[0], k in present)
                        pending = set()
                    self.assertEqual(set(radarr._missing_pending), pending, msg)

if __name__ == "__main__":
    unittest.main()
