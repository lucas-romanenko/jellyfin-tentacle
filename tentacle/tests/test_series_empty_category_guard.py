"""Pins the call to _category_went_empty inside _sync_series.

test_deletion_guard_wiring pins the movie call site only. With the series call
removed (`if _category_went_empty(db, cat, len(series_list)):` -> `if False:`)
the whole suite stays green. A small series category that answers [] on two
syncs in a row then has every series in it pruned (the prune cap does not stop
a handful), although the provider removed nothing: #16, for series.
Run from tentacle/:  python tests/hermetic.py tests/test_series_empty_category_guard.py
"""
import unittest

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from models.database import Series  # noqa: E402
from nightly_harness import NightlyHarness  # noqa: E402


class SeriesCategoryOutage(NightlyHarness):
    def test_series_category_empty_on_two_syncs_prunes_nothing(self):
        self.add_category("s1", type_="series", name="EN - SERIES")
        self.add_category("s2", type_="series", name="EN - KIDS")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.catalogue_series("s2", [f"Kids {i}" for i in range(3)], first_tmdb=9000)
        self.sync_only()
        self.assertEqual(self.db.query(Series).count(), 103)
        full = list(self.client.series["s2"])
        self.client.series["s2"] = []          # the category answers [] (a provider hiccup)
        self.sync_only()
        self.sync_only()
        self.assertEqual(self.db.query(Series).count(), 103,
                         "series of a category that answered [] twice were pruned")
        self.client.series["s2"] = full
        self.sync_only()
        self.assertEqual(self.db.query(Series).count(), 103)


if __name__ == "__main__":
    unittest.main()
