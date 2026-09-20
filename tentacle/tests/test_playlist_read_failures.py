"""#31: an unreadable playlist is unknown, not empty.

get_playlist_items() returns None when Jellyfin could not be read (timeout or
transport error). Treating that as an empty playlist re-appends every desired
item, or counts a full playlist as holding 0 and "refills" it.

Run from tentacle/:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path

import services.smartlists as sl
from services.jellyfin import JellyfinService


def _service(get_result):
    svc = JellyfinService.__new__(JellyfinService)
    svc.user_id = None
    svc._get = lambda *a, **k: get_result
    return svc


class TestGetPlaylistItemsSignalsFailure(unittest.TestCase):
    def test_failed_read_returns_none(self):
        self.assertIsNone(_service(None).get_playlist_items("pl-1"))

    def test_genuinely_empty_playlist_returns_empty_list(self):
        self.assertEqual(_service({"Items": []}).get_playlist_items("pl-1"), [])

    def test_populated_playlist_returns_items(self):
        svc = _service({"Items": [{"Id": "a"}, {"Id": "b"}]})
        self.assertEqual(len(svc.get_playlist_items("pl-1")), 2)


class FakeJellyfin:
    """Enough of the client for _process_single_playlist's update path."""

    def __init__(self, playlist_items):
        self._playlist_items = playlist_items
        self.added = []
        self.removed = []

    def query_items(self, **kwargs):
        return [{"Id": "m1"}, {"Id": "m2"}]

    def item_exists(self, playlist_id):
        return True

    def get_playlist_items(self, playlist_id, *a, **k):
        return self._playlist_items

    def add_to_playlist(self, playlist_id, ids):
        self.added.append(list(ids))
        return True

    def remove_from_playlist(self, playlist_id, entry_ids):
        self.removed.append(list(entry_ids))
        return True


class TestProcessSinglePlaylistSkipsUnreadable(unittest.TestCase):
    def _run(self, playlist_items):
        jf = FakeJellyfin(playlist_items)
        stats = {"updated": 0, "processed": 0, "changed": 0, "errors": 0, "item_counts": {}}
        config = {"Name": "TV", "JellyfinPlaylistId": "pl-1", "MediaTypes": ["Movie"]}
        sl._process_single_playlist(jf, Path("/nonexistent"), config, "u1", stats)
        return jf, stats

    def test_unreadable_playlist_is_left_alone(self):
        jf, stats = self._run(None)
        self.assertEqual(jf.added, [], "a failed read must not re-append items")
        self.assertEqual(jf.removed, [])
        self.assertEqual(stats["errors"], 1)

    def test_readable_playlist_still_syncs(self):
        jf, _ = self._run([])
        self.assertEqual(jf.added, [["m1", "m2"]])


if __name__ == "__main__":
    unittest.main()
