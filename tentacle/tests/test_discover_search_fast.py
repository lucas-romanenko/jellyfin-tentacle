"""Discover search answers while the user types, without waiting on Radarr or
Sonarr (2026-10-21).

The TV app searches on every keystroke ("t", "ts", "tsn"). Each request waited
on Radarr's and Sonarr's text lookups, which ask the internet metadata
services: "ts" took Radarr's full 15 s timeout, past the plugin's own 15 s, so
the TV showed "Tentacle is busy and didn't answer in time" over the results.
Now:

* TMDB gives the results; a lookup gets SEARCH_ARR_WAIT and never holds the
  answer, and one that answers later is used by the next request for the query;
* a query's searches are kept for a few minutes (normalized: case, spaces), and
  a request for a query still running shares it instead of starting another;
* one character searches Live TV channels only; two skip the lookups;
* a failed TMDB search is asked again next time, not kept as "nothing found";
* "In Library" / "Requested" come from Tentacle's tables and the cached
  Jellyfin and Radarr/Sonarr id maps: a stale map is used while it refreshes
  in the background;
* channels stay first and the item fields the TV app reads are unchanged.

Run from tentacle/:  python -m unittest tests.test_discover_search_fast
"""
import threading
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


def _tmdb_item(tid, title, mt="movie", popularity=10.0):
    return {"tmdb_id": tid, "title": title, "year": "2020", "overview": "",
            "rating": 7.0, "poster_path": f"/p{tid}.jpg", "backdrop_path": None,
            "media_type": mt, "popularity": popularity}


class FakeTmdb:
    def __init__(self, results=None, fail=False):
        self._tl = threading.local()
        self.results = results if results is not None else [
            _tmdb_item(1, "TSN The Movie"), _tmdb_item(2, "TSN Show", "series")]
        self.fail = fail
        self.calls = []

    def _lookup_failed(self):
        return getattr(self._tl, "failed", False)

    def search_multi_results(self, q, media_type="all"):
        self.calls.append((q, media_type))
        if self.fail:
            self._tl.failed = True
            return []
        return [dict(i) for i in self.results]


def _radarr_hit(tid, title):
    return {"tmdbId": tid, "title": title, "year": 2001, "overview": "",
            "ratings": {"value": 6.0}, "images": []}


class SearchTestBase(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        import routers.discover as disc
        self.disc = disc
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr.test"), ("radarr_api_key", "rk"),
                     ("sonarr_url", "http://sonarr.test"), ("sonarr_api_key", "sk")):
            mdb.set_setting(self.db, k, v)
        for i, name in enumerate(["TSN 1", "TSN 2", "CBC"]):
            self.db.add(mdb.LiveChannel(provider_id=1, name=name, stream_url=f"http://x/{i}",
                                        enabled=True, sort_order=i))
        self.db.add(mdb.Movie(tmdb_id=1, title="TSN The Movie", source="radarr"))
        self.db.commit()

        disc.clear_search_cache()
        self.addCleanup(disc.clear_search_cache)
        saved_jf = (dict(disc._jf_ids_cache), dict(disc._jf_ids_cache["ts"]))
        saved_arr = dict(disc._arr_ids_cache)

        def restore():
            disc._jf_ids_cache.update(saved_jf[0])
            disc._jf_ids_cache["ts"] = saved_jf[1]
            disc._arr_ids_cache.update(saved_arr)
            disc._id_refreshes.clear()
        self.addCleanup(restore)
        # Fresh id maps by default: nothing is fetched
        now = time.time()
        disc._jf_ids_cache.update({"movie": {}, "series": {}, "ts": {"movie": now, "series": now}})
        disc._arr_ids_cache.update({"data": {"movie": {5}, "series": set()}, "ts": now})
        disc._id_refreshes.clear()

        self.tmdb = FakeTmdb()
        p = mock.patch.object(disc, "_get_tmdb", side_effect=lambda db: self.tmdb)
        p.start()
        self.addCleanup(p.stop)
        self.lookups = []
        self.radarr_gate = threading.Event()
        self.radarr_gate.set()
        self.radarr_results = [_radarr_hit(5, "TSN Requested"), _radarr_hit(1, "TSN The Movie")]

        def fake_lookup(kind, url, key, q):
            self.lookups.append((kind, q))
            if kind == "radarr":
                self.radarr_gate.wait(10)
                return self.radarr_results
            return []
        p = mock.patch.object(disc, "_arr_lookup_job", side_effect=fake_lookup)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.radarr_gate.set)

    def search(self, q, type_="all"):
        return self.disc.search_discover(q=q, type=type_, db=self.db)["items"]


