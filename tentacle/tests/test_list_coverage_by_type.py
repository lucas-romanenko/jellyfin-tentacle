"""#264 follow-up: a list's coverage (the card's "Add N Missing" buttons and the
coverage view) must count what Add Missing sends. TMDB numbers movies and shows
separately, so a list's film is looked up only among movies and its show only
among series; a film whose number a library show also has is still missing.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import random
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


class _Lists(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = mdb.TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

    def fill(self, items, movies=(), series=()):
        import models.database as mdb
        self.db.query(mdb.ListItem).delete()
        self.db.query(mdb.Movie).delete()
        self.db.query(mdb.Series).delete()
        self.db.query(mdb.ListSubscription).delete()
        self.db.add(mdb.ListSubscription(id=1, user_id=1, name="Mixed", type="trakt",
                                         url="https://example.invalid/list", tag="Mixed"))
        for tid, media_type, title in items:
            self.db.add(mdb.ListItem(list_id=1, tmdb_id=tid, media_type=media_type, title=title))
        for tid, source in movies:
            self.db.add(mdb.Movie(tmdb_id=tid, title=f"Film {tid}", source=source))
        for tid, source in series:
            self.db.add(mdb.Series(tmdb_id=tid, title=f"Show {tid}", source=source))
        self.db.commit()

    def coverage(self):
        import routers.lists as lists
        return lists.get_list_coverage(1, self.db, self.user)

    def sent(self):
        import routers.lists as lists
        out = {}
        for fn, request in (("add_missing_to_radarr", "request_movies"), ("add_missing_to_sonarr", "request_series")):
            outcome = mock.Mock()
            outcome.as_response.return_value = {}
            with mock.patch.object(lists.media_requests, request, return_value=outcome) as req:
                getattr(lists, fn)(1, None, self.db, self.user)
            args, kwargs = req.call_args
            out[fn] = list(kwargs.get("tmdb_ids", args[1] if len(args) > 1 else None))
        return out["add_missing_to_radarr"], out["add_missing_to_sonarr"]


class TestCoverageByType(_Lists):
    def test_a_film_whose_number_a_library_show_has_is_missing(self):
        self.fill([(1396, "movie", "A film"), (604, "movie", "Another film")], series=[(1396, "sonarr")])
        cov = self.coverage()
        self.assertEqual(cov["missing_movies"], 2)
        self.assertEqual(cov["sonarr"], [])
        self.assertEqual(self.sent()[0], [1396, 604])

    def test_a_show_whose_number_a_library_film_has_is_missing(self):
        self.fill([(603, "series", "A show"), (1399, "series", "Another show")], movies=[(603, "radarr")])
        cov = self.coverage()
        self.assertEqual(cov["missing_series"], 2)
        self.assertEqual(cov["radarr"], [])
        self.assertEqual(self.sent()[1], [603, 1399])

    def test_an_item_without_a_tmdb_id_is_listed_but_not_counted(self):
        self.fill([(603, "movie", "The Matrix"), (None, "movie", "IMDb only")])
        cov = self.coverage()
        self.assertEqual(len(cov["missing"]), 2)
        self.assertEqual(cov["missing_movies"], 1)
        self.assertEqual(self.sent()[0], [603])

    def test_titles_in_the_library_are_grouped_as_before(self):
        self.fill([(603, "movie", "The Matrix"), (1396, "series", "Breaking Bad"), (77, None, "Old row")],
                  movies=[(603, "radarr"), (77, "provider_1")], series=[(1396, "sonarr")])
        cov = self.coverage()
        self.assertEqual([e["tmdb_id"] for e in cov["radarr"]], [603])
        self.assertEqual([e["tmdb_id"] for e in cov["sonarr"]], [1396])
        self.assertEqual([e["tmdb_id"] for e in cov["vod"]], [77])
        self.assertEqual((cov["missing_movies"], cov["missing_series"]), (0, 0))


class TestCoverageCountsWhatIsSent(_Lists):
    def test_random_lists(self):
        seeds = int(os.environ.get("GM_SEEDS", "1000"))
        base = int(os.environ.get("GM_SEED", "20260929"))
        bad = []
        for n in range(seeds):
            rnd = random.Random(base + n)
            ids = rnd.sample(range(1, 40), rnd.randint(1, 20))   # a narrow range: numbers collide
            items = [(tid, rnd.choice(["movie", "series", None]), f"T{tid}") for tid in ids]
            if rnd.random() < 0.2:
                items.append((None, "movie", "IMDb only"))
            movies = [(t, rnd.choice(["radarr", "provider_1"])) for t in rnd.sample(range(1, 40), rnd.randint(0, 12))]
            series = [(t, rnd.choice(["sonarr", "provider_1"])) for t in rnd.sample(range(1, 40), rnd.randint(0, 12))]
            self.fill(items, movies, series)
            cov = self.coverage()
            radarr, sonarr = self.sent()
            if (cov["missing_movies"], cov["missing_series"]) != (len(radarr), len(sonarr)):
                bad.append((base + n, cov["missing_movies"], len(radarr), cov["missing_series"], len(sonarr)))
            if len(cov["vod"]) + len(cov["radarr"]) + len(cov["sonarr"]) + len(cov["missing"]) != len(items):
                bad.append((base + n, "an item is in no group or in two"))
        print(f"\n[coverage] {seeds} seeds from {base}: failures={len(bad)} {bad[:3]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
