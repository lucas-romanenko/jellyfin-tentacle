"""A title the provider still lists is never pruned because its label changed (#621).

The provider relabels a title it still lists (same stream_id / series_id). If
the new label no longer resolves, the title is not "seen", and the two-strike
prune deletes it on the second sync. #262 covers films whose new label is a
clean name TMDB can't match (_place_relisted_movies). Still open: a film whose
new label the title cleaner rejects, and every series (no counterpart at all).

Reported by therobmilne with this reproduction; runs the real sync_provider +
sweep_orphaned_vod_records nights from nightly_harness (fake provider, TMDB).
"""
import unittest
from pathlib import Path

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from nightly_harness import NightlyHarness, FakeTMDB  # noqa: E402
from services.cleaner import clean_title  # noqa: E402


class RelabelledMovie(NightlyHarness):
    def _relabel(self, new_label):
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Movie 13"]
        self.assertIsNotNone(self.movie(tmdb))
        # Same stream id, new label; the provider still lists it every night.
        self.client.movies["1"] = [((new_label if t == "Movie 13" else t), sid)
                                   for t, sid in self.client.movies["1"]]
        for _ in range(3):
            self.night()
        return self.movie(tmdb)

    def test_clean_label_tmdb_cannot_match_is_kept(self):
        """#262: kept, its .strm plays the listed stream."""
        self.assertIsNotNone(self._relabel("Unmatched Title"))

    def test_label_the_cleaner_rejects_is_kept(self):
        self.assertEqual(clean_title("EN - X (2010)"), (None, None))
        self.assertIsNotNone(self._relabel("EN - X"),
                             "film pruned although its stream is still listed (label rejected by the cleaner)")


class RelabelledSeries(NightlyHarness):
    def _relabel(self, new_label):
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Show 13"]
        self.assertIsNotNone(self.series_row(tmdb))
        self.client.series["s1"] = [((new_label if t == "Show 13" else t), sid)
                                    for t, sid in self.client.series["s1"]]
        for _ in range(3):
            self.night()
        return self.series_row(tmdb)

    def test_unchanged_label_is_kept(self):
        """Control."""
        self.assertIsNotNone(self._relabel("Show 13"))

    def test_clean_label_tmdb_cannot_match_is_kept(self):
        self.assertIsNotNone(self._relabel("Unmatched Title"),
                             "series pruned although the same series id is still listed (new label, no TMDB match)")

    def test_label_the_cleaner_rejects_is_kept(self):
        self.assertIsNotNone(self._relabel("EN - X"),
                             "series pruned although the same series id is still listed (label rejected by the cleaner)")

    def test_listing_ids_are_recorded(self):
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", ["Show 1", "Show 2"])
        self.night()
        for title, sid in self.client.series["s1"]:
            self.assertEqual(self.series_row(FakeTMDB.ids[title]).provider_series_ids, [str(sid)])

    def test_relabelled_show_is_back_filled(self):
        """Kept as that show: a new episode of the relabelled listing is written."""
        sid = 5000 + 13
        base = self.client.get_series_info

        def info(series_id):
            got = base(series_id)
            if series_id == sid:
                got["episodes"]["1"].append({"id": series_id * 10 + 2, "episode_num": 2,
                                             "container_extension": "mp4"})
            return got
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.night()
        self.client.series["s1"] = [(("Unmatched Title" if t == "Show 13" else t), s)
                                    for t, s in self.client.series["s1"]]
        self.client.get_series_info = info
        self.night()
        row = self.series_row(FakeTMDB.ids["Show 13"])
        self.assertEqual(len(list(Path(row.strm_path).glob("Season */*.strm"))), 2)

    def test_reused_id_naming_another_show_is_pruned(self):
        """The provider's id now lists other episodes under a label that doesn't
        resolve: not that show; pruned as before, and its files not rewritten."""
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.night()
        tmdb, sid = FakeTMDB.ids["Show 13"], 5000 + 13
        files = list(Path(self.series_row(tmdb).strm_path).glob("Season */*.strm"))
        before = {f: f.read_text() for f in files}
        self.assertTrue(before)
        base = self.client.get_series_info
        self.client.get_series_info = lambda s: (
            {"episodes": {"1": [{"id": 999001, "episode_num": 1, "container_extension": "mp4"}]}}
            if s == sid else base(s))
        self.client.series["s1"] = [(("Unmatched Title" if t == "Show 13" else t), s)
                                    for t, s in self.client.series["s1"]]
        self.night()
        self.assertEqual({f: f.read_text() for f in files if f.exists()}, before)
        for _ in range(2):
            self.night()
        self.assertIsNone(self.series_row(tmdb))

    def test_no_files_to_compare_is_kept(self):
        """Opted out of .strm files: nothing to tell by, kept on doubt."""
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Show 13"]
        row = self.series_row(tmdb)
        row.strm_disabled = True
        self.db.commit()
        self.client.series["s1"] = [(("EN - X" if t == "Show 13" else t), s)
                                    for t, s in self.client.series["s1"]]
        for _ in range(3):
            self.night()
        self.assertIsNotNone(self.series_row(tmdb))


if __name__ == "__main__":
    unittest.main()
