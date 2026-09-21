"""A TMDB error during a sync must not make existing titles look removed upstream.

The sync only counts a provider stream as "seen" once it has a tmdb_id. Streams whose
cleaned provider name differs from the stored TMDB title (e.g. provider "The Title 5",
TMDB "Title 5") miss the known_titles shortcut and go through TMDBService.search_movie
every time its 30-day cache entry has expired. When that request fails (HTTP 429/5xx,
read timeout, TMDB unreachable), search_movie returns None and the sync counts the
stream as "skipped": the title is absent from seen_ids while fetch_ok stays True, so
the prune marks it, and a second sync with the same failure deletes the row and files.

These tests use the real TMDBService (only its HTTP session is faked) and the real
sync_provider.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import tempfile
import unittest

from models.database import Movie
import services.sync as sync
from services.tmdb import TMDBService
from nightly_harness import NightlyHarness


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeTMDBHttp:
    """TMDB search + details. Provider names are "The Title N"; TMDB titles are "Title N".
    ``throttled`` = search queries answered with HTTP 429."""

    def __init__(self):
        self.throttled = set()
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        params = params or {}
        if url.endswith("/search/movie"):
            q = params["query"]
            if q in self.throttled:
                return _Resp(429)
            n = int(q.rsplit(" ", 1)[1])
            return _Resp(200, {"results": [{"id": 1000 + n, "title": f"Title {n}",
                                            "release_date": "2010-01-01", "popularity": 1}]})
        if "/movie/" in url:
            tmdb_id = int(url.rsplit("/", 1)[1])
            return _Resp(200, {"id": tmdb_id, "title": f"Title {tmdb_id - 1000}",
                               "release_date": "2010-01-01", "genres": []})
        return _Resp(404)


class TestTmdbCache(unittest.TestCase):
    def test_throttled_search_is_not_cached_as_no_match(self):
        """Must-not-change: an HTTP 429 must not be remembered as "no TMDB match".
        (Passes at 97d25e1: _cache_set stores the None, but _cache_get returns that None
        and search_movie reads it as a cache miss, so negative entries never hit.)"""
        cache = tempfile.mkdtemp()
        http = FakeTMDBHttp()
        tmdb = TMDBService("token", cache)
        tmdb.session = http
        http.throttled = {"The Title 5"}
        self.assertIsNone(tmdb.search_movie("The Title 5", "2010"))
        http.throttled = set()
        result = tmdb.search_movie("The Title 5", "2010")
        self.assertIsNotNone(result, "429 was cached as a negative match")
        self.assertEqual(result["tmdb_id"], 1005)


class TestTmdbFailureDuringSync(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.cache = tempfile.mkdtemp()
        self.http = FakeTMDBHttp()
        cache, http = self.cache, self.http

        def real_tmdb(bearer, data_dir, threshold=0.7):
            svc = TMDBService("token", cache, threshold)
            svc.session = http
            return svc
        sync.TMDBService = real_tmdb

    def test_existing_title_survives_tmdb_throttling_on_two_syncs(self):
        """Import 100 titles, let the 30-day TMDB cache expire, then run two syncs
        (the nightly plus a manual one, or two nightlies once the prune/sweep marker
        bug is fixed) while TMDB throttles one search. The provider still lists the
        title both times. At 0e1805f (unchanged since 97d25e1) the row and its .strm are deleted."""
        self.add_category("1")
        self.client.movies["1"] = [(f"The Title {i}", 500 + i) for i in range(100)]
        # FakeClient appends " (2010)" to each name
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 100)
        row = self.movie(1005)
        self.assertEqual(row.title, "Title 5")

        shutil.rmtree(self.cache)          # 30 days later: cache entries expired
        self.http.throttled = {"The Title 5"}
        self.sync_only()
        self.sync_only()

        self.assertIsNotNone(self.movie(1005),
                             "title still offered by the provider was pruned after TMDB errors")


if __name__ == "__main__":
    unittest.main()
