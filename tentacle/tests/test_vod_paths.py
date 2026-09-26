"""Where the VOD sync writes a title, and what its .strm plays (#154, #155, #156).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #156: a folder name was capped at 200 CHARACTERS; a path component is 255
  BYTES, and 200 characters of CJK is 600 bytes: ENAMETOOLONG on every sync.
- #155: two titles mapping to one folder (namesakes, or names that sanitize to
  the same string) shared one .strm/.nfo, and pruning one deleted the other.
- #154: a higher-priority provider took the row over but the .strm kept
  playing the lower-priority provider; when that one expired, nothing repaired it.
Runs the real sync_provider against a fake provider (tests/nightly_harness.py).
"""
import unittest
from pathlib import Path as _RealPath

import services.sync as sync
from models.database import Movie, Provider, ProviderCategory, Series
from nightly_harness import FakeClient, FakeTMDB, NightlyHarness


class LongNames(NightlyHarness):
    def test_a_title_over_255_bytes_imports(self):
        title = "アニメ" * 30
        self.add_category("1")
        self.catalogue_movies("1", [title])
        self.sync_only()
        row = self.movie(1000)
        self.assertIsNotNone(row, "the title never imported")
        strm = _RealPath(row.strm_path)
        self.assertTrue(strm.exists())
        self.assertLessEqual(len(strm.name.encode()), 255)
        self.assertLessEqual(len(strm.parent.name.encode()), 255)

    def test_a_long_series_writes_episode_files_that_fit(self):
        title = "长篇连续剧的名字" * 15
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [title])
        self.sync_only()
        row = self.series_row(5000)
        self.assertIsNotNone(row)
        show = _RealPath(row.strm_path)
        eps = list(show.rglob("*.strm"))
        self.assertEqual(1, len(eps))
        self.assertLessEqual(len(eps[0].name.encode()), 255)
        self.assertTrue(eps[0].name.endswith(" S01E01.strm"), eps[0].name)

    def test_a_name_that_fits_is_unchanged(self):
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.sync_only()
        self.assertTrue(self.movie(1000).strm_path.endswith("/Heat (2010)/Heat (2010).strm"))


class LeadingBracket(unittest.TestCase):
    def test_a_title_starting_with_a_bracket_keeps_its_name(self):
        """The old guard for an empty title tested the whole name for a leading
        "(", so "(500) Days of Summer" was written as "Unknown (2009)"."""
        from services.nfo import vod_folder_name
        self.assertEqual("(500) Days of Summer (2009)", vod_folder_name("(500) Days of Summer", "2009"))
        self.assertEqual("Unknown (2009)", vod_folder_name("...", "2009"))


class FolderCollisions(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1")
        # Two different titles that sanitize to one folder name.
        self.client.movies["1"] = [("Brothers?", 11), ("Brothers", 12)]
        FakeTMDB.ids.update({"Brothers?": 2001, "Brothers": 2002})
        self.sync_only()

    def _file(self, row):
        return _RealPath(row.strm_path)

    def test_each_title_gets_its_own_files(self):
        a, b = self.movie(2001), self.movie(2002)
        self.assertNotEqual(a.strm_path, b.strm_path)
        self.assertIn("[tmdbid-2002]", b.strm_path)
        self.assertTrue(self._file(a).read_text().endswith("/11.mp4"))
        self.assertTrue(self._file(b).read_text().endswith("/12.mp4"))

    def test_pruning_one_keeps_the_others_files_even_when_they_were_shared(self):
        """Rows written before the fix point at ONE file: pruning one must not delete it."""
        a, b = self.movie(2001), self.movie(2002)
        b.strm_path, b.nfo_path = a.strm_path, a.nfo_path   # the old, shared layout
        self.db.commit()
        self.client.movies["1"] = [("Brothers?", 11)]      # the provider drops "Brothers"
        self.sync_only()                                    # first strike: marked
        self.sync_only()                                    # second: removed
        self.assertIsNone(self.movie(2002))
        self.assertTrue(self._file(self.movie(2001)).exists(), "the other title's files were deleted")


class PriorityTakeover(NightlyHarness):
    def test_the_strm_follows_the_higher_priority_provider(self):
        self.provider.priority = 2
        self.db.commit()
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.sync_only()
        self.assertTrue(self._strm_text(1000).startswith("http://provider/"))

        high = Provider(name="High", server_url="http://high", username="u", password="p", active=True, priority=1)
        self.db.add(high)
        self.db.commit()
        self.db.add(ProviderCategory(provider_id=high.id, category_id="h1", category_name="H", type="movie",
                                     whitelisted=True, source_tag="HighTag"))
        self.db.commit()

        class HighClient(FakeClient):
            def movie_stream_url(self, stream_id, ext):
                return f"http://high/movie/u/p/{stream_id}.{ext}"
        high_client = HighClient(movies={"h1": [("Heat", 77)]})
        low_client = self.client
        sync.make_provider_client = lambda p: high_client if p.id == high.id else low_client
        run = sync.sync_provider(high, "full", self.db)
        self.assertEqual("completed", run.status, run.error_message)
        self.db.expire_all()
        row = self.movie(1000)
        self.assertEqual(high.id, row.provider_id)
        self.assertEqual("http://high/movie/u/p/77.mp4", self._strm_text(1000))
        self.assertIn("HighTag Movies", row.tags)
        self.assertNotIn("Tag1 Movies", row.tags)

    def _strm_text(self, tmdb_id):
        row = self.movie(tmdb_id)
        return _RealPath(row.strm_path).read_text().strip()


if __name__ == "__main__":
    unittest.main()