class SlowLookupsDoNotHoldTheAnswer(SearchTestBase):
    def test_a_hanging_radarr_lookup_costs_only_the_short_wait(self):
        self.radarr_gate.clear()
        with mock.patch.object(self.disc, "SEARCH_ARR_WAIT", 0.3):
            t0 = time.monotonic()
            items = self.search("tsn")
            took = time.monotonic() - t0
        self.assertLess(took, 2.0)
        self.assertEqual(["TSN 1", "TSN 2", "TSN The Movie", "TSN Show"], [i["title"] for i in items])

    def test_a_lookup_that_answers_late_is_used_by_the_next_request(self):
        self.radarr_gate.clear()
        with mock.patch.object(self.disc, "SEARCH_ARR_WAIT", 0.2):
            first = self.search("tsn")
            self.assertNotIn("TSN Requested", [i["title"] for i in first])
            self.radarr_gate.set()
            self.disc._search_cache[("tsn", "all")]["radarr"].result(timeout=5)
            second = self.search("tsn")
        self.assertIn("TSN Requested", [i["title"] for i in second])
        self.assertEqual(1, sum(1 for k, _ in self.lookups if k == "radarr"))

    def test_lookups_answering_in_time_still_add_titles_tmdb_lacks(self):
        items = self.search("tsn")
        titles = [i["title"] for i in items]
        self.assertEqual(1, titles.count("TSN The Movie"), "TMDB and Radarr hits are deduplicated")
        extra = next(i for i in items if i["title"] == "TSN Requested")
        self.assertEqual("radarr", extra["source"])
        self.assertTrue(extra["requested"])
        self.assertFalse(extra["in_library"])

    def test_a_failing_lookup_does_not_fail_the_answer(self):
        def boom(*a):
            raise RuntimeError("radarr exploded")
        with mock.patch.object(self.disc, "_arr_lookup_job", side_effect=boom):
            items = self.search("tsn")
        self.assertIn("TSN Show", [i["title"] for i in items])


class QueriesAreCached(SearchTestBase):
    def test_the_same_query_is_searched_once(self):
        self.search("tsn")
        self.search("  TSN ")
        self.search("Tsn")
        self.assertEqual([("tsn", "all")], self.tmdb.calls)
        self.assertEqual(2, len(self.lookups))   # one Radarr, one Sonarr

    def test_each_type_is_its_own_query(self):
        self.search("tsn")
        self.search("tsn", "movies")
        self.assertEqual([("tsn", "all"), ("tsn", "movie")], self.tmdb.calls)

    def test_an_expired_query_is_searched_again(self):
        self.search("tsn")
        self.disc._search_cache[("tsn", "all")]["ts"] -= self.disc.SEARCH_CACHE_TTL + 1
        self.search("tsn")
        self.assertEqual(2, len(self.tmdb.calls))

    def test_a_failed_tmdb_search_is_not_kept(self):
        self.tmdb.fail = True
        items = self.search("tsn")
        # Radarr's titles still answer (in Radarr's order)
        self.assertEqual(["TSN 1", "TSN 2", "TSN Requested", "TSN The Movie"],
                         [i["title"] for i in items])
        self.tmdb.fail = False
        self.search("tsn")
        self.assertEqual(2, len(self.tmdb.calls))

    def test_cached_results_are_not_changed_by_a_request(self):
        first = self.search("tsn")
        first[2]["title"] = "changed"
        self.assertIn("TSN The Movie", [i["title"] for i in self.search("tsn")])

    def test_the_cache_stays_bounded(self):
        with mock.patch.object(self.disc, "SEARCH_CACHE_MAX", 8):
            for n in range(20):
                self.search(f"tsn {n}")
            self.assertLessEqual(len(self.disc._search_cache), 8)


