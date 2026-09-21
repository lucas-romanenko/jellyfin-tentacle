"""Discover ownership map and the Watch deep link.

Regression tests for #23 (fixed in 6e50661, which added no tests), plus the
remaining gaps, against routers/discover.py:

* a failed or partial Jellyfin listing is not cached as "Jellyfin has nothing"
  for the full TTL, and a failed refresh keeps the previous map;   (6e50661)
* concurrent requests on a cold cache share one listing;           (6e50661)
* an add to Radarr/Sonarr does not throw away the Jellyfin map;     (6e50661)
* a stale stored jellyfin_item_id is replaced by the live map;      (6e50661)
* a missing Jellyfin server id is looked up again;                  (6e50661)
* STILL OPEN: the map must point at the item users are shown (primary of
  merged versions), not a hidden alternate, and must not overwrite a stored
  user-visible id with the hidden one;
* regressions for #5 (owned in Jellyfin => in_library) and #10 (VOD row gets
  an id + deep link). Image-proxy tests: test_discover_image_proxy.py.

Does not need fastapi: runs with only requests + sqlalchemy installed (like
CI). When FastAPI/httpx/pydantic are missing, minimal stand-ins are installed
first.

Run from tentacle/:  python -m pytest tests/test_discover_library.py
             or:     python -m unittest tests.test_discover_library
"""
import sys
import threading
import time
import types
import unittest
from unittest import mock


# ── Stand-ins for web-framework modules CI doesn't install ────────────────
def _install_framework_stubs():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass

    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, *a, **kw):
            pass

        def _deco(self, *a, **kw):
            return lambda fn: fn

        get = post = put = delete = patch = _deco

    fastapi.HTTPException = HTTPException
    fastapi.APIRouter = APIRouter
    fastapi.Depends = lambda *a, **kw: None
    fastapi.Request = type("Request", (), {})
    fastapi.Response = type("Response", (), {})
    responses = types.ModuleType("fastapi.responses")

    class Response:
        def __init__(self, content=b"", media_type=None, **kw):
            self.body = content
            self.media_type = media_type

    responses.Response = Response
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses

    if "pydantic" not in sys.modules:
        try:
            import pydantic  # noqa: F401
        except ImportError:
            pydantic = types.ModuleType("pydantic")
            pydantic.BaseModel = type("BaseModel", (), {})
            sys.modules["pydantic"] = pydantic

    try:
        import httpx  # noqa: F401
    except ImportError:
        httpx = types.ModuleType("httpx")
        httpx.HTTPError = type("HTTPError", (Exception,), {})
        httpx.AsyncClient = object
        sys.modules["httpx"] = httpx

    auth = types.ModuleType("routers.auth")
    auth.get_user_from_request = lambda *a, **kw: None
    auth.require_admin = lambda *a, **kw: None
    sys.modules["routers.auth"] = auth


_install_framework_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models.database as mdb  # noqa: E402
from models.database import Base, Movie, Series, Setting  # noqa: E402
import routers.discover as discover  # noqa: E402
import services.jellyfin as jellyfin  # noqa: E402

JF_URL = "http://jellyfin.test:8096"


# ── Fake Jellyfin /Items ──────────────────────────────────────────────────
class FakeJellyfin:
    """Serves /Items pages like Jellyfin 10.11 does for JellyfinService._get.

    items: {"Movie": [...], "Series": [...]}; each item is a dict with Id,
    ProviderIds and an optional "_hidden" flag. Hidden items (the non-primary
    half of merged versions, hidden duplicate folders) are only returned when
    the request has no UserId, as observed live.
    fail(params) -> True makes that page time out (JellyfinService._get
    returns None on timeouts/connection errors).
    """

    def __init__(self, items, fail=None, delay=0.0, server_id="srv1"):
        self.items = items
        self.fail = fail or (lambda params: False)
        self.delay = delay
        self.server_id = server_id
        self.listing_starts = {"Movie": 0, "Series": 0}
        self.calls = []
        self._lock = threading.Lock()

    def get(self, service, path, params=None):
        params = dict(params or {})
        with self._lock:
            self.calls.append((path, params))
        if self.delay:
            time.sleep(self.delay)
        if path != "/Items":
            return None
        kind = params.get("IncludeItemTypes")
        start = int(params.get("StartIndex", 0))
        limit = int(params.get("Limit", 100))
        if start == 0:
            with self._lock:
                self.listing_starts[kind] += 1
        if self.fail(params):
            return None
        pool = [i for i in self.items.get(kind, []) if params.get("UserId") is None or not i.get("_hidden")]
        page = [{k: v for k, v in i.items() if k != "_hidden"} for i in pool[start:start + limit]]
        return {"Items": page, "TotalRecordCount": len(pool)}


