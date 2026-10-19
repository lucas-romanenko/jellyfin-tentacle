"""#273: TMDB unreachable at the connection level (DNS, refused, reset) must
not fail the whole Discover page. The TMDB sections that could be read and
"From My Lists" (Tentacle's own database) still show, with a warning; the
single-item Discover routes answer 503 "TMDB could not be reached" instead of
a bare 500.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from services.exceptions import TMDBConnectionError

ITEM = {"tmdb_id": 1, "title": "X", "media_type": "movie", "poster_path": "/x.jpg"}
LISTED = {"tmdb_id": 2, "title": "From a list", "media_type": "movie"}


def _down(*a, **k):
    raise TMDBConnectionError("Cannot reach TMDB API: RemoteDisconnected")


class _Tmdb:
    def get_popular(self, t): return [dict(ITEM)]
    def get_now_playing(self): return _down()
    def get_upcoming(self): return []
    def get_on_the_air(self): return _down()
    def get_top_rated(self, t): return [dict(ITEM, tmdb_id=3)]


class TestDiscoverPage(unittest.TestCase):
    def _run(self, type_, tmdb=None):
        import routers.discover as disc
        with mock.patch.object(disc, "_get_tmdb", return_value=tmdb or _Tmdb()), \
             mock.patch.object(disc, "_known_tmdb_ids", return_value={"movie": set(), "series": set()}), \
             mock.patch.object(disc, "_get_missing_from_lists", return_value=[dict(LISTED)]), \
             mock.patch.object(disc, "_mark_requested", side_effect=lambda i: i), \
             mock.patch.object(disc, "_is_in_library", return_value=False):
            return disc.get_discover(type=type_, db=None, user=None)

    def test_movies_keep_the_sections_that_answered_and_my_lists(self):
        out = self._run("movies")
        ids = [s["id"] for s in out["sections"]]
        self.assertIn("popular", ids)
        self.assertIn("missing", ids)
        self.assertNotIn("now_playing", ids)
        self.assertIn("TMDB", out.get("warning", ""))

    def test_series_keep_the_sections_that_answered_and_my_lists(self):
        out = self._run("series")
        ids = [s["id"] for s in out["sections"]]
        self.assertEqual(ids, ["popular", "top_rated", "missing"])
        self.assertIn("TMDB", out.get("warning", ""))

    def test_no_warning_when_tmdb_answers(self):
        class Up(_Tmdb):
            def get_now_playing(self): return []
        out = self._run("movies", Up())
        self.assertNotIn("warning", out)


class TestDiscoverDoesNotWaitOnEachSection(unittest.TestCase):
    """A TMDB that can't be connected to, or doesn't answer, costs Discover one
    timeout, not one per section: at 10 s each, three sections took 30 s, past
    the plugin's 15 s, so Jellyfin's Discover tab still showed "unavailable".
    Sections already cached still show. The sync is not changed (#26)."""

    def _svc(self):
        from services.tmdb import TMDBService
        from tmp_dirs import temp_dir
        return TMDBService("token", temp_dir(self))

    def _discover(self, svc, type_="movies"):
        import routers.discover as disc
        with mock.patch.object(disc, "_get_tmdb", return_value=svc), \
             mock.patch.object(disc, "_known_tmdb_ids", return_value={"movie": set(), "series": set()}), \
             mock.patch.object(disc, "_get_missing_from_lists", return_value=[dict(LISTED)]), \
             mock.patch.object(disc, "_mark_requested", side_effect=lambda i: i), \
             mock.patch.object(disc, "_is_in_library", return_value=False):
            return disc.get_discover(type=type_, db=None, user=None)

    def _check(self, exc, type_, cached_key, cached_id):
        import requests
        svc = self._svc()
        svc._cache_set(cached_key, [dict(ITEM, tmdb_id=9)])
        with mock.patch.object(svc.session, "get", side_effect=exc("timed out")) as get:
            out = self._discover(svc, type_)
        self.assertEqual(1, get.call_count, "one timeout per page load, not one per section")
        self.assertEqual([cached_id, "missing"], [s["id"] for s in out["sections"]])
        self.assertIn("TMDB", out.get("warning", ""))

    def test_connect_timeout_movies(self):
        import requests
        self._check(requests.ConnectTimeout, "movies", "upcoming_v4:5", "upcoming")

    def test_read_timeout_movies(self):
        import requests
        self._check(requests.ReadTimeout, "movies", "upcoming_v4:5", "upcoming")

    def test_connect_timeout_series(self):
        import requests
        self._check(requests.ConnectTimeout, "series", "popular:series:5", "popular")

    def test_the_next_page_load_tries_tmdb_again(self):
        import requests
        svc1, svc2 = self._svc(), self._svc()
        with mock.patch.object(svc1.session, "get", side_effect=requests.ConnectTimeout("x")):
            self._discover(svc1)
        with mock.patch.object(svc2.session, "get", side_effect=requests.ConnectTimeout("x")) as get:
            self._discover(svc2)
        self.assertEqual(1, get.call_count)

    def test_other_callers_are_unchanged(self):
        # The sync and every other caller: a read timeout is still "no answer"
        # (None), and every connection failure is still raised, each time.
        import requests
        svc = self._svc()
        with mock.patch.object(svc.session, "get", side_effect=requests.ReadTimeout("x")) as get:
            self.assertIsNone(svc._request("movie/popular"))
            self.assertIsNone(svc._request("movie/popular"))
        self.assertEqual(2, get.call_count)
        with mock.patch.object(svc.session, "get", side_effect=requests.ConnectTimeout("x")) as get:
            for _ in range(2):
                with self.assertRaises(TMDBConnectionError):
                    svc._request("movie/popular")
        self.assertEqual(2, get.call_count)


class TestSingleItemRoutes(unittest.TestCase):
    def test_detail_answers_503_when_tmdb_is_unreachable(self):
        import main
        import models.database as mdb
        from routers import auth
        from fastapi.testclient import TestClient
        import routers.discover as disc
        tmdb = mock.Mock()
        tmdb.get_movie_details.side_effect = _down
        tmdb.get_genres.side_effect = _down
        main.app.dependency_overrides[auth.get_user_from_request] = lambda: mock.Mock(id=1, is_admin=True)
        main.app.dependency_overrides[mdb.get_db] = lambda: iter([mock.Mock()])
        try:
            with mock.patch.object(disc, "_get_tmdb", return_value=tmdb):
                c = TestClient(main.app, raise_server_exceptions=False)
                for path in ("/api/discover/detail/movie/603", "/api/discover/genres"):
                    with self.subTest(path=path):
                        r = c.get(path)
                        self.assertEqual(r.status_code, 503)
                        self.assertIn("TMDB could not be reached", r.json()["detail"])
        finally:
            main.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
