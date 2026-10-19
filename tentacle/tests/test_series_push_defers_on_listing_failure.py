"""#161 follow-up: when a series' seasons/episodes can't be listed (twice in a
row, the in-run retry included), the tag push used to update the series
anyway. Jellyfin then copied the series' parental rating onto every episode,
and with no snapshot nothing could restore it: an episode rated above the
series stayed visible to a restricted profile for good.

Now the series update is deferred to the next push (its tags stay as they
are for one cycle), and only after DEFER_SERIES_MAX_ATTEMPTS failed listings
in a row is it updated anyway, with the warning and Activity entry as before,
so a series can't be kept out of its tag playlists for good.

Run from tentacle/:  python -m unittest discover -s tests -p "test_series_push_defers_on_listing_failure.py"
"""
import unittest
from unittest import mock

from test_series_push_keeps_episode_ratings import _FakeJellyfin, _PendingDir, _service


class TestDeferOnListingFailure(_PendingDir):
    def setUp(self):
        super().setUp()
        import services.jellyfin as j
        j._deferred_series_mem.clear()
        j._deferred_series_disk_ok = True
        self.addCleanup(j._deferred_series_mem.clear)

    def test_an_unwritable_data_dir_still_bounds_the_deferral(self):
        import os
        import services.jellyfin as j
        fake = _FakeJellyfin()
        fake.list_fails = True
        with mock.patch.dict(os.environ, {"DATA_DIR": os.path.join(self.data_dir, "missing", "dir")}), \
                mock.patch.object(j, "_log_activity_safe"), \
                self.assertLogs("services.jellyfin", level="WARNING"):
            results = [_service(fake).set_item_tags("s1", ["Netflix TV", "x"])
                       for _ in range(j.DEFER_SERIES_MAX_ATTEMPTS)]
        self.assertEqual(results[-1], True)
        j._deferred_series_disk_ok = True

    def test_a_failing_listing_defers_the_series_update(self):
        import services.jellyfin as j
        fake = _FakeJellyfin()
        fake.list_fails = True
        with mock.patch.object(j, "_log_activity_safe") as activity, \
                self.assertLogs("services.jellyfin", level="WARNING"):
            self.assertFalse(_service(fake).set_item_tags("s1", ["Netflix TV", "x"]))
        self.assertEqual(fake.items["s1"]["Tags"], ["Netflix TV"])       # not updated this run
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")    # nothing flattened
        activity.assert_not_called()

    def test_the_next_push_with_a_working_listing_updates_and_keeps_the_ratings(self):
        import services.jellyfin as j
        fake = _FakeJellyfin()
        fake.list_fails = True
        with mock.patch.object(j, "_log_activity_safe"), self.assertLogs("services.jellyfin", level="WARNING"):
            _service(fake).set_item_tags("s1", ["Netflix TV", "x"])
        fake.list_fails = False
        self.assertTrue(_service(fake).set_item_tags("s1", ["Netflix TV", "x"]))
        self.assertEqual(fake.items["s1"]["Tags"], ["Netflix TV", "x"])
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")
        self.assertEqual(fake.items["e1"]["OfficialRating"], "TV-PG")
        self.assertEqual(j._deferred_series_load(), {})                  # the count is cleared

    def test_after_the_bounded_number_of_failures_it_updates_anyway_and_says_so(self):
        import services.jellyfin as j
        fake = _FakeJellyfin()
        fake.list_fails = True
        results = []
        with mock.patch.object(j, "_log_activity_safe") as activity, \
                self.assertLogs("services.jellyfin", level="WARNING"):
            for _ in range(j.DEFER_SERIES_MAX_ATTEMPTS):
                results.append(_service(fake).set_item_tags("s1", ["Netflix TV", "x"]))
        self.assertEqual(results, [False] * (j.DEFER_SERIES_MAX_ATTEMPTS - 1) + [True])
        self.assertEqual(fake.items["s1"]["Tags"], ["Netflix TV", "x"])
        activity.assert_called_once()
        self.assertEqual(activity.call_args.args[0], "rating_cascade_unprotected")
        self.assertEqual(j._deferred_series_load(), {})

    def test_a_movie_is_never_deferred(self):
        fake = _FakeJellyfin()
        fake.items["m1"] = {"Id": "m1", "Type": "Movie", "Name": "Film", "Tags": []}
        fake.list_fails = True
        self.assertTrue(_service(fake).set_item_tags("m1", ["x"]))


if __name__ == "__main__":
    unittest.main()