class ShortQueries(SearchTestBase):
    def test_one_character_searches_channels_only(self):
        items = self.search("t")
        self.assertEqual(["TSN 1", "TSN 2"], [i["title"] for i in items])
        self.assertEqual([], self.tmdb.calls)
        self.assertEqual([], self.lookups)

    def test_two_characters_search_tmdb_but_not_the_lookups(self):
        items = self.search("ts")
        self.assertEqual([("ts", "all")], self.tmdb.calls)
        self.assertEqual([], self.lookups)
        self.assertIn("TSN Show", [i["title"] for i in items])

    def test_channels_type_never_asks_tmdb(self):
        items = self.search("tsn", "channels")
        self.assertEqual(["channel", "channel"], [i["media_type"] for i in items])
        self.assertEqual([], self.tmdb.calls)


class BadgesFromCachedMaps(SearchTestBase):
    def test_in_library_from_the_tables_and_jellyfin_map(self):
        self.disc._jf_ids_cache["series"] = {2: "jf-item"}
        items = {i["title"]: i for i in self.search("tsn")}
        self.assertTrue(items["TSN The Movie"]["in_library"])   # Movie table
        self.assertTrue(items["TSN Show"]["in_library"])        # Jellyfin map
        self.assertFalse(items["TSN The Movie"]["requested"])

    def test_stale_maps_are_used_while_they_refresh_in_the_background(self):
        self.disc._jf_ids_cache["series"] = {2: "jf-item"}
        self.disc._jf_ids_cache["ts"] = {"movie": 0, "series": 0}
        self.disc._arr_ids_cache["ts"] = 0
        gate = threading.Event()
        self.addCleanup(gate.set)
        calls = []

        def slow_jf(mt):
            calls.append(mt)
            gate.wait(10)
            return {}

        def slow_arr():
            calls.append("arr")
            gate.wait(10)
            return {"movie": set(), "series": set()}
        with mock.patch.object(self.disc, "_get_jellyfin_tmdb_items", side_effect=slow_jf), \
             mock.patch.object(self.disc, "_get_arr_tmdb_ids", side_effect=slow_arr):
            t0 = time.monotonic()
            items = {i["title"]: i for i in self.search("tsn")}
            took = time.monotonic() - t0
            self.search("tsn")    # a refresh already running is not started twice
            gate.set()
            for fut in list(self.disc._id_refreshes.values()):
                fut.result(timeout=5)
        self.assertLess(took, 1.5)
        self.assertTrue(items["TSN Show"]["in_library"])
        self.assertTrue(items["TSN Requested"]["requested"])
        self.assertEqual(["arr", "movie", "series"], sorted(calls))

    def test_a_first_map_fetch_is_waited_for_only_briefly(self):
        self.disc._jf_ids_cache.update({"movie": None, "series": None, "ts": {"movie": 0, "series": 0}})
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(self.disc, "_get_jellyfin_tmdb_items",
                               side_effect=lambda mt: gate.wait(10) and {}), \
             mock.patch.object(self.disc, "SEARCH_ARR_WAIT", 0.3):
            t0 = time.monotonic()
            items = self.search("tsn")
            took = time.monotonic() - t0
            gate.set()
        self.assertLess(took, 2.0)
        self.assertIn("TSN Show", [i["title"] for i in items])


class ResponseShape(SearchTestBase):
    def test_channels_first_then_items_with_the_fields_the_tv_reads(self):
        items = self.search("tsn")
        self.assertEqual({"media_type": "channel", "title": "TSN 1"},
                         {k: items[0][k] for k in ("media_type", "title")})
        self.assertIn("channel_id", items[0])
        movie = items[2]
        for field in ("tmdb_id", "title", "year", "overview", "rating", "poster_path",
                      "backdrop_path", "media_type", "in_library", "requested"):
            self.assertIn(field, movie)

    def test_empty_query(self):
        self.assertEqual([], self.search("   "))


if __name__ == "__main__":
    unittest.main()
