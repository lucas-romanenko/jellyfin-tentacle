"""'Bad copy? Get another one' when Radarr/Sonarr's file delete answers late, or not at all.

Radarr/Sonarr delete the file before they answer DELETE moviefile/episodefile,
and with a recycle bin on another drive they first copy the whole file there.
That can take longer than Tentacle's 30 s timeout, and they carry on and
delete the file all the same. Before the fix, replace_movie/replace_episode
stopped at the timed-out DELETE: the release was already blocklisted and the
file then went away, but no search was started, the Deletion log had no row,
and pressing the button again answered 409 ("no file").

What must hold (checked by the cases below and, over random faults at every
call, by TestFaultInjection):
  I1  once the file is gone, the search for another copy is started (the
      title monitored first when it wasn't), or the answer says the file is
      deleted and the search didn't start; never an error that hides it;
  I2  the Deletion log gets one row exactly when the press has seen the file
      go, written before the monitor/search calls (so their failure can't
      lose it), and none while the file is still there;
  I3  "ok" only when the file is gone and the search was accepted;
  I4  monitor and search only after the delete (Radarr/Sonarr's "unmonitor
      deleted" would undo an earlier monitor; a search while the file is
      there finds no upgrade), and the DELETE is sent once;
  I5  a refusal with the file still there is answered at once, as before;
  I6  the press answers before the Jellyfin plugin stops listening.

No real sleeping: time.sleep and time.monotonic are a fake clock that the
fake Radarr/Sonarr also advance (a timed-out call costs its timeout).

Run from tentacle/:  python -m unittest discover -s tests -p "test_bad_copy_delete_timeout.py"
Env: BAD_COPY_PROPERTY_SEEDS (default 1000), BAD_COPY_PROPERTY_FIRST_SEED (default 1).
"""
import json
import logging
import os
import random
import unittest
from collections import Counter
from unittest import mock

import requests

import models.database as mdb
from services import bad_copy
from test_arr_add_verify_budget import PLUGIN_ADD_TIMEOUT
from tmp_dirs import temp_dir

MOVIE_ID, MOVIE_FILE, EP_ID, EP_FILE = 11, 501, 71, 901


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class Clock:
    def __init__(self):
        self.start = self.now = 1000.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)
        self.slept += max(0.0, seconds)


class _Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status
        self.text = "" if data is None else json.dumps(data)

    def json(self):
        return self._data


