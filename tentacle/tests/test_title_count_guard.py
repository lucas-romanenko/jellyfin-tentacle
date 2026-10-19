"""A TMDB outage or a provider blip must not disarm the empty-category guard (#25).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A category's title_count is also the sync's memory that it holds titles: a
category that held titles and suddenly returns none is treated as a provider
blip for EMPTY_CATEGORY_STRIKES syncs, and nothing is pruned. The sync set the
count from what matched TMDB that night, so a night on which every lookup in a
category failed set it to 0, and "Fetch categories" wrote the provider's raw
counts, 0 everywhere when the provider answered with nothing. Either way the
guard no longer held, and two empty answers later every title in the category
was deleted (Rob's follow-up on #25: 8 of 8).
"""
import unittest
from unittest import mock

import services.sync as sync
from models.database import Movie, ProviderCategory
from nightly_harness import FakeTMDB, NightlyHarness
from services.exceptions import TMDBConnectionError


class TmdbDown(FakeTMDB):
    """Every live lookup fails the way a TMDB outage does (strict lookups raise)."""
    down = False

    def search_movie(self, name, year=None, **kwargs):
        if TmdbDown.down:
            raise TMDBConnectionError("TMDB lookup did not complete")
        return super().search_movie(name, year, **kwargs)


class GuardSurvivesATmdbOutage(NightlyHarness):
    def setUp(self):
        super().setUp()
        sync.TMDBService = TmdbDown
        TmdbDown.down = False
        self.add_category("1")
        self.titles = [f"Film {i}" for i in range(8)]
        self.catalogue_movies("1", self.titles)
        # Another category that keeps answering: a provider that returns nothing
        # at all is caught by a guard of its own.
        self.add_category("2")
        self.catalogue_movies("2", [f"Other {i}" for i in range(8)], first_tmdb=2000)
        self.sync_only()

    def _count(self):
        return self.db.query(ProviderCategory).filter_by(category_id="1").one().title_count

    def test_a_night_when_every_lookup_fails_keeps_the_count(self):
        self.assertEqual(8, self._count())
        # The rows carry TMDB's official titles, not the provider's names, so
        # every stream needs a live lookup; TMDB is down that night.
        for row in self.db.query(Movie).filter(Movie.tmdb_id < 2000):
            row.title = f"{row.title} (official)"
        self.db.commit()
        TmdbDown.down = True
        self.sync_only()
        self.assertEqual(8, self._count())

    def test_two_empty_answers_after_the_outage_prune_nothing(self):
        for row in self.db.query(Movie).filter(Movie.tmdb_id < 2000):
            row.title = f"{row.title} (official)"
        self.db.commit()
        TmdbDown.down = True
        self.sync_only()
        TmdbDown.down = False
        self.client.movies["1"] = []          # the provider blips twice
        self.sync_only()
        self.sync_only()
        left = self.db.query(Movie).filter(Movie.tmdb_id < 2000).count()
        self.assertEqual(8, left, "a provider blip after a TMDB outage deleted titles")

    def test_a_night_without_failures_still_records_a_real_shrink(self):
        self.client.movies["1"] = [(t, 1000 + i) for i, t in enumerate(self.titles[:5])]
        self.sync_only()
        self.assertEqual(5, self._count())


class FetchCategoriesKeepsTheCount(NightlyHarness):
    def _fetch(self, vod_counts):
        from routers import providers
        cats = [{"category_id": "1", "category_name": "CAT 1"}, {"category_id": "2", "category_name": "CAT 2"}]
        with mock.patch.object(providers, "fetch_provider_categories",
                               lambda p: (cats, [], vod_counts, {})), \
                mock.patch("services.provider_activity.refuse_while_recording", lambda db, what: None):
            providers.fetch_categories(self.provider.id, db=self.db)
        self.db.expire_all()
        return {c.category_id: c.title_count for c in self.db.query(ProviderCategory)}

    def test_a_provider_answer_with_no_titles_at_all_keeps_the_counts(self):
        self.add_category("1").title_count = 8
        self.db.commit()
        counts = self._fetch({})
        self.assertEqual(8, counts["1"])
        self.assertEqual(0, counts["2"], "a new category starts at 0")

    def test_real_counts_are_still_written(self):
        self.add_category("1").title_count = 8
        self.db.commit()
        self.assertEqual(3, self._fetch({"1": 3, "2": 4})["1"])
        self.assertEqual(0, self._fetch({"2": 4})["1"], "a category that really emptied, while others have titles")


if __name__ == "__main__":
    unittest.main()
