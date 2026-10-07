"""Two providers' VOD syncs at once (#446): the one that committed its
category second failed on UNIQUE(tmdb_id), its other new titles were left on
disk with no row, and the kept row could play the other provider's stream.
Syncs of different providers now run one at a time (services.sync
_vod_sync_lock): the second waits, says so, stays cancellable and is not
counted as stuck.

Real SQLite file with the production engine settings (WAL, busy_timeout,
autoflush off). Provider 1 is held after it imported the shared film, which
stands in for the rest of a large category.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p "test_vod_two_provider_sync_race.py"
"""
import logging
import shutil
import threading
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, ProviderCategory, SyncRun
import services.sync as sync
from services.provider_activity import protected_wait_state
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)

FILMS = {"New Film": (5001, "New Film", "2026"),
         "Only One": (5002, "Only One", "2026"),
         "Only Two": (5003, "Only Two", "2026")}


def _meta(tid, title, year):
    return {"tmdb_id": tid, "title": title, "year": year, "overview": "", "genres": [],
            "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}


class TMDB:
    enabled = True

    def __init__(self, *a, **k):
        pass

    def search_movie(self, name, year=None, **k):
        f = FILMS.get(name)
        return _meta(*f) if f else None

    def get_movie_details(self, tid, **k):
        for f in FILMS.values():
            if f[0] == tid:
                return _meta(*f)
        return None

    def search_series(self, *a, **k):
        return None

    def get_series_details(self, *a, **k):
        return None

    def cleanup_cache(self):
        pass


class Client:
    def __init__(self, host, user, movies):
        self.host, self.user, self.movies = host, user, movies

    def get_vod_streams(self, cat):
        return [dict(s) for s in self.movies.get(cat, [])]

    def movie_stream_url(self, sid, ext):
        return f"http://{self.host}/movie/{self.user}/p/{sid}.{ext}"

    def get_series_list(self, cat):
        return []

    def get_series_info(self, sid):
        return {"episodes": {}}

    def episode_stream_url(self, e, ext):
        return f"http://{self.host}/series/{self.user}/p/{e}.{ext}"


def stream(name, sid):
    return {"name": name, "stream_id": sid, "container_extension": "mp4"}


class TwoProvidersSyncingAtOnce(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False, "timeout": 30})
        event.listen(engine, "connect", mdb._set_sqlite_pragma)   # WAL + busy_timeout, as in production
        mdb.Base.metadata.create_all(engine)
        self.engine = engine
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.vod = _RealPath(tmp) / "vod"
        (self.vod / "movies").mkdir(parents=True)
        (self.vod / "shows").mkdir(parents=True)
        vod = self.vod

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)

        self._saved = {k: getattr(sync, k, None) for k in
                       ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT",
                        "VOD_SYNC_POLL_SECONDS")}
        sync.Path = mapped
        sync.VOD_SYNC_POLL_SECONDS = 0.05
        sync.TMDBService = TMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"

        db = self.Session()
        one = Provider(name="One", server_url="http://one", username="u1", password="p", active=True,
                       priority=1, require_tmdb_match=True)
        two = Provider(name="Two", server_url="http://two", username="u2", password="p", active=True,
                       priority=2, require_tmdb_match=True)
        db.add_all([one, two])
        db.commit()
        db.add(ProviderCategory(provider_id=one.id, category_id="c1", category_name="One films", type="movie",
                                whitelisted=True, source_tag="One"))
        db.add(ProviderCategory(provider_id=two.id, category_id="c2", category_name="Two films", type="movie",
                                whitelisted=True, source_tag="Two"))
        db.commit()
        self.one, self.two = one.id, two.id
        db.close()

        clients = {
            self.one: Client("one", "u1", {"c1": [stream("New Film (2026)", 11), stream("Only One (2026)", 12)]}),
            self.two: Client("two", "u2", {"c2": [stream("New Film (2026)", 21), stream("Only Two (2026)", 22)]}),
        }
        sync.make_provider_client = lambda p: clients[p.id]

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(sync, k, v)
        self.engine.dispose()

    def _sync(self, pid, progress=None, cancel_check=None):
        db = self.Session()
        try:
            run = sync.sync_provider(db.get(Provider, pid), "full", db, progress_callback=progress,
                                     cancel_check=cancel_check)
            return run.status, run.error_message
        finally:
            db.close()

    def _problems(self, results):
        problems = []
        for pid, label in ((self.one, "provider 1"), (self.two, "provider 2")):
            status = results.get(pid)
            if not status or status[0] != "completed":
                problems.append(f"{label}'s sync: {status}")
        db = self.Session()
        try:
            rows = db.query(Movie).filter_by(tmdb_id=5001).all()
            if len(rows) != 1 or rows[0].provider_id != self.one:
                problems.append(f"New Film rows: {[(r.provider_id, r.source) for r in rows]}")
            else:
                plays = _RealPath(rows[0].strm_path).read_text().strip()
                if not plays.startswith("http://one/"):
                    problems.append(f"provider 1's New Film row (priority 1, source {rows[0].source}) plays {plays}")
            if db.query(Movie).filter_by(tmdb_id=5003).count() != 1:
                d = self.vod / "movies" / "Only Two (2026)"
                left = sorted(str(p.relative_to(self.vod)) for p in d.glob("*")) if d.exists() else []
                problems.append(f"provider 2's other new film 'Only Two' has no row; files on disk: {left}")
        finally:
            db.close()
        return problems

    def test_control_one_after_the_other(self):
        results = {self.one: self._sync(self.one), self.two: self._sync(self.two)}
        self.assertEqual(self._problems(results), [])

    def _start(self, pid, cb, results, done=None, cancel_check=None):
        def run():
            try:
                results[pid] = self._sync(pid, cb, cancel_check)
            finally:
                if done is not None:
                    done.set()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def test_two_providers_syncing_at_once(self):
        one_imported, two_moved, one_done = threading.Event(), threading.Event(), threading.Event()
        results, two_waits = {}, []

        def hold_one(phase, category, stats, item_title=None, **kw):
            if item_title == "Only One":      # New Film imported, category not committed yet
                one_imported.set()
                two_moved.wait(10)            # provider 2 imported New Film too, or waits for its turn

        def hold_two(phase, category, stats, item_title=None, **kw):
            if item_title and item_title.startswith("Waiting for"):
                two_waits.append(item_title)
                two_moved.set()
            if item_title == "Only Two":      # New Film imported by provider 2 too
                two_moved.set()
                one_done.wait(10)

        t1 = self._start(self.one, hold_one, results, one_done)
        self.assertTrue(one_imported.wait(20))
        t2 = self._start(self.two, hold_two, results)
        t1.join(60)
        t2.join(60)
        self.assertEqual(self._problems(results), [])
        self.assertEqual(two_waits, ["Waiting for One's sync to finish"])

    def test_a_waiting_sync_is_cancellable_and_not_stuck(self):
        one_imported, release_one, two_waiting = threading.Event(), threading.Event(), threading.Event()
        cancel_two = threading.Event()
        results = {}

        def hold_one(phase, category, stats, item_title=None, **kw):
            if item_title == "Only One":
                one_imported.set()
                release_one.wait(10)

        def hold_two(phase, category, stats, item_title=None, **kw):
            if item_title and item_title.startswith("Waiting for"):
                two_waiting.set()

        t1 = self._start(self.one, hold_one, results)
        self.assertTrue(one_imported.wait(20))
        t2 = self._start(self.two, hold_two, results, cancel_check=cancel_two.is_set)
        self.assertTrue(two_waiting.wait(20))

        db = self.Session()
        try:
            run2 = db.query(SyncRun).filter_by(provider_id=self.two).one()
            self.assertEqual(run2.status, "running")      # visible on the dashboard, so it can be cancelled
        finally:
            db.close()
        # The status route does not auto-fail a run that is waiting (routers.sync).
        self.assertTrue(protected_wait_state(run2.id)[0])

        cancel_two.set()
        t2.join(20)
        self.assertEqual(results.get(self.two, ("",))[0], "cancelled")
        self.assertFalse(protected_wait_state(run2.id)[0])
        release_one.set()
        t1.join(60)
        self.assertEqual(results[self.one], ("completed", None))

        # Provider 1's turn was given back: the next sync runs at once.
        results.clear()
        waits = []
        self._sync(self.two, lambda *a, item_title=None, **kw: waits.append(item_title))
        self.assertFalse([w for w in waits if w and w.startswith("Waiting for")])

    def test_a_failed_sync_gives_its_turn_back(self):
        def broken(p):
            raise RuntimeError("provider client exploded")
        clients = sync.make_provider_client
        sync.make_provider_client = broken
        self.assertEqual(self._sync(self.one)[0], "failed")
        sync.make_provider_client = clients
        self.assertFalse(sync._vod_sync_lock.locked())
        self.assertEqual(self._sync(self.two)[0], "completed")


if __name__ == "__main__":
    unittest.main()
