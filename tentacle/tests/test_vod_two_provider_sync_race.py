"""Two providers' VOD syncs at once no longer both import the same new title.

The sync routes (routers/sync.py) and the nightly loop (main.py) refuse a
second sync of the same provider only, so provider 2's sync could run while
provider 1's did: "Sync" for provider 2 on the VOD page during provider 1's
manual or nightly sync. A category commits its new rows once, at its end, so
neither sync saw the other's new film: both wrote 'New Film (2026)/New Film
(2026).strm' (the last write won) and both added the row. The second commit
failed on UNIQUE(tmdb_id): that run ended "failed", the rest of the category
was rolled back with its files left on disk, the rest of the run was skipped,
and the row that was kept played the other provider's stream. Now one
provider's sync runs at a time: the other waits (on screen, cancellable, not
counted as stuck) and then finds the first one's rows, as one after the other.

The pinned tests hold provider 1 after it imported the shared title until
provider 2 has imported it too (before) or is waiting for provider 1 (now),
standing in for the time the rest of a large category takes. The property
test starts two or three providers with overlapping catalogues at once, with
small random delays, and checks the library against invariants:
  T1  every run completes
  T2  every listed film / series has exactly one row
  T3  each row's .strm plays a stream of the provider that owns the row
  T4  every .strm on disk belongs to a row (nothing left behind)
TENTACLE_FUZZ_SEEDS (default 12) and TENTACLE_FUZZ_FIRST (default 0) pick its
seeds; the scratch run for the PR used 1,000. A real SQLite file with the
production engine settings (WAL, busy_timeout, autoflush off). No network.
"""
import os
import random
import shutil
import threading
import time
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, ProviderCategory, Series, SyncRun
import services.sync as sync
from services.provider_activity import protected_wait_state
from tmp_dirs import temp_dir


