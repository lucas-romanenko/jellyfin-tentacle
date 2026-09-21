"""A provider title the cleaner mangles is pruned as "removed by the provider".

The nightly sync builds ``seen_ids`` from what ``clean_title()`` produced. A
title the cleaner truncates ("Top Gun" -> "Gun") no longer resolves to the row
it created, and a title the cleaner rejects ("Max (2015)" -> None) is skipped
before ``seen_ids`` is touched at all. Either way the row looks removed by the
provider, so the prune marks it on one night and deletes it on the next —
while the provider is still listing the title every single night.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from models.database import Movie
from nightly_harness import NightlyHarness, FakeTMDB


class RawNameClient:
    """Xtream client that returns the provider's names verbatim."""

    def __init__(self):
        self.movies = {}     # cat_id -> [(raw_name, stream_id)]
        self.series = {}

    def get_vod_streams(self, cat_id):
        return [{"name": raw, "stream_id": sid, "container_extension": "mp4"}
                for raw, sid in self.movies.get(cat_id, [])]

    def movie_stream_url(self, stream_id, ext):
        return f"http://provider/movie/u/p/{stream_id}.{ext}"

    def get_series_list(self, cat_id):
        return []

    def get_series_info(self, series_id):
        return {"episodes": {}}

    def episode_stream_url(self, ep_id, ext):
        return f"http://provider/series/u/p/{ep_id}.{ext}"


class TestMangledTitleIsPruned(NightlyHarness):
    def setUp(self):
        super().setUp()
        import services.sync as sync
        self.client = RawNameClient()
        sync.make_provider_client = lambda provider: self.client
        self.cat = self.add_category("c1", "movie")
        # TMDB knows both films by their real names.
        FakeTMDB.ids = {"Top Gun": 1001, "Max": 1002, "Other Film": 1003}

    def _list(self, names):
        self.client.movies["c1"] = [(n, 500 + i) for i, n in enumerate(names)]

    def test_untagged_provider_names_prune_their_own_rows(self):
        # Night 1: the category still carries the "EN - " tags, so both titles
        # are matched and imported.
        self._list(["EN - Top Gun (1986)", "EN - Max (2015)", "EN - Other Film (2001)"])
        self.night()
        self.assertIsNotNone(self.movie(1001), "Top Gun was not imported")
        self.assertIsNotNone(self.movie(1002), "Max was not imported")
        self.assertIsNotNone(self.movie(1003), "Other Film was not imported")

        # Nights 2 and 3: the provider drops the tags. Nothing else changed —
        # both films are still listed in the same category every night.
        # (a third title keeps its tag, so the category is plainly readable and
        # the prune runs as it does on any normal night)
        self._list(["Top Gun (1986)", "Max (2015)", "EN - Other Film (2001)"])
        self.night()
        self.night()

        still_here = {m.tmdb_id for m in self.db.query(Movie).all()}
        self.assertIn(1001, still_here,
                      "'Top Gun (1986)' was cleaned to 'Gun' and its row was pruned")
        self.assertIn(1002, still_here,
                      "'Max (2015)' was cleaned to nothing and its row was pruned")


if __name__ == "__main__":
    unittest.main()
