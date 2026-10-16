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


if __name__ == "__main__":
    unittest.main()