def movie(tmdb_id, item_id, hidden=False):
    d = {"Id": item_id, "Name": f"m{tmdb_id}", "ProviderIds": {"Tmdb": str(tmdb_id)}}
    if hidden:
        d["_hidden"] = True
    return d


def big_movie_library(n=12000):
    """More items than one 10,000-item page."""
    return [movie(100000 + i, f"id{100000 + i}") for i in range(n)]


class DiscoverTestBase(unittest.TestCase):
    user_id = "adminuser"

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = self.Session()
        db.add_all([
            Setting(key="jellyfin_url", value=JF_URL),
            Setting(key="jellyfin_api_key", value="k"),
            Setting(key="jellyfin_user_id", value=self.user_id),
        ])
        db.commit()
        db.close()
        self._patches = [
            mock.patch.object(mdb, "SessionLocal", self.Session),
            mock.patch.object(discover, "_jf_ids_cache", self._fresh_cache()),
            mock.patch.object(discover, "_arr_ids_cache", {"data": {"movie": set(), "series": set()}, "ts": 1e18}),
            mock.patch.object(discover, "_jf_server_id_cache", self._fresh_server_cache()),
        ]
        for p in self._patches:
            p.start()
        self.clock = [1_000_000.0]
        self._clock_patch = mock.patch("time.time", lambda: self.clock[0])
        self._clock_patch.start()

    def tearDown(self):
        self._clock_patch.stop()
        for p in reversed(self._patches):
            p.stop()

    @staticmethod
    def _fresh_cache():
        # Shape-agnostic: works for 97d25e1's cache and for the fix's.
        cache = {k: (dict(v) if isinstance(v, dict) else v) for k, v in discover._jf_ids_cache.items()}
        cache["movie"] = None
        cache["series"] = None
        cache["ts"] = {"movie": 0, "series": 0}
        if "ok" in cache:
            cache["ok"] = {"movie": False, "series": False}
        return cache

    @staticmethod
    def _fresh_server_cache():
        c = dict(discover._jf_server_id_cache)
        c["id"] = None
        if "checked" in c:
            c["checked"] = False
        if "retry_at" in c:
            c["retry_at"] = 0.0
        return c

    def use_jellyfin(self, fake):
        p = mock.patch.object(jellyfin.JellyfinService, "_get",
                              lambda svc, path, params=None: fake.get(svc, path, params))
        p.start()
        self.addCleanup(p.stop)
        return fake