def _meta(tid, title, year):
    return {"tmdb_id": tid, "title": title, "year": year, "overview": "", "genres": [],
            "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}


class FakeTMDB:
    titles = {}     # title -> tmdb id (films and series alike)

    def __init__(self, *a, **k):
        pass

    def search_movie(self, name, year=None, **k):
        tid = FakeTMDB.titles.get(name)
        return _meta(tid, name, year or "2026") if tid else None

    search_series = search_movie

    def get_movie_details(self, tid, **k):
        for name, t in FakeTMDB.titles.items():
            if t == tid:
                return _meta(tid, name, "2026")
        return None

    get_series_details = get_movie_details

    def cleanup_cache(self):
        pass


class FakeClient:
    """One Xtream account: {cat_id: [(title, stream id)]} for films and
    series, on its own host and username (how a sync tells another
    provider's stream from its own)."""

    def __init__(self, n, movies=None, series=None, delay=None):
        self.host, self.user = f"p{n}", f"u{n}"
        self.movies, self.series = movies or {}, series or {}
        self.delay = delay      # () -> seconds to sleep before answering a list

    def _wait(self):
        if self.delay:
            time.sleep(self.delay())

    def get_vod_streams(self, cat):
        self._wait()
        return [{"name": f"{t} (2026)", "stream_id": sid, "container_extension": "mp4"}
                for t, sid in self.movies.get(cat, [])]

    def movie_stream_url(self, sid, ext):
        return f"http://{self.host}/movie/{self.user}/p/{sid}.{ext}"

    def get_series_list(self, cat):
        self._wait()
        return [{"name": f"{t} (2026)", "series_id": sid} for t, sid in self.series.get(cat, [])]

    def get_series_info(self, sid):
        return {"episodes": {"1": [{"id": sid * 10 + 1, "episode_num": 1, "container_extension": "mp4"}]}}

    def episode_stream_url(self, e, ext):
        return f"http://{self.host}/series/{self.user}/p/{e}.{ext}"


class _World(unittest.TestCase):
    """A real SQLite file as models/database.py configures it, a temp
    /media/vod, and providers whose catalogues the test sets."""

    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.engine = create_engine(f"sqlite:///{tmp}/t.db",
                                    connect_args={"check_same_thread": False, "timeout": 30})
        event.listen(self.engine, "connect", mdb._set_sqlite_pragma)    # WAL + busy_timeout
        self.addCleanup(self.engine.dispose)
        mdb.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        self.vod = _RealPath(tmp) / "vod"
        (self.vod / "movies").mkdir(parents=True)
        (self.vod / "shows").mkdir(parents=True)
        vod = self.vod

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)

        for k in ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT",
                  "VOD_SYNC_LOCK_POLL_SECONDS"):
            self.addCleanup(setattr, sync, k, getattr(sync, k, None))
        sync.Path = mapped
        sync.TMDBService = FakeTMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        sync.VOD_SYNC_LOCK_POLL_SECONDS = 0.05
        FakeTMDB.titles = {}
        self.clients = {}
        self.hosts = {}
        sync.make_provider_client = lambda p: self.clients[p.id]

    def add_provider(self, n, client, movie_cats=(), series_cats=()):
        db = self.Session()
        try:
            p = Provider(name=f"P{n}", server_url=f"http://{client.host}", username=client.user, password="p",
                         active=True, priority=n, require_tmdb_match=True)
            db.add(p)
            db.commit()
            for kind, cats in (("movie", movie_cats), ("series", series_cats)):
                for cat in cats:
                    db.add(ProviderCategory(provider_id=p.id, category_id=cat, category_name=cat, type=kind,
                                            whitelisted=True, source_tag=f"P{n}"))
            db.commit()
            self.clients[p.id] = client
            self.hosts[p.id] = client.host
            return p.id
        finally:
            db.close()

    def sync(self, pid, progress=None, cancel_check=None):
        db = self.Session()
        try:
            run = sync.sync_provider(db.get(Provider, pid), "full", db, progress_callback=progress,
                                     cancel_check=cancel_check)
            return run.status, run.error_message
        finally:
            db.close()

    def sync_in_thread(self, pid, results, progress=None, cancel_check=None, done=None):
        def run():
            try:
                results[pid] = self.sync(pid, progress, cancel_check)
            except Exception as e:  # pragma: no cover - reported by the assertions
                results[pid] = ("raised", repr(e))
            finally:
                if done is not None:
                    done.set()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def problems(self, results, listed):
        """T1-T4. `listed`: {"movie"|"series": {title: {provider ids listing it}}}."""
        out = []
        for pid, status in sorted(results.items()):
            if status[0] != "completed":
                out.append(f"T1 P{pid}'s sync: {status}")
        db = self.Session()
        try:
            owned = set()
            for kind, Model in (("movie", Movie), ("series", Series)):
                for title, listers in sorted(listed.get(kind, {}).items()):
                    rows = db.query(Model).filter(Model.tmdb_id == FakeTMDB.titles[title]).all()
                    if len(rows) != 1:
                        out.append(f"T2 {kind} {title!r}: {len(rows)} rows")
                        continue
                    row = rows[0]
                    if row.provider_id not in listers:
                        out.append(f"T2 {kind} {title!r} belongs to P{row.provider_id}, listed by {sorted(listers)}")
                    path = _RealPath(row.strm_path)     # a series' is its folder
                    owned.add(path if kind == "movie" else path.resolve())
                    strms = [path] if kind == "movie" else sorted(path.rglob("*.strm"))
                    for strm in strms:
                        plays = strm.read_text().strip() if strm.exists() else "(no file)"
                        if not plays.startswith(f"http://{self.hosts.get(row.provider_id)}/"):
                            out.append(f"T3 {kind} {title!r} (P{row.provider_id}'s row) plays {plays}")
            for strm in sorted((self.vod / "movies").rglob("*.strm")):
                if strm not in owned:
                    out.append(f"T4 {strm.relative_to(self.vod)} has no row")
            for strm in sorted((self.vod / "shows").rglob("*.strm")):
                if not any(o in strm.resolve().parents for o in owned):
                    out.append(f"T4 {strm.relative_to(self.vod)} has no row")
        finally:
            db.close()
        return out


