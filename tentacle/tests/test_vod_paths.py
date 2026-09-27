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
    """A higher-priority provider takes a title over: its files must play that
    provider from then on, in place (same paths, same Jellyfin items) (#154)."""

    def setUp(self):
        super().setUp()
        self.provider.priority = 2
        self.db.commit()
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", ["Friends"])
        self.high = Provider(name="High", server_url="http://high", username="u", password="p",
                             active=True, priority=1)
        self.db.add(self.high)
        self.db.commit()
        for cat_id, type_ in (("h1", "movie"), ("hs1", "series")):
            self.db.add(ProviderCategory(provider_id=self.high.id, category_id=cat_id, category_name=cat_id,
                                         type=type_, whitelisted=True, source_tag="HighTag"))
        self.db.commit()

        class HighClient(FakeClient):
            def movie_stream_url(self, stream_id, ext):
                return f"http://high/movie/u/p/{stream_id}.{ext}"

            def episode_stream_url(self, ep_id, ext):
                return f"http://high/series/u/p/{ep_id}.{ext}"
        high_client = HighClient(movies={"h1": [("Heat", 77)]}, series={"hs1": [("Friends", 88)]})
        low_client = self.client
        sync.make_provider_client = lambda p: high_client if p.id == self.high.id else low_client

    def _sync(self, provider):
        run = sync.sync_provider(provider, "full", self.db)
        self.assertEqual("completed", run.status, run.error_message)
        self.db.expire_all()

    def _strm(self):
        return _RealPath(self.movie(1000).strm_path).read_text().strip()

    def _episodes(self):
        show = _RealPath(self.series_row(5000).strm_path)
        return {p.relative_to(show).as_posix(): p.read_text().strip() for p in show.rglob("*.strm")}

    def test_the_strm_follows_the_higher_priority_provider_and_stays(self):
        self._sync(self.provider)
        self.assertEqual("http://provider/movie/u/p/1000.mp4", self._strm())
        for provider in (self.high, self.provider, self.high, self.provider):
            self._sync(provider)
            self.assertEqual("http://high/movie/u/p/77.mp4", self._strm(), provider.name)
        self.assertEqual(self.high.id, self.movie(1000).provider_id)

    def test_a_title_taken_over_before_this_fix_is_healed(self):
        self._sync(self.provider)
        row = self.movie(1000)                       # the old code: row moved, file not
        row.provider_id, row.source = self.high.id, f"provider_{self.high.id}"
        self.db.commit()
        self._sync(self.high)
        self.assertEqual("http://high/movie/u/p/77.mp4", self._strm())

    def test_a_hand_made_url_is_left_alone(self):
        self._sync(self.provider)
        self._sync(self.high)
        path = _RealPath(self.movie(1000).strm_path)
        path.write_text("http://my-proxy.lan:8080/play?id=77", encoding="utf-8")
        self._sync(self.high)
        self.assertEqual("http://my-proxy.lan:8080/play?id=77", self._strm())

    def test_a_series_takeover_rewrites_its_episodes_in_place(self):
        self._sync(self.provider)
        before = self._episodes()
        self.assertTrue(all(url.startswith("http://provider/series/") for url in before.values()))
        self._sync(self.high)
        after = self._episodes()
        self.assertEqual(set(before), set(after), "no episode file was added, moved or renamed")
        self.assertTrue(all(url.startswith("http://high/series/") for url in after.values()), after)

    def test_tags_are_left_alone(self):
        """The old provider still offers the title: its source playlist keeps it.
        The new provider's tag is merged in on its next pass, like any title's."""
        self._sync(self.provider)
        self._sync(self.high)
        self.assertIn("Tag1 Movies", self.movie(1000).tags)
        self._sync(self.high)
        self.assertEqual({"Tag1 Movies", "HighTag Movies"},
                         {t for t in self.movie(1000).tags if t.endswith(" Movies") and "Recently" not in t})


class SecondCategoryTag(NightlyHarness):
    def test_a_title_listed_in_another_category_later_gets_its_tag(self):
        """_merge_source_tag appended to the loaded list in place, so assigning
        it back looked like no change: the tag was never written."""
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.sync_only()
        self.add_category("2")
        self.client.movies["2"] = [("Heat", 1000)]
        self.sync_only()
        self.assertIn("Tag2 Movies", self.movie(1000).tags)


if __name__ == "__main__":
    unittest.main()
