"""A provider year one off from TMDB's still matches (#265).

search_movie()/search_series() said "try with year, then without", but broke
out after the first pass whenever a year was given, and with no year sent the
same query twice. A film labelled with its local or streaming year
("Dune (2020)" for TMDB's 2021 Dune) was skipped on every sync. The year-less
retry accepts only a result within one year of the provider's, so a remake
decades apart (Heat 1986 vs Heat 1995) never takes its place.
"""
import logging
import tempfile
import unittest

from services.tmdb import TMDBService

DUNE = {"id": 438631, "title": "Dune", "release_date": "2021-09-15", "popularity": 90}
OFFICE = {"id": 2316, "name": "The Office", "first_air_date": "2005-03-24", "popularity": 90}
HEAT = {"id": 949, "title": "Heat", "release_date": "1995-12-15", "popularity": 60}


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class Fake(TMDBService):
    """TMDB whose year filter finds nothing for a year the title wasn't released in."""

    def __init__(self, cache_dir, movies=(DUNE,), shows=(OFFICE,)):
        super().__init__("token", cache_dir)
        self.calls = []
        self.movies, self.shows = list(movies), list(shows)

    def _request(self, endpoint, params=None):
        params = dict(params or {})
        self.calls.append((endpoint, params))
        if endpoint == "search/movie":
            y = params.get("year")
            return {"results": [m for m in self.movies if not y or m["release_date"].startswith(str(y))]}
        if endpoint == "search/tv":
            y = params.get("first_air_date_year")
            return {"results": [s for s in self.shows if not y or s["first_air_date"].startswith(str(y))]}
        return None

    def get_movie_details(self, i, **k):
        m = next(m for m in self.movies if m["id"] == i)
        return {"tmdb_id": i, "title": m["title"], "year": m["release_date"][:4]}

    def get_series_details(self, i, **k):
        s = next(s for s in self.shows if s["id"] == i)
        return {"tmdb_id": i, "title": s["name"], "year": s["first_air_date"][:4]}


class YearFallback(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name

    def test_a_film_a_year_early_is_found(self):
        tmdb = Fake(self.dir)
        found = tmdb.search_movie("Dune", "2020", strict=True)
        self.assertIsNotNone(found, tmdb.calls)
        self.assertEqual(438631, found["tmdb_id"])
        self.assertEqual([("search/movie", {"query": "Dune", "year": "2020"}),
                          ("search/movie", {"query": "Dune"})], tmdb.calls)

    def test_a_show_a_year_early_is_found(self):
        tmdb = Fake(self.dir)
        found = tmdb.search_series("The Office", "2004", strict=True)
        self.assertIsNotNone(found, tmdb.calls)
        self.assertEqual(2316, found["tmdb_id"])

    def test_the_exact_year_needs_one_search(self):
        tmdb = Fake(self.dir)
        self.assertEqual(438631, tmdb.search_movie("Dune", "2021")["tmdb_id"])
        self.assertEqual(1, len(tmdb.calls))

    def test_no_year_and_no_match_is_one_search(self):
        tmdb = Fake(self.dir, movies=())
        self.assertIsNone(tmdb.search_movie("Nothing Like It"))
        self.assertEqual(1, len(tmdb.calls))

    def test_a_namesake_years_apart_is_not_taken(self):
        tmdb = Fake(self.dir, movies=(HEAT,))
        self.assertIsNone(tmdb.search_movie("Heat", "1986"))
        tmdb = Fake(self.dir + "/2", shows=(OFFICE,))
        self.assertIsNone(tmdb.search_series("The Office", "2001"))


if __name__ == "__main__":
    unittest.main()