class TwoProvidersAtOnce(_World):
    """The same new title from two providers, provider 1 held mid-category."""

    def _two(self, kind):
        FakeTMDB.titles.update({"New Title": 5001, "Only One": 5002, "Only Two": 5003})
        lists = {"c1": [("New Title", 11), ("Only One", 12)], "c2": [("New Title", 21), ("Only Two", 22)]}
        if kind == "movie":
            self.one = self.add_provider(1, FakeClient(1, movies={"c1": lists["c1"]}), movie_cats=["c1"])
            self.two = self.add_provider(2, FakeClient(2, movies={"c2": lists["c2"]}), movie_cats=["c2"])
        else:
            self.one = self.add_provider(1, FakeClient(1, series={"c1": lists["c1"]}), series_cats=["c1"])
            self.two = self.add_provider(2, FakeClient(2, series={"c2": lists["c2"]}), series_cats=["c2"])
        return {kind: {"New Title": {self.one, self.two}, "Only One": {self.one}, "Only Two": {self.two}}}

    def _at_once(self, listed):
        one_imported = threading.Event()   # provider 1 imported New Title; its category is not committed
        two_caught_up = threading.Event()  # provider 2 imported it too (before), or waits for provider 1 (now)
        one_done = threading.Event()
        waits = []
        results = {}

        def hold_one(phase, category, stats, item_title=None, **kw):
            if item_title == "Only One":
                one_imported.set()
                two_caught_up.wait(10)

        def hold_two(phase, category, stats, item_title=None, **kw):
            if item_title and item_title.startswith("Waiting for"):
                db = self.Session()
                try:
                    run = db.query(SyncRun).filter_by(provider_id=self.two, status="running").one()
                    waits.append((item_title, protected_wait_state(run.id)[0]))
                finally:
                    db.close()
                two_caught_up.set()
            if item_title == "Only Two":
                two_caught_up.set()
                one_done.wait(10)

        t1 = self.sync_in_thread(self.one, results, hold_one, done=one_done)
        self.assertTrue(one_imported.wait(20), "provider 1 never reached its second title")
        t2 = self.sync_in_thread(self.two, results, hold_two)
        t1.join(60)
        t2.join(60)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "a sync hung")
        self.assertEqual([], self.problems(results, listed))
        # Provider 2 said why it was not moving, and its run was not taken for a stuck one
        self.assertEqual([("Waiting for P1's sync to finish", True)], waits)

    def test_one_after_the_other(self):
        listed = self._two("movie")
        results = {self.one: self.sync(self.one), self.two: self.sync(self.two)}
        self.assertEqual([], self.problems(results, listed))

    def test_films(self):
        self._at_once(self._two("movie"))

    def test_series(self):
        self._at_once(self._two("series"))

    def test_a_waiting_sync_can_be_cancelled(self):
        listed = self._two("movie")
        one_holding = threading.Event()    # provider 1 is mid-category, so it holds the sync lock
        release_one = threading.Event()
        two_waiting = threading.Event()
        results = {}

        def hold_one(phase, category, stats, item_title=None, **kw):
            if item_title == "Only One":
                one_holding.set()
                release_one.wait(10)

        def watch_two(phase, category, stats, item_title=None, **kw):
            if item_title and item_title.startswith("Waiting for"):
                two_waiting.set()

        t1 = self.sync_in_thread(self.one, results, hold_one)
        self.addCleanup(t1.join, 30)
        self.addCleanup(release_one.set)   # cleanups run last first: release provider 1, then wait for it
        # Provider 2 starts only once provider 1 holds the lock: started together,
        # provider 2 could take the lock first and then has nothing to wait for
        self.assertTrue(one_holding.wait(20), "provider 1 never reached its second title")
        t2 = self.sync_in_thread(self.two, results, watch_two, cancel_check=two_waiting.is_set)
        t2.join(10)
        self.assertFalse(t2.is_alive(), "the cancelled sync is still waiting")
        self.assertEqual("cancelled", results[self.two][0])
        release_one.set()
        t1.join(30)
        self.assertEqual("completed", results[self.one][0])
        # Nothing of provider 2's was written while it waited; its next sync runs
        self.assertFalse((self.vod / "movies" / "Only Two (2026)").exists())
        results[self.two] = self.sync(self.two)
        self.assertEqual([], self.problems(results, listed))

    def test_a_failed_sync_lets_the_next_one_run(self):
        listed = self._two("movie")
        real = sync.make_provider_client

        def unreachable(p):
            if p.id == self.one:
                raise sync.ProviderConnectionError("provider unreachable")
            return real(p)
        sync.make_provider_client = unreachable
        self.assertEqual("failed", self.sync(self.one)[0])
        results = {}
        self.sync_in_thread(self.two, results).join(20)
        self.assertEqual("completed", results.get(self.two, ("hung",))[0])
        sync.make_provider_client = real
        results[self.one] = self.sync(self.one)
        self.assertEqual([], self.problems(results, listed))


