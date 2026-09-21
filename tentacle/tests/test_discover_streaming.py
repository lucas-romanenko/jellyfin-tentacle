"""New on Streaming: recent releases per provider, marked for in-library (#feature).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestStreamingRoutes(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import get_db, TentacleUser, Movie
        from routers import discover
        self.discover = discover
        self.db = _db()
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="lucas", is_admin=True))
        mdb.set_setting(self.db, "tmdb_token", "x")
        mdb.set_setting(self.db, "data_dir", tempfile.mkdtemp())
        # A Netflix title already in the library, to prove it gets marked.
        self.db.add(Movie(tmdb_id=603, title="Owned", source="radarr"))
        self.db.commit()
        app = FastAPI()
        app.include_router(discover.router)
        app.dependency_overrides[get_db] = lambda: self.db
        u = self.db.query(TentacleUser).first()
        from routers.auth import get_user_from_request
        app.dependency_overrides[get_user_from_request] = lambda: u
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()

    def test_providers_lists_the_five_services(self):
        r = self.client.get("/api/discover/providers")
        self.assertEqual(r.status_code, 200, r.text)
        slugs = [p["slug"] for p in r.json()["providers"]]
        self.assertEqual(slugs, ["netflix", "crave", "disney", "prime", "appletv"])
        self.assertEqual(r.json()["region"], "CA")

    def test_unknown_provider_is_404(self):
        r = self.client.get("/api/discover/streaming?provider=hbo&type=movies")
        self.assertEqual(r.status_code, 404)

    def test_streaming_returns_marked_items(self):
        fake = mock.MagicMock()
        fake.get_new_on_provider.return_value = [
            {"tmdb_id": 100, "title": "New Movie", "year": "2026", "media_type": "movie",
             "poster_path": "/p.jpg", "provider_id": 8},
            {"tmdb_id": 603, "title": "Owned", "year": "2020", "media_type": "movie",
             "poster_path": "/o.jpg", "provider_id": 8},
        ]
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake), \
                mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {}):
            r = self.client.get("/api/discover/streaming?provider=netflix&type=movies")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["name"], "Netflix")
        by_id = {i["tmdb_id"]: i for i in body["items"]}
        self.assertTrue(by_id[603]["in_library"], "a library title must be marked in_library")
        self.assertFalse(by_id[100].get("in_library"))
        # Provider id passed straight through to TMDB.
        fake.get_new_on_provider.assert_called_once()
        self.assertEqual(fake.get_new_on_provider.call_args.args[1], 8)

    def test_series_type_asks_tmdb_for_series(self):
        fake = mock.MagicMock()
        fake.get_new_on_provider.return_value = []
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake):
            self.client.get("/api/discover/streaming?provider=disney&type=series")
        self.assertEqual(fake.get_new_on_provider.call_args.args[0], "series")
        self.assertEqual(fake.get_new_on_provider.call_args.args[1], 337)

    def test_requires_a_session(self):
        from routers.auth import get_user_from_request
        self.client.app.dependency_overrides.pop(get_user_from_request, None)
        r = self.client.get("/api/discover/streaming?provider=netflix")
        self.assertEqual(r.status_code, 401)


class TestTmdbProviderQuery(unittest.TestCase):
    def test_builds_a_flatrate_region_query_sorted_newest(self):
        from services.tmdb import TMDBService
        svc = TMDBService("token", tempfile.mkdtemp())
        seen = {}
        def fake_request(endpoint, params=None):
            seen["endpoint"] = endpoint
            seen["params"] = params
            return {"results": [{"id": 1, "title": "A", "release_date": "2026-09-01",
                                 "poster_path": "/a.jpg", "vote_average": 7.0}]}
        with mock.patch.object(svc, "_request", fake_request):
            out = svc.get_new_on_provider("movie", 8, region="CA", pages=1)
        self.assertEqual(seen["endpoint"], "discover/movie")
        p = seen["params"]
        self.assertEqual(p["watch_region"], "CA")
        self.assertEqual(p["with_watch_providers"], "8")
        self.assertEqual(p["with_watch_monetization_types"], "flatrate")
        self.assertEqual(p["sort_by"], "primary_release_date.desc")
        self.assertIn("primary_release_date.gte", p)
        self.assertEqual(out[0]["provider_id"], 8)

    def test_series_uses_first_air_date(self):
        from services.tmdb import TMDBService
        svc = TMDBService("token", tempfile.mkdtemp())
        seen = {}
        with mock.patch.object(svc, "_request", lambda e, params=None: seen.update(endpoint=e, params=params) or {"results": []}):
            svc.get_new_on_provider("series", 337, region="CA", pages=1)
        self.assertEqual(seen["endpoint"], "discover/tv")
        self.assertEqual(seen["params"]["sort_by"], "first_air_date.desc")



class TestGenreRoutes(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import get_db, TentacleUser, Movie
        from routers import discover
        from routers.auth import get_user_from_request
        self.discover = discover
        self.db = _db()
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="lucas", is_admin=True))
        mdb.set_setting(self.db, "tmdb_token", "x")
        mdb.set_setting(self.db, "data_dir", tempfile.mkdtemp())
        self.db.add(Movie(tmdb_id=603, title="Owned", source="radarr"))
        self.db.commit()
        app = FastAPI()
        app.include_router(discover.router)
        app.dependency_overrides[get_db] = lambda: self.db
        u = self.db.query(TentacleUser).first()
        app.dependency_overrides[get_user_from_request] = lambda: u
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()

    def test_genres_list(self):
        fake = mock.MagicMock()
        fake.get_genres.return_value = [{"id": 28, "name": "Action"}, {"id": 35, "name": "Comedy"}]
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake):
            r = self.client.get("/api/discover/genres?type=movies")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([g["name"] for g in r.json()["genres"]], ["Action", "Comedy"])
        self.assertEqual(fake.get_genres.call_args.args[0], "movie")

    def test_genre_items_marked(self):
        fake = mock.MagicMock()
        fake.get_by_genre.return_value = [
            {"tmdb_id": 100, "title": "A", "media_type": "movie", "poster_path": "/a.jpg"},
            {"tmdb_id": 603, "title": "Owned", "media_type": "movie", "poster_path": "/o.jpg"},
        ]
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake), \
                mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {}):
            r = self.client.get("/api/discover/genre?genre_id=28&type=movies")
        self.assertEqual(r.status_code, 200, r.text)
        by_id = {i["tmdb_id"]: i for i in r.json()["items"]}
        self.assertTrue(by_id[603]["in_library"])
        self.assertEqual(fake.get_by_genre.call_args.args[:2], ("movie", 28))
        self.assertEqual(fake.get_by_genre.call_args.kwargs.get("mode"), "top_rated")

    def test_genre_series_type(self):
        fake = mock.MagicMock(); fake.get_by_genre.return_value = []
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake):
            self.client.get("/api/discover/genre?genre_id=99&type=series")
        self.assertEqual(fake.get_by_genre.call_args.args[:2], ("series", 99))

    def test_genre_new_mode_forwarded(self):
        fake = mock.MagicMock(); fake.get_by_genre.return_value = []
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake):
            self.client.get("/api/discover/genre?genre_id=28&type=movies&mode=new")
        self.assertEqual(fake.get_by_genre.call_args.kwargs.get("mode"), "new")

    def test_discover_keeps_the_original_movie_sections_only(self):
        # The extra Trending/Top Rated top-level tabs were reverted: movies keep
        # Popular / Now Playing / Upcoming (+ From My Lists).
        fake = mock.MagicMock()
        fake.get_popular.return_value = [{"tmdb_id": 1, "title": "P", "media_type": "movie", "poster_path": "/p.jpg"}]
        fake.get_now_playing.return_value = []
        fake.get_upcoming.return_value = []
        with mock.patch.object(self.discover, "_get_tmdb", lambda db: fake), \
                mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {}), \
                mock.patch.object(self.discover, "_get_missing_from_lists", lambda *a, **k: []):
            r = self.client.get("/api/discover?type=movies")
        ids = [s["id"] for s in r.json()["sections"]]
        self.assertNotIn("trending", ids)
        self.assertNotIn("top_rated", ids)
        self.assertIn("popular", ids)



class TestFromMyListsPicker(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import get_db, TentacleUser, ListSubscription, ListItem
        from routers import discover
        from routers.auth import get_user_from_request
        self.discover = discover
        self.db = _db()
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="lucas", is_admin=True))
        mdb.set_setting(self.db, "tmdb_token", "x")
        mdb.set_setting(self.db, "data_dir", tempfile.mkdtemp())
        self.db.add(ListSubscription(id=10, name="IMDb Top 250", type="imdb_rss", url="u", tag="t", active=True, user_id=1))
        self.db.add(ListSubscription(id=11, name="Letterboxd", type="letterboxd", url="u2", tag="t2", active=True, user_id=1))
        # missing items in each list
        self.db.add(ListItem(list_id=10, tmdb_id=101, media_type="movie", title="A", poster_path="/a.jpg"))
        self.db.add(ListItem(list_id=11, tmdb_id=201, media_type="movie", title="B", poster_path="/b.jpg"))
        self.db.commit()
        app = FastAPI()
        app.include_router(discover.router)
        app.dependency_overrides[get_db] = lambda: self.db
        u = self.db.query(TentacleUser).first()
        app.dependency_overrides[get_user_from_request] = lambda: u
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()

    def test_lists_returns_active_subscriptions(self):
        r = self.client.get("/api/discover/lists?type=movies")
        self.assertEqual(r.status_code, 200, r.text)
        names = [l["name"] for l in r.json()["lists"]]
        self.assertIn("IMDb Top 250", names)
        self.assertIn("Letterboxd", names)

    def test_list_missing_all_mixes_both_lists(self):
        with mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {}):
            r = self.client.get("/api/discover/list-missing?list_id=all&type=movies")
        ids = {i["tmdb_id"] for i in r.json()["items"]}
        self.assertEqual(ids, {101, 201})

    def test_list_missing_one_list_only(self):
        with mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {}):
            r = self.client.get("/api/discover/list-missing?list_id=10&type=movies")
        ids = {i["tmdb_id"] for i in r.json()["items"]}
        self.assertEqual(ids, {101}, "a single-list tab shows only that list's missing items")

    def test_list_missing_bad_id_is_400(self):
        r = self.client.get("/api/discover/list-missing?list_id=abc&type=movies")
        self.assertEqual(r.status_code, 400)

    def test_owned_titles_are_excluded(self):
        # 101 is in the library -> not "missing".
        with mock.patch.object(self.discover, "_get_jellyfin_tmdb_items", lambda mt: {101: {"Id": "x"}}):
            r = self.client.get("/api/discover/list-missing?list_id=10&type=movies")
        self.assertEqual(r.json()["items"], [])


if __name__ == "__main__":
    unittest.main()