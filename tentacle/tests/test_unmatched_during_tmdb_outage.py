"""Providers with "Require TMDB match" switched off (#377).

A TMDB lookup that FAILED (outage, 429/5xx, timeout) was treated like "TMDB has
no such title": the title was imported under a provider-only (negative) id with
no metadata, and because the next sync found that (title, year) in its
known-titles map it never asked TMDB again -- the title stayed unmatched for good.
Now such a title is left for the next sync (as with the setting on) for up to
LOOKUP_RETRY_DAYS from its first failed lookup, then imported without a match
as before. A real "no match" is imported at once, as before.

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_unmatched_during_tmdb_outage.py"
No network.
"""
import json
import logging
import random
import unittest
from datetime import datetime, timedelta
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import MatchOverride, Movie, Provider, ProviderCategory, Series
import services.sync as sync
from tmp_dirs import temp_dir

KNOWN_MOVIES = {"heat": 949, "alien": 348, "fargo": 275}
KNOWN_SHOWS = {"dark": 70523, "lost": 4607}


class TMDB:
    down = False            # every lookup fails
    down_for = set()        # lookups of these names fail
    calls = []

    def __init__(self, *a, **k):
        pass

    def _check(self, name):
        TMDB.calls.append(name.lower())
        if TMDB.down or name.lower() in TMDB.down_for:
            raise RuntimeError("TMDB 503")

    def search_movie(self, name, year=None, **k):
        self._check(name)
        tid = KNOWN_MOVIES.get(name.lower())
        if tid:
            return {"tmdb_id": tid, "title": name.title(), "year": year, "overview": "o", "runtime": 100,
                    "rating": 7.0, "genres": [], "poster_path": None, "backdrop_path": None}
        return None

    def get_movie_details(self, tid, *a, **k):
        for name, t in KNOWN_MOVIES.items():
            if t == tid:
                return {"tmdb_id": t, "title": name.title(), "year": "1995", "overview": "o", "runtime": 100,
                        "rating": 7.0, "genres": [], "poster_path": None, "backdrop_path": None}
        return None

    def search_series(self, name, year=None, **k):
        self._check(name)
        tid = KNOWN_SHOWS.get(name.lower())
        if tid:
            return {"tmdb_id": tid, "title": name.title(), "year": year, "overview": "o", "genres": [],
                    "poster_path": None, "backdrop_path": None, "status": "Ended", "rating": None}
        return None

    def get_series_details(self, *a, **k):
        return None

    def cleanup_cache(self):
        pass


class XClient(sync.XtreamClient):
    def __init__(self, provider):
        super().__init__(provider)
        self.movies, self.series, self.info = {}, {}, {}

    def get_vod_streams(self, cat):
        return [dict(s) for s in self.movies.get(cat, [])]

    def get_series_list(self, cat):
        return [dict(s) for s in self.series.get(cat, [])]

    def get_series_info(self, sid):
        return {"episodes": self.info.get(str(sid), {})}


class Clock(datetime):
    now_ = datetime(2026, 10, 1, 3, 0)

    @classmethod
    def utcnow(cls):
        return cls.now_