def _seeds():
    n = int(os.environ.get("TENTACLE_FUZZ_SEEDS", "12"))
    first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
    return range(first, first + n)


class ProvidersAtOnceProperty(_World):
    """Two or three providers, overlapping catalogues, all started at once."""

    def _world(self, rng):
        films = [f"Film {i}" for i in range(rng.randint(3, 8))]
        shows = [f"Show {i}" for i in range(rng.randint(0, 4))]
        for i, t in enumerate(films + shows):
            FakeTMDB.titles[t] = 7000 + i
        listed = {"movie": {}, "series": {}}
        sid = 100
        for n in range(1, rng.randint(2, 3) + 1):
            movies, series = {}, {}
            for c in range(rng.randint(1, 2)):
                movies[f"m{n}{c}"] = []
                for t in rng.sample(films, rng.randint(1, len(films))):
                    sid += 1
                    movies[f"m{n}{c}"].append((t, sid))
            if shows and rng.random() < 0.7:
                series[f"s{n}"] = []
                for t in rng.sample(shows, rng.randint(1, len(shows))):
                    sid += 1
                    series[f"s{n}"].append((t, sid))
            delay_rng = random.Random(rng.random())
            client = FakeClient(n, movies, series, delay=lambda r=delay_rng: r.uniform(0, 0.004))
            pid = self.add_provider(n, client, movie_cats=list(movies), series_cats=list(series))
            for cats, kind in ((movies, "movie"), (series, "series")):
                for entries in cats.values():
                    for t, _ in entries:
                        listed[kind].setdefault(t, set()).add(pid)
        return listed

    def test_random_catalogues_synced_at_once(self):
        failures = []
        for seed in _seeds():
            with self.subTest(seed=seed):
                self.doCleanups()       # each seed in a fresh world
                self.setUp()
                rng = random.Random(seed)
                listed = self._world(rng)
                jitter = random.Random(seed + 1)
                lock = threading.Lock()

                def progress(phase, category, stats, item_title=None, **kw):
                    with lock:
                        d = jitter.uniform(0, 0.003)
                    time.sleep(d)

                results = {}
                go = threading.Barrier(len(self.clients))
                threads = []
                for pid in list(self.clients):
                    def run(pid=pid):
                        go.wait(10)
                        try:
                            results[pid] = self.sync(pid, progress)
                        except Exception as e:  # pragma: no cover - reported below
                            results[pid] = ("raised", repr(e))
                    t = threading.Thread(target=run, daemon=True)
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join(60)
                self.assertFalse(any(t.is_alive() for t in threads), f"seed {seed}: a sync hung")
                problems = self.problems(results, listed)
                if problems:
                    failures.append((seed, problems[:3]))
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
