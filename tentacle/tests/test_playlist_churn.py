"""#29: playlists are not cleared and re-added when only the set changed.

Jellyfin playlists are append-only, so an item that belongs in the middle used
to force a full clear + re-add. For rows the plugin re-sorts at read time
(releasedate / communityrating / name) the stored order is irrelevant, so the
set is all that matters and an append is enough. A new YouTube upload is the
common case: it sorts first, so every upload rebuilt the whole playlist.

Run from tentacle/:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path

import services.smartlists as sl


def order_config(sort_by):
    return {"Order": {"SortOptions": [{"SortBy": sort_by}]}}


class TestPlaylistOrderMatters(unittest.TestCase):
    def test_read_time_sorted_rows_do_not_need_stored_order(self):
        for sort_by in ("ReleaseDate", "releasedate", "CommunityRating", "Name"):
            with self.subTest(sort_by=sort_by):
                self.assertFalse(sl._playlist_order_matters(order_config(sort_by)))

    def test_datecreated_rows_use_stored_order(self):
        self.assertTrue(sl._playlist_order_matters(order_config("DateCreated")))

    def test_random_rows_keep_their_fixed_shuffle(self):
        # Comparing a fresh SortBy=Random query with the stored shuffle would
        # rebuild the playlist on every refresh.
        self.assertFalse(sl._playlist_order_matters(order_config("Random")))

    def test_no_sort_options_means_order_is_irrelevant(self):
        self.assertFalse(sl._playlist_order_matters({}))


class FakeJellyfin:
    def __init__(self, current):
        self._current = current
        self.added = []
        self.removed = []
        self.cleared = 0

    def query_items(self, **kwargs):
        # Newest first, as a ReleaseDate row is queried.
        return [{"Id": "new"}, {"Id": "a"}, {"Id": "b"}]

    def item_exists(self, playlist_id):
        return True

    def get_playlist_items(self, playlist_id, *a, **k):
        return [{"Id": i, "PlaylistItemId": f"pi-{i}"} for i in self._current]

    def add_to_playlist(self, playlist_id, ids):
        self.added.append(list(ids))
        return True

    def remove_from_playlist(self, playlist_id, entry_ids):
        self.removed.append(list(entry_ids))
        return True


class TestNewestItemDoesNotRebuild(unittest.TestCase):
    def _run(self, sort_by):
        jf = FakeJellyfin(["a", "b"])
        stats = {"updated": 0, "processed": 0, "changed": 0, "errors": 0, "item_counts": {}}
        config = {"Name": "Channel", "JellyfinPlaylistId": "pl-1", "MediaTypes": ["Movie"]}
        config.update(order_config(sort_by))
        sl._process_single_playlist(jf, Path("/nonexistent"), config, "u1", stats)
        return jf

    def test_new_upload_is_appended_not_rebuilt(self):
        jf = self._run("ReleaseDate")
        self.assertEqual(jf.added, [["new"]], "only the new item should be added")
        self.assertEqual(jf.removed, [], "nothing should be removed")

    def test_datecreated_row_still_rebuilds_to_keep_order(self):
        jf = self._run("DateCreated")
        # Stored order matters here, so the front insert is a rebuild.
        self.assertTrue(jf.removed, "a DateCreated row must still be reordered")


if __name__ == "__main__":
    unittest.main()