# ── Ownership map cache ───────────────────────────────────────────────────
class TestJellyfinMapCache(DiscoverTestBase):
    def test_failed_fetch_is_retried_instead_of_cached_for_the_ttl(self):
        """Jellyfin unreachable once must not mean "owns nothing" for 5 minutes."""
        down = [True]
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(602411, "vod1")]}, fail=lambda p: down[0]))
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie"), {})
        down[0] = False
        self.clock[0] += 60  # well inside JF_IDS_TTL (300 s)
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(602411), "vod1")

    def test_failed_refresh_keeps_the_previous_map(self):
        """An expired map whose refresh fails is still better than an empty one."""
        down = [False]
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(602411, "vod1")]}, fail=lambda p: down[0]))
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(602411), "vod1")
        down[0] = True
        self.clock[0] += discover.JF_IDS_TTL + 1
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(602411), "vod1")

    def test_partial_listing_is_not_cached_as_complete(self):
        """A timed-out second page must not be cached as the whole library."""
        flaky = [True]
        self.use_jellyfin(FakeJellyfin({"Movie": big_movie_library()},
                                       fail=lambda p: flaky[0] and int(p.get("StartIndex", 0)) > 0))
        first = discover._get_jellyfin_tmdb_items("movie")
        self.assertNotIn(111999, first)  # the last item lives on a failed page
        flaky[0] = False
        self.clock[0] += 60
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(111999), "id111999")

    def test_partial_refresh_keeps_the_previous_complete_map(self):
        """A timed-out page on refresh must not drop the titles on that page."""
        flaky = [False]
        self.use_jellyfin(FakeJellyfin({"Movie": big_movie_library()},
                                       fail=lambda p: flaky[0] and int(p.get("StartIndex", 0)) > 0))
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(111999), "id111999")
        flaky[0] = True
        self.clock[0] += discover.JF_IDS_TTL + 1
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(111999), "id111999")

    def test_concurrent_cold_requests_share_one_listing(self):
        """Discover rows resolve in parallel threads; they must not each list the library."""
        fake = self.use_jellyfin(FakeJellyfin({"Movie": [movie(1, "a"), movie(2, "b")]}, delay=0.1))
        results, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(discover._get_jellyfin_tmdb_items("movie"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fake.listing_starts["Movie"], 1)
        self.assertTrue(all(r == {1: "a", 2: "b"} for r in results), results)

    def test_add_to_arr_does_not_refetch_the_jellyfin_map(self):
        """bust_arr_ids_cache() runs after every add; nothing new is in Jellyfin yet."""
        fake = self.use_jellyfin(FakeJellyfin({"Movie": [movie(1, "a")]}))
        discover._get_jellyfin_tmdb_items("movie")
        for _ in range(5):  # e.g. "add missing" on a list
            discover.bust_arr_ids_cache()
            discover._get_jellyfin_tmdb_items("movie")
        self.assertEqual(fake.listing_starts["Movie"], 1)

    def test_add_to_arr_still_resets_the_arr_cache(self):
        """Must not change: new Radarr/Sonarr entries badge as requested immediately."""
        discover.bust_arr_ids_cache()
        self.assertEqual(discover._arr_ids_cache["ts"], 0)

    def test_unconfigured_jellyfin_is_an_empty_map(self):
        """Must not change: no Jellyfin configured degrades to the DB-only check."""
        db = self.Session()
        db.query(Setting).filter(Setting.key == "jellyfin_url").delete()
        db.commit()
        db.close()
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie"), {})

    def test_map_points_at_the_item_users_are_shown(self):
        """Merged versions: the unscoped listing also returns the hidden alternate
        (live: "300" -> the .strm, not the downloaded 300.mp4 users see)."""
        self.use_jellyfin(FakeJellyfin({"Movie": [
            movie(1271, "primary-mp4"),
            movie(1271, "alternate-strm", hidden=True),
        ]}))
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie")[1271], "primary-mp4")

    def test_unknown_configured_user_still_gets_a_map(self):
        """Must not change: a stale jellyfin_user_id (Jellyfin answers 400) doesn't
        turn the Jellyfin check off."""
        import requests
        fake = FakeJellyfin({"Movie": [movie(1271, "primary-mp4")]})

        def get(svc, path, params=None):
            if (params or {}).get("UserId"):
                resp = requests.Response()
                resp.status_code = 400
                raise requests.HTTPError("400 Bad Request", response=resp)
            return fake.get(svc, path, params)

        p = mock.patch.object(jellyfin.JellyfinService, "_get", get)
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(discover._get_jellyfin_tmdb_items("movie").get(1271), "primary-mp4")


# ── Detail endpoint: id + deep link ──────────────────────────────────────
class FakeTMDB:
    def get_movie_details(self, tmdb_id):
        return {"tmdb_id": tmdb_id, "title": "T", "media_type": "movie"}

    def get_series_details(self, tmdb_id):
        return {"tmdb_id": tmdb_id, "title": "S", "media_type": "series"}


class TestDiscoverDetail(DiscoverTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(discover, "_get_tmdb", lambda db: FakeTMDB())
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(jellyfin.JellyfinService, "get_server_id", lambda svc: "srv1")
        p2.start()
        self.addCleanup(p2.stop)

    def detail(self, media_type, tmdb_id):
        db = self.Session()
        try:
            return discover.get_discover_detail(media_type, tmdb_id, request=None, db=db)
        finally:
            db.close()

    def add_row(self, model, tmdb_id, source="provider_1", jellyfin_item_id=None):
        db = self.Session()
        db.add(model(tmdb_id=tmdb_id, title="x", source=source, jellyfin_item_id=jellyfin_item_id))
        db.commit()
        db.close()

    def stored_id(self, model, tmdb_id):
        db = self.Session()
        try:
            return db.query(model).filter(model.tmdb_id == tmdb_id).one().jellyfin_item_id
        finally:
            db.close()

    def test_vod_series_without_stored_id_gets_id_and_deep_link(self):
        """#10 regression: a .strm series row with no stored id is reachable."""
        self.add_row(Series, 95350)
        self.use_jellyfin(FakeJellyfin({"Series": [
            {"Id": "891a3459", "Name": "Lanterns", "ProviderIds": {"Tmdb": "95350"}}]}))
        d = self.detail("series", 95350)
        self.assertTrue(d["in_library"])
        self.assertEqual(d["jellyfin_item_id"], "891a3459")
        self.assertEqual(d["jellyfin_url"], f"{JF_URL}/web/#/details?id=891a3459&serverId=srv1")
        self.assertEqual(self.stored_id(Series, 95350), "891a3459")

    def test_title_only_in_jellyfin_is_in_library(self):
        """#5 regression: owned in Jellyfin but unknown to Tentacle => in library, not requested."""
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(603692, "dl1")]}))
        d = self.detail("movie", 603692)
        self.assertTrue(d["in_library"])
        self.assertFalse(d["requested"])
        self.assertEqual(d["jellyfin_item_id"], "dl1")

    def test_stale_stored_id_is_replaced_by_the_live_map(self):
        """Jellyfin re-created the item (rebuild, rename, .strm folder re-created)."""
        self.add_row(Movie, 602411, jellyfin_item_id="old-id")
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(602411, "new-id")]}))
        d = self.detail("movie", 602411)
        self.assertEqual(d["jellyfin_item_id"], "new-id")
        self.assertIn("id=new-id", d["jellyfin_url"])
        self.assertEqual(self.stored_id(Movie, 602411), "new-id")

    def test_stored_id_is_kept_while_jellyfin_is_unreachable(self):
        """Must not change: Jellyfin down keeps offering the stored link."""
        self.add_row(Movie, 602411, jellyfin_item_id="old-id")
        self.use_jellyfin(FakeJellyfin({"Movie": []}, fail=lambda p: True))
        d = self.detail("movie", 602411)
        self.assertEqual(d["jellyfin_item_id"], "old-id")
        self.assertTrue(d["in_library"])

    def test_stored_visible_id_is_not_replaced_by_a_hidden_alternate(self):
        """Since 6e50661 the live map beats a stored id. The map must then be the
        item users see, or a correct stored id is overwritten with the hidden
        merged-version alternate (and persisted)."""
        self.add_row(Movie, 1271, jellyfin_item_id="primary-mp4")
        self.use_jellyfin(FakeJellyfin({"Movie": [
            movie(1271, "primary-mp4"),
            movie(1271, "alternate-strm", hidden=True),
        ]}))
        d = self.detail("movie", 1271)
        self.assertEqual(d["jellyfin_item_id"], "primary-mp4")
        self.assertEqual(self.stored_id(Movie, 1271), "primary-mp4")

    def test_server_id_is_looked_up_again_after_a_failure(self):
        """Jellyfin slow/unreachable on the first link must not drop serverId until restart."""
        answers = iter([None, "srv1"])
        with mock.patch.object(jellyfin.JellyfinService, "get_server_id", lambda svc: next(answers)):
            db = self.Session()
            try:
                first = discover._jellyfin_web_url(db, "abc")
                self.clock[0] += 120
                second = discover._jellyfin_web_url(db, "abc")
            finally:
                db.close()
        self.assertEqual(first, f"{JF_URL}/web/#/details?id=abc")
        self.assertEqual(second, f"{JF_URL}/web/#/details?id=abc&serverId=srv1")


# ── In-library marking on list/search rows ────────────────────────────────
class TestInLibraryMarking(DiscoverTestBase):
    def test_rows_mark_titles_owned_only_in_jellyfin(self):
        """#5 regression: Popular/Trending/Search rows use Jellyfin as the authority."""
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(10, "a")], "Series": [movie(20, "b")]}))
        items = discover._dedup_and_mark(
            [{"tmdb_id": 10, "media_type": "movie"}, {"tmdb_id": 20, "media_type": "series"},
             {"tmdb_id": 30, "media_type": "movie"}],
            {"movie": set(), "series": set()})
        self.assertEqual([i["in_library"] for i in items], [True, True, False])


if __name__ == "__main__":
    unittest.main()