class SlowArr:
    """Radarr and Sonarr on a clock. DELETE of the file takes `delete_takes` s
    on their side (None: it never completes, the file stays); the client gives
    up at its timeout, they don't. `delete` picks the answer:
      "answer"   - answered when done (a ReadTimeout if that is past the timeout)
      "refused"  - answered 500 at once, the file stays
      "lost"     - deleted, but the answer is a 500 / a reset connection
      "reset"    - the connection drops at once, the file stays
    `fail` maps a path to the answers for its next calls ("500", "timeout",
    "empty", or None: a normal answer); `injected` lists those used."""

    def __init__(self, clock, delete="answer", delete_takes=0.5):
        self.clock = clock
        self.delete, self.delete_takes = delete, delete_takes
        self.calls = []                 # (time, method, path, json)
        self.gone_at = None             # when the file went away
        self.seen_gone = False          # an answer the press got showed it gone
        self.fail = {}
        self.injected = []              # (path, fault) actually answered
        self.movie = {"id": MOVIE_ID, "tmdbId": 100, "title": "Dud Film", "hasFile": True, "monitored": True,
                      "movieFile": {"id": MOVIE_FILE}}
        self.movie_history = [
            {"id": 2, "eventType": "grabbed", "date": "2026-02-01T00:00:00Z",
             "sourceTitle": "Dud.Film.GERMAN.1080p", "downloadId": "A1"},
            {"id": 3, "eventType": "downloadFolderImported", "date": "2026-02-01T01:00:00Z",
             "downloadId": "A1", "data": {"fileId": str(MOVIE_FILE)}},
        ]
        self.episode = {"id": EP_ID, "seasonNumber": 1, "episodeNumber": 2, "hasFile": True,
                        "episodeFileId": EP_FILE, "monitored": True}
        self.ep_history = {"records": [
            {"id": 9, "eventType": "grabbed", "date": "2026-03-01T00:00:00Z",
             "sourceTitle": "Show.S01E02.HC.SUBS", "downloadId": "B2"},
            {"id": 10, "eventType": "downloadFolderImported", "date": "2026-03-01T01:00:00Z",
             "downloadId": "B2", "data": {"fileId": str(EP_FILE)}},
        ]}

    # -- state ---------------------------------------------------------
    def _gone(self):
        return self.gone_at is not None and self.clock.now >= self.gone_at

    def _sync(self):
        if self._gone():
            self.movie.update(hasFile=False, movieFile=None)
            self.episode.update(hasFile=False, episodeFileId=0)

    def file_there(self):
        self._sync()
        return not self._gone()

    # -- the API -------------------------------------------------------
    def __call__(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = url.split("/api/v3/")[1]
        self._sync()
        self.calls.append((self.clock.now, method, path, json))
        if method == "DELETE" and path in (f"moviefile/{MOVIE_FILE}", f"episodefile/{EP_FILE}"):
            return self._delete(timeout)
        planned = self.fail.get(path) or []
        what = planned.pop(0) if planned else None
        if what:
            self.injected.append((path, what))
            if what == "timeout":
                self.clock.now += timeout
                self._sync()
                raise requests.ReadTimeout(f"Read timed out. (read timeout={timeout})")
            if what == "500":
                self.clock.now += 0.05
                return _Resp({"message": "nope"}, 500)
            if what == "empty":
                self.clock.now += 0.05
                return _Resp(None)
        self.clock.now += 0.05
        self._sync()
        if method == "GET" and path == "movie":
            return _Resp([self.movie] if params.get("tmdbId") == self.movie["tmdbId"] else [])
        if method == "GET" and path == f"movie/{MOVIE_ID}":
            self.seen_gone |= not self.movie["hasFile"]
            return _Resp(dict(self.movie))
        if method == "GET" and path == "history/movie":
            return _Resp(self.movie_history)
        if method == "GET" and path == "series":
            return _Resp([{"id": 21, "tmdbId": 200, "title": "Show"}])
        if method == "GET" and path == "episode":
            return _Resp([dict(self.episode)])
        if method == "GET" and path == f"episode/{EP_ID}":
            self.seen_gone |= not self.episode["hasFile"]
            return _Resp(dict(self.episode))
        if method == "GET" and path == "history":
            return _Resp(self.ep_history)
        return _Resp(None)

    def _delete(self, timeout):
        sent = self.clock.now
        if self.delete == "refused":
            self.clock.now += 0.05
            return _Resp({"message": "Unable to delete movie file"}, 500)
        if self.delete == "reset":
            self.clock.now += 0.05
            raise requests.ConnectionError("Connection aborted.")
        takes = self.delete_takes
        if takes is not None:
            self.gone_at = sent + takes
        if self.delete == "lost":
            self.clock.now += min(takes, timeout)
            self._sync()
            if takes < timeout and self.gone_at is not None:
                raise requests.ConnectionError("Connection aborted.")
            raise requests.ReadTimeout(f"Read timed out. (read timeout={timeout})")
        if takes is not None and takes < timeout:
            self.clock.now += takes
            self._sync()
            self.seen_gone = True
            return _Resp(None)
        self.clock.now += timeout
        self._sync()
        raise requests.ReadTimeout(f"HTTPConnectionPool(host='arr', port=7878): Read timed out. "
                                   f"(read timeout={timeout})")

    # -- what happened -------------------------------------------------
    def writes(self):
        return [(t, m, p, j) for t, m, p, j in self.calls if m != "GET"]

    def searches(self):
        return [(t, j) for t, m, p, j in self.writes() if m == "POST" and p == "command"]


class _Base(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in {"radarr_url": "http://r", "radarr_api_key": "k",
                     "sonarr_url": "http://s", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        self.clock = Clock()
        self.arr = SlowArr(self.clock)
        for p in (mock.patch.object(bad_copy.requests, "request", side_effect=lambda *a, **kw: self.arr(*a, **kw)),
                  mock.patch("time.sleep", self.clock.sleep),
                  mock.patch("time.monotonic", self.clock.monotonic)):
            p.start()
            self.addCleanup(p.stop)

    def press(self, media="movie"):
        """Press the button: (result, error)."""
        start = self.clock.now
        try:
            if media == "movie":
                r = bad_copy.replace_movie(self.db, 100, user_name="Lucas")
            else:
                r = bad_copy.replace_episode(self.db, 200, 1, 2, user_name="Lucas")
            e = None
        except bad_copy.BadCopyError as err:
            r, e = None, err
        self.elapsed = self.clock.now - start
        return r, e

    def log_rows(self):
        return self.db.query(mdb.DeletionLog).filter(mdb.DeletionLog.kind == "bad-copy").count()


class TestMovie(_Base):
    def test_a_delete_that_outlasts_the_timeout_still_ends_in_a_search(self):
        self.arr.delete_takes = 90             # the recycle bin copy takes 90 s
        r, e = self.press()
        self.assertIsNone(e, e and str(e))
        self.assertTrue(r["ok"] and r["blocklisted"])
        self.assertFalse(self.arr.file_there())
        self.assertEqual([{"name": "MoviesSearch", "movieIds": [MOVIE_ID]}], [j for _, j in self.arr.searches()])
        self.assertGreaterEqual(self.arr.searches()[0][0], self.arr.gone_at, "searched while the file was there")
        self.assertEqual(1, self.log_rows())
        self.assertLess(self.elapsed, PLUGIN_ADD_TIMEOUT)

    def test_an_unmonitored_movie_is_monitored_once_the_file_is_gone(self):
        self.arr.movie["monitored"] = False
        self.arr.delete_takes = 60
        r, e = self.press()
        self.assertIsNone(e)
        put = [(t, j) for t, m, p, j in self.arr.writes() if p == "movie/editor"]
        self.assertEqual([{"movieIds": [MOVIE_ID], "monitored": True}], [j for _, j in put])
        self.assertGreaterEqual(put[0][0], self.arr.gone_at)

    def test_a_delete_still_running_when_the_wait_ends(self):
        self.arr.delete_takes = 1000
        r, e = self.press()
        self.assertIsNotNone(e)
        self.assertIn("hasn't finished deleting", str(e))
        self.assertNotIn("7878", str(e), "no address in what users see")
        self.assertEqual([], self.arr.searches())
        self.assertEqual(0, self.log_rows(), "nothing is logged while the file is there")
        probes = [c for c in self.arr.calls if c[2] == f"movie/{MOVIE_ID}"]
        self.assertGreaterEqual(len(probes), 10, "the wait polls, it doesn't sample once")
        self.assertLess(self.elapsed, PLUGIN_ADD_TIMEOUT)

    def test_a_delete_that_never_completes_leaves_the_file_and_says_so(self):
        self.arr.delete_takes = None
        r, e = self.press()
        self.assertIn("hasn't finished deleting", str(e))
        self.assertTrue(self.arr.file_there())
        self.assertEqual(0, self.log_rows())
        self.assertEqual([], self.arr.searches())

    def test_a_refused_delete_is_answered_at_once(self):
        self.arr.delete = "refused"
        r, e = self.press()
        self.assertEqual("Radarr refused (500) on moviefile/501", str(e))
        self.assertEqual(0, self.clock.slept, "a refusal is not waited on")
        self.assertEqual(1, len([c for c in self.arr.calls if c[2] == f"movie/{MOVIE_ID}"]))
        self.assertTrue(self.arr.file_there())
        self.assertEqual([], self.arr.searches())
        self.assertEqual(0, self.log_rows())

    def test_a_refused_delete_with_no_answer_about_the_file_stands(self):
        self.arr.delete = "refused"
        self.arr.fail[f"movie/{MOVIE_ID}"] = ["empty"]
        r, e = self.press()
        self.assertEqual("Radarr refused (500) on moviefile/501", str(e))
        self.assertEqual(0, self.log_rows())

    def test_a_delete_whose_answer_was_lost_carries_on(self):
        self.arr.delete, self.arr.delete_takes = "lost", 2
        r, e = self.press()
        self.assertIsNone(e)
        self.assertEqual(1, len(self.arr.searches()))
        self.assertEqual(1, self.log_rows())

    def test_failed_checks_during_the_wait_are_retried(self):
        self.arr.delete_takes = 70
        self.arr.fail[f"movie/{MOVIE_ID}"] = ["500", "timeout", "empty", "500"]
        r, e = self.press()
        self.assertIsNone(e, e and str(e))
        self.assertEqual(1, len(self.arr.searches()))

    def test_a_search_refused_after_the_delete_is_logged_and_said(self):
        self.arr.fail["command"] = ["500"]
        r, e = self.press()
        self.assertIn("The file of Dud Film is deleted, but the search for another copy didn't start", str(e))
        self.assertEqual(502, e.status)
        self.assertEqual(1, self.log_rows(), "the deleted file is in the Deletion log")

    def test_a_search_that_times_out_after_the_delete_quotes_the_error_as_given(self):
        # The text quotes _Arr.call's error word for word, so it shows what that
        # error shows about Radarr (an address or not), and nothing more.
        self.arr.fail["command"] = ["timeout"]
        raised, call = [], bad_copy._Arr.call

        def spy(arr, method, path, **kw):
            try:
                return call(arr, method, path, **kw)
            except bad_copy.BadCopyError as err:
                raised.append((path, str(err)))
                raise
        with mock.patch.object(bad_copy._Arr, "call", spy):
            r, e = self.press()
        cause = [err for path, err in raised if path == "command"]
        self.assertEqual(1, len(cause), raised)
        self.assertEqual(f"The file of Dud Film is deleted, but the search for another copy didn't start "
                         f"({cause[0]}). Use Search again.", str(e))
        self.assertEqual(502, e.status)
        self.assertEqual(1, self.log_rows())

    def test_the_delete_is_sent_once(self):
        self.arr.delete_takes = 1000
        self.press()
        self.assertEqual(1, len([w for w in self.arr.writes() if w[1] == "DELETE"]))


class TestEpisode(_Base):
    def test_a_delete_that_outlasts_the_timeout_still_ends_in_a_search(self):
        self.arr.delete_takes = 100
        r, e = self.press("series")
        self.assertIsNone(e, e and str(e))
        after = [(m, p, j) for t, m, p, j in self.arr.writes() if t >= self.arr.gone_at]
        self.assertEqual([("PUT", "episode/monitor", {"episodeIds": [EP_ID], "monitored": True}),
                          ("POST", "command", {"name": "EpisodeSearch", "episodeIds": [EP_ID]})], after)
        self.assertEqual(1, self.log_rows())
        self.assertLess(self.elapsed, PLUGIN_ADD_TIMEOUT)

    def test_a_monitor_refused_after_the_delete_is_logged_and_said(self):
        self.arr.fail["episode/monitor"] = ["500"]
        r, e = self.press("series")
        self.assertIn("The file of Show S01E02 is deleted", str(e))
        self.assertEqual(1, self.log_rows())

    def test_a_refused_delete_is_answered_at_once(self):
        self.arr.delete = "refused"
        r, e = self.press("series")
        self.assertEqual("Sonarr refused (500) on episodefile/901", str(e))
        self.assertEqual(0, self.clock.slept)
        self.assertEqual(0, self.log_rows())


class TestRoute(_Base):
    def test_the_route_answers_ok_after_a_slow_delete_and_a_retry_is_not_needed(self):
        from routers.library import ReplaceCopyBody, replace_copy
        admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.db.add(admin)
        self.db.commit()
        self.arr.delete_takes = 120
        r = replace_copy("movie", 100, ReplaceCopyBody(), db=self.db, user=admin)
        self.assertTrue(r["ok"])
        self.assertEqual(1, len(self.arr.searches()))


class TestFaultInjection(_Base):
    """Random faults at every Radarr/Sonarr call; the invariants after each press."""

    def _run(self, seed):
        rng = random.Random(seed)
        self.db.query(mdb.DeletionLog).delete()
        self.db.commit()
        self.clock.slept = 0.0
        media = rng.choice(["movie", "series"])
        arr = self.arr = SlowArr(self.clock, delete=rng.choice(["answer", "answer", "refused", "lost", "reset"]),
                                 delete_takes=rng.choice([None, rng.uniform(0.1, 29), rng.uniform(30, 400)]))
        if arr.delete == "lost" and arr.delete_takes is None:
            arr.delete_takes = rng.uniform(0.1, 60)
        arr.movie["monitored"] = rng.random() < 0.7
        if rng.random() < 0.2:
            arr.movie_history, arr.ep_history = [], {"records": []}
        faults = ["500", "timeout", "empty"]
        probe = f"movie/{MOVIE_ID}" if media == "movie" else f"episode/{EP_ID}"
        p_probe = rng.choice([0, 0.2, 0.5])
        arr.fail[probe] = [rng.choice(faults) if rng.random() < p_probe else None for _ in range(60)]
        for path in ("command", "movie/editor", "episode/monitor", "history/failed/2", "history/failed/9"):
            if rng.random() < 0.15:
                arr.fail[path] = [rng.choice(faults[:2])]
        start = self.clock.now
        r, e = self.press(media)
        return media, arr, r, e, start, probe

    def _check(self, seed, media, arr, r, e, start, probe):
        where = f"seed {seed}: {media}, delete={arr.delete} takes={arr.delete_takes}, result={r}, error={e}"
        writes = arr.writes()
        deletes = [w for w in writes if w[1] == "DELETE"]
        self.assertEqual(1, len(deletes), where)
        t_delete = deletes[0][0]
        post = [w for w in writes if w[2] in ("command", "movie/editor", "episode/monitor")]
        # I4: monitor/search only after the delete, and only once the file is gone
        for t, m, p, j in post:
            self.assertGreater(t, t_delete, where)
            self.assertIsNotNone(arr.gone_at, where)
            self.assertGreaterEqual(t, arr.gone_at, f"{p} sent while the file was there; {where}")
        rows = self.log_rows()
        # I2: one row exactly when the press saw the file go
        self.assertEqual(1 if arr.seen_gone else 0, rows, where)
        if rows:
            self.assertFalse(arr.file_there(), where)
        if r is not None:
            # I3: ok only when the file is gone, logged, and the search went out
            self.assertTrue(r["ok"], where)
            self.assertFalse(arr.file_there(), where)
            self.assertEqual(1, rows, where)
            self.assertEqual(1, len(arr.searches()), where)
        else:
            msg = str(e)
            if arr.seen_gone:
                # I1: the file is gone: the answer says so
                self.assertIn("is deleted, but the search for another copy didn't start", msg, where)
            else:
                self.assertEqual([], post, where)
                self.assertTrue("hasn't finished deleting" in msg or "refused" in msg
                                or "Couldn't reach" in msg, where)
        # I1 (the bug): a delete that ends inside the wait, with checks answering and the
        # search accepted, ends in a search
        clean = not [p for p, _ in arr.injected if p in (probe, "command", "movie/editor", "episode/monitor")]
        if clean and arr.gone_at is not None and arr.gone_at - t_delete <= bad_copy.DELETE_WAIT - 1 \
                and arr.delete != "refused":
            self.assertIsNotNone(r, where)
        # I5: a refusal with the file still there is not waited on
        if arr.delete in ("refused", "reset"):
            self.assertEqual(0, self.clock.slept, where)
        # I6: answered before the plugin gives up
        self.assertLess(self.clock.now - start, PLUGIN_ADD_TIMEOUT, where)
        if r is not None:
            return "ok after a late delete" if arr.gone_at - t_delete >= 30 else "ok"
        return ("deleted, search not started" if arr.seen_gone else
                "still deleting" if "hasn't finished" in str(e) else "refused, file kept")

    def test_random_faults(self):
        first = int(os.environ.get("BAD_COPY_PROPERTY_FIRST_SEED", "1"))
        count = int(os.environ.get("BAD_COPY_PROPERTY_SEEDS", "1000"))
        failures, outcomes = [], Counter()
        for seed in range(first, first + count):
            try:
                outcomes[self._check(seed, *self._run(seed))] += 1
            except AssertionError as err:
                failures.append(f"{err}")
        self.assertEqual([], failures[:5], f"{len(failures)} of {count} seeds broke an invariant")
        if count >= 500:   # every path is exercised, so the test can't quietly stop testing one
            for outcome in ("ok", "ok after a late delete", "deleted, search not started",
                            "still deleting", "refused, file kept"):
                self.assertGreater(outcomes[outcome], count // 100, f"{outcome}: {dict(outcomes)}")


if __name__ == "__main__":
    unittest.main()
