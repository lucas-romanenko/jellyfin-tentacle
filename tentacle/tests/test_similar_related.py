"""More Like This (#292): TMDB's related titles for the plugin's row.

Run from the tentacle/ directory:  python -m unittest tests.test_similar_related

GET /api/similar/{movie|tv}/{id} gives the plugin TMDB's collection (the
other films of a franchise, by release date), recommendations and similar
titles as ids, plus Tentacle's own tags, which the plugin leaves out of its
score. One details request (recommendations and similar appended) and one
for a film's collection, kept 7 days; a TMDB that can't answer is not kept.
"""
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir

from services.exceptions import TMDBConnectionError
from services.tmdb import TMDBService

TOY_STORY_2 = {
    "id": 863, "title": "Toy Story 2",
    "belongs_to_collection": {"id": 10194, "name": "Toy Story Collection"},
    "recommendations": {"results": [{"id": 585}, {"id": 12}, {"id": 863}, {"id": None}]},
    "similar": {"results": [{"id": 9806}]},
}
COLLECTION = {"parts": [{"id": 301528, "release_date": "2019-06-19"}, {"id": 862, "release_date": "1995-10-30"},
                        {"id": 863, "release_date": "1999-10-30"}, {"id": 10193, "release_date": "2010-06-16"}]}


class Related(unittest.TestCase):
    def setUp(self):
        self.tmdb = TMDBService(bearer_token="x", cache_dir=temp_dir(self))
        self.calls = []

    def answer(self, answers):
        def fake(endpoint, params=None):
            self.calls.append((endpoint, (params or {}).get("append_to_response")))
            return answers.get(endpoint)
        return mock.patch.object(self.tmdb, "_request", side_effect=fake)

    def test_a_film_gives_its_collection_recommendations_and_similar(self):
        with self.answer({"movie/863": TOY_STORY_2, "collection/10194": COLLECTION}):
            got = self.tmdb.get_related("movie", 863)
        self.assertEqual({"collection": [862, 10193, 301528], "recommendations": [585, 12], "similar": [9806]}, got)
        self.assertEqual([("movie/863", "recommendations,similar"), ("collection/10194", None)], self.calls)

    def test_it_is_kept_a_week(self):
        with self.answer({"movie/863": TOY_STORY_2, "collection/10194": COLLECTION}):
            self.tmdb.get_related("movie", 863)
            self.tmdb.get_related("movie", 863)
        self.assertEqual(2, len(self.calls), "the second ask is answered from the cache")
        hit, _ = self.tmdb._cache_lookup("related:movie:863")
        self.assertTrue(hit)

    def test_a_show_uses_tv_and_has_no_collection(self):
        show = {"id": 1396, "recommendations": {"results": [{"id": 60059}]}, "similar": {"results": []}}
        with self.answer({"tv/1396": show}):
            got = self.tmdb.get_related("tv", 1396)
        self.assertEqual({"collection": [], "recommendations": [60059], "similar": []}, got)

    def test_a_tmdb_that_cannot_answer_is_not_kept(self):
        with self.answer({}):
            self.assertIsNone(self.tmdb.get_related("movie", 863))
        with self.answer({"movie/863": TOY_STORY_2, "collection/10194": COLLECTION}):
            self.assertEqual([862, 10193, 301528], self.tmdb.get_related("movie", 863)["collection"])

    def test_bad_input_asks_nothing(self):
        with self.answer({}):
            self.assertIsNone(self.tmdb.get_related("person", 863))
            self.assertIsNone(self.tmdb.get_related("movie", 0))
        self.assertEqual([], self.calls)


class Route(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import get_db, TentacleUser, ListSubscription
        from routers import similar
        from routers.auth import get_user_from_request
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="lucas", is_admin=True))
        mdb.set_setting(self.db, "data_dir", temp_dir(self))
        self.db.add(ListSubscription(name="Oscars", type="imdb_rss", url="https://example.invalid/l", tag="Oscar Winners"))
        self.db.commit()
        app = FastAPI()
        app.include_router(similar.router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.db.get(TentacleUser, 1)
        self.client = TestClient(app)

    def test_ids_and_tentacles_own_tags(self):
        from models.database import Movie
        self.db.add_all([Movie(tmdb_id=862, title="Toy Story", source="vod", jellyfin_item_id="AAAA-1"),
                         Movie(tmdb_id=585, title="Monsters, Inc.", source="vod"),
                         Movie(tmdb_id=999, title="Not related", source="vod", jellyfin_item_id="cccc3")])
        self.db.commit()
        rel = {"collection": [862], "recommendations": [585], "similar": []}
        with mock.patch.object(TMDBService, "get_related", return_value=rel):
            r = self.client.get("/api/similar/movie/863")
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        # The library's copies of the related titles, as Jellyfin ids without dashes.
        self.assertEqual({"862": ["aaaa1"]}, body["library"])
        self.assertTrue(body["tmdb_ok"])
        self.assertEqual([862], body["collection"])
        self.assertIn("Recently Added Movies", body["tentacle_tags"])
        self.assertIn("Oscar Winners", body["tentacle_tags"])

    def test_tmdb_down_still_answers_with_the_tags(self):
        with mock.patch.object(TMDBService, "get_related", side_effect=TMDBConnectionError("down")):
            body = self.client.get("/api/similar/tv/1396").json()
        self.assertFalse(body["tmdb_ok"])
        self.assertEqual([], body["recommendations"])
        self.assertIn("Recently Added TV", body["tentacle_tags"])

    def test_only_movie_or_tv(self):
        self.assertEqual(400, self.client.get("/api/similar/person/1").status_code)


if __name__ == "__main__":
    unittest.main()