class Base(unittest.TestCase):
    REQUIRE = False

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(lambda: (self.db.close(), engine.dispose()))
        vod = _RealPath(tmp) / "vod"
        (vod / "movies").mkdir(parents=True)
        (vod / "shows").mkdir(parents=True)

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)

        saved = {k: getattr(sync, k) for k in
                 ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT", "datetime")}
        self.addCleanup(lambda: [setattr(sync, k, v) for k, v in saved.items()])
        sync.Path = mapped
        sync.TMDBService = TMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        sync.datetime = Clock
        Clock.now_ = datetime(2026, 10, 1, 3, 0)
        TMDB.down, TMDB.down_for, TMDB.calls = False, set(), []
        self.p = Provider(name="P", server_url="http://provider", username="u", password="p", active=True,
                          require_tmdb_match=self.REQUIRE)
        self.db.add(self.p)
        self.db.commit()
        self.client = XClient(self.p)
        sync.make_provider_client = lambda p: self.client
        for c, t in (("m", "movie"), ("s", "series")):
            self.db.add(ProviderCategory(provider_id=self.p.id, category_id=c, category_name=c, type=t,
                                         whitelisted=True, source_tag=f"T{c}"))
        self.db.commit()
        self.client.movies = {"m": [{"name": "Heat (1995)", "stream_id": 501, "container_extension": "mkv"},
                                    {"name": "Filler (2001)", "stream_id": 502, "container_extension": "mkv"}]}
        ep = lambda i, n: {"id": i, "episode_num": n, "container_extension": "mkv"}
        self.client.series = {"s": [{"name": "Dark (2017)", "series_id": 11},
                                    {"name": "Obscure Show (2003)", "series_id": 33}]}
        self.client.info = {"11": {"1": [ep(1101, 1)]}, "33": {"1": [ep(3301, 1)]}}

    def night(self, days=1):
        run = sync.sync_provider(self.p, "full", self.db)
        self.assertIn(run.status, ("completed",), run.error_message)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()
        Clock.now_ += timedelta(days=days)
        return run

    def movie_ids(self, name):
        return sorted(m.tmdb_id for m in self.db.query(Movie).filter(Movie.title.ilike(f"{name}%")))

    def series_ids(self, name):
        return sorted(s.tmdb_id for s in self.db.query(Series).filter(Series.title.ilike(f"{name}%")))

    def stored(self, kind):
        return json.loads(mdb.get_setting(self.db, f"tmdb_failed_lookups:{self.p.id}:{kind}") or "{}")


class FailedLookupIsNotNoMatch(Base):
    def test_movie_imported_during_outage_is_matched_later(self):
        TMDB.down = True
        self.night()
        TMDB.down = False
        self.night()
        self.night()
        self.assertEqual([949], self.movie_ids("heat"), "Heat imported during a TMDB outage is still a "
                                                        "provider-only title after two healthy syncs")

    def test_series_imported_during_outage_is_matched_later(self):
        TMDB.down = True
        self.night()
        TMDB.down = False
        self.night()
        self.night()
        self.assertEqual([70523], self.series_ids("dark"), "Dark is still provider-only after two healthy syncs")

    def test_outage_night_imports_nothing_for_the_failed_titles(self):
        TMDB.down = True
        run = self.night()
        self.assertEqual([], self.movie_ids("heat"))
        self.assertEqual([], self.movie_ids("filler"))
        self.assertEqual([], self.series_ids("dark"))
        self.assertEqual({"heat|1995", "filler|2001"}, set(self.stored("movie")))
        self.assertEqual({"dark|2017", "obscure show|2003"}, set(self.stored("series")))
        self.assertEqual(0, len(list(sync.VOD_MOVIES_ROOT.iterdir())), "files written for a deferred title")
        self.assertTrue(run.status == "completed")

    def test_a_real_no_match_is_still_imported_at_once(self):
        self.night()
        self.assertEqual(1, len(self.movie_ids("filler")))
        self.assertLess(self.movie_ids("filler")[0], 0)
        self.assertLess(self.series_ids("obscure")[0], 0)
        self.assertEqual({}, self.stored("movie"))
        self.night()
        self.assertEqual(1, TMDB.calls.count("filler"), "a provider-only title is looked up again every night")

    def test_title_failing_for_three_days_is_imported_without_a_match(self):
        TMDB.down = True
        self.night()                      # day 0: first failure
        self.night()                      # day 1
        self.night()                      # day 2
        self.assertEqual([], self.movie_ids("heat"))
        self.night()                      # day 3: bound reached
        ids = self.movie_ids("heat")
        self.assertEqual(1, len(ids))
        self.assertLess(ids[0], 0)
        self.assertEqual(1, len(self.series_ids("dark")))
        self.assertEqual({}, self.stored("movie"), "an imported title is no longer tracked")

    def test_matched_titles_drop_out_of_the_setting(self):
        TMDB.down = True
        self.night()
        TMDB.down = False
        self.night()
        self.assertIsNone(self.db.query(mdb.Setting).filter(
            mdb.Setting.key == f"tmdb_failed_lookups:{self.p.id}:movie").first())

    def test_first_failure_time_is_kept_across_nights(self):
        TMDB.down = True
        self.night()
        first = self.stored("movie")["heat|1995"]
        self.night()
        self.assertEqual(first, self.stored("movie")["heat|1995"])

    def test_one_title_failing_does_not_hold_back_the_others(self):
        TMDB.down_for = {"heat"}
        self.client.movies["m"].append({"name": "Alien (1979)", "stream_id": 503, "container_extension": "mkv"})
        self.night()
        self.assertEqual([348], self.movie_ids("alien"))
        self.assertEqual([], self.movie_ids("heat"))

    def test_a_corrupt_setting_is_read_as_no_failures(self):
        mdb.set_setting(self.db, f"tmdb_failed_lookups:{self.p.id}:movie", "{not json")
        TMDB.down = True
        self.night()
        self.assertEqual([], self.movie_ids("heat"))
        self.assertIn("heat|1995", self.stored("movie"))

    def test_odd_stored_times_never_fail_the_sync_or_stretch_the_bound(self):
        key = f"tmdb_failed_lookups:{self.p.id}:movie"
        TMDB.down = True
        for stored in ({"heat|1995": "2026-09-30T03:00:00+00:00"},      # time zone
                       {"heat|1995": "2027-01-01T00:00:00"},            # clock was ahead
                       {"heat|1995": 12}):                              # not a string
            mdb.set_setting(self.db, key, json.dumps(stored))
            self.night(days=0)
            self.assertEqual([], self.movie_ids("heat"))
        for _ in range(3):                                              # days 0, 1, 2
            self.night()
        self.assertEqual([], self.movie_ids("heat"))
        self.night()                                                    # day 3: bound reached
        self.assertEqual(1, len(self.movie_ids("heat")), "the 3-day bound was stretched")


class FixedByHandIsNeverRematched(Base):
    def test_fixed_stream_is_not_looked_up_or_rematched(self):
        # The admin fixed stream 501 ("Heat" label) to Fargo (275), and the row is Fargo.
        self.db.add(MatchOverride(provider_id=self.p.id, media_type="movie", stream_key="501", tmdb_id=275,
                                  previous_tmdb_id=949, title="Fargo", set_by="admin"))
        self.db.commit()
        self.night()
        self.assertEqual([275], self.movie_ids("fargo"))
        TMDB.calls = []
        TMDB.down = True
        self.night()
        TMDB.down = False
        self.night()
        self.night()
        self.assertEqual([275], self.movie_ids("fargo"))
        self.assertEqual([], self.movie_ids("heat"))
        self.assertNotIn("heat", TMDB.calls, "a stream fixed by hand was looked up by its label")


class RequireMatchOnUnchanged(Base):
    REQUIRE = True

    def test_outage_skips_and_matches_later_as_before(self):
        TMDB.down = True
        self.night()
        self.assertEqual([], self.movie_ids("heat"))
        TMDB.down = False
        self.night()
        self.assertEqual([949], self.movie_ids("heat"))
        self.assertEqual([], self.movie_ids("filler"))
        self.assertEqual({}, self.stored("movie"))

    def test_outage_for_days_never_imports_without_a_match(self):
        TMDB.down = True
        for _ in range(5):
            self.night()
        self.assertEqual([], self.movie_ids("heat"))
        self.assertEqual([], self.movie_ids("filler"))


class FailedLookupsProperty(unittest.TestCase):
    """_FailedLookups across random nights (2,000 seeds): a title is deferred
    exactly while its lookups keep failing and less than LOOKUP_RETRY_DAYS
    have passed since the first failure of the current failing streak; a night
    where it did not fail (matched, no match, gone, or imported) ends the streak."""
    SEEDS = 2000

    def test_random_nights(self):
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            stored = None
            now = datetime(2026, 1, 1) + timedelta(hours=rnd.randint(0, 1000))
            streak = {}          # title -> first failure of the current streak (model)
            imported = set()
            titles = [(f"t{i}", str(1990 + i) if rnd.random() < 0.7 else None) for i in range(4)]
            for night in range(rnd.randint(1, 12)):
                fl = sync._FailedLookups.__new__(sync._FailedLookups)
                fl.key, fl.tonight, fl.deferred = "k", {}, 0
                fl.prior = json.loads(stored) if stored else {}
                for t in titles:
                    if t in imported or rnd.random() < 0.15:
                        streak.pop(t, None)       # known now, or not listed tonight
                        continue
                    if rnd.random() < 0.6:        # lookup failed
                        first = streak.setdefault(t, now)
                        expect = now - first < timedelta(days=sync.LOOKUP_RETRY_DAYS)
                        got = fl.defer(t, now)
                        self.assertEqual(expect, got, f"seed {seed} night {night} {t}")
                        if not got:
                            imported.add(t)       # imported without a match
                            streak.pop(t, None)
                    else:
                        streak.pop(t, None)       # TMDB answered
                stored = json.dumps(fl.tonight) if fl.tonight else None
                self.assertEqual(set(streak), {tuple([k.split("|")[0], k.split("|")[1] or None])
                                               for k in fl.tonight}, f"seed {seed} night {night}")
                now += timedelta(hours=rnd.choice((1, 6, 12, 24, 24, 24, 48)))


class RandomOutageNights(Base):
    """Full syncs over random nights (one per day) where each known title's
    lookup fails or not at random: a title TMDB knows ends under its TMDB id
    unless its lookups failed on LOOKUP_RETRY_DAYS+1 nights in a row from its
    first failure; never two rows for one stream; no-match titles are
    provider-only from their first answered night."""
    SEEDS = 250

    def test_random_outage_nights(self):
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            self.db.query(Movie).delete()
            self.db.query(Series).delete()
            self.db.query(mdb.Setting).filter(mdb.Setting.key.like("tmdb_failed_lookups:%")).delete()
            self.db.commit()
            for d in (sync.VOD_MOVIES_ROOT, sync.VOD_SERIES_ROOT):
                import shutil
                shutil.rmtree(d)
                d.mkdir()
            Clock.now_ = datetime(2026, 10, 1, 3, 0)
            names = ["heat", "alien", "filler"]
            self.client.movies = {"m": [{"name": f"{n.title()} (1995)", "stream_id": 600 + i,
                                         "container_extension": "mkv"} for i, n in enumerate(names)]}
            self.client.series = {"s": []}
            history = {n: [] for n in names}       # per night: True = lookup failed
            imported_at = {}
            for night in range(rnd.randint(1, 6)):
                TMDB.down_for = {n for n in names if rnd.random() < 0.5}
                for n in names:
                    if n not in imported_at:
                        history[n].append(n in TMDB.down_for)
                self.night()
                for n in names:
                    if n not in imported_at and self.movie_ids(n):
                        imported_at[n] = night
            msg = f"seed {seed}: {history} imported {imported_at}"
            for n in names:
                ids = self.movie_ids(n)
                self.assertLessEqual(len(ids), 1, msg)
                h = history[n]
                if n in KNOWN_MOVIES:
                    if ids and ids[0] < 0:
                        # only after LOOKUP_RETRY_DAYS of failed nights in a row
                        self.assertGreaterEqual(len(h), sync.LOOKUP_RETRY_DAYS + 1, msg)
                        self.assertTrue(all(h[-(sync.LOOKUP_RETRY_DAYS + 1):]), msg)
                    if False in h:
                        self.assertEqual([KNOWN_MOVIES[n]], ids, msg)
                else:
                    if False in h:
                        self.assertEqual(1, len(ids), msg)
                        self.assertLess(ids[0], 0, msg)


if __name__ == "__main__":
    unittest.main()
