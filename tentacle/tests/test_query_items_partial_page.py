"""A smart playlist loses entries when one page of JellyfinService.query_items()
times out.

query_items() pages /Items 2000 at a time. _get() returns None on a timeout /
transport error, and the paging loop just `break`s, handing back the pages that
did arrive as if they were the whole answer. _process_single_playlist() then
diffs the playlist against that partial list and removes every entry past the
failed page. The M5 guard only catches a completely empty result.

Run from tentacle/:  python -m unittest discover -s tests -p "test_query_items_partial_page.py"
"""
import unittest
from pathlib import Path

import services.smartlists as sl
from services.jellyfin import JellyfinService

TOTAL = 3600


class PartialPagingJellyfin(JellyfinService):
    def __init__(self):
        self.url = "http://jf"
        self.user_id = "u1"
        self.removed = []
        self.added = []
        self.current = [{"Id": f"m{i}", "PlaylistItemId": f"e{i}", "Type": "Movie"} for i in range(TOTAL)]

    def _get(self, path, params=None):
        assert path == "/Items"
        start = params.get("StartIndex", 0)
        if start >= 2000:
            return None  # second page timed out
        return {"Items": [{"Id": f"m{i}"} for i in range(start, min(start + params["Limit"], TOTAL))],
                "TotalRecordCount": TOTAL}

    def item_exists(self, pid):
        return True

    def get_playlist_items(self, pid, *a, **k):
        return list(self.current)

    def add_to_playlist(self, pid, ids):
        self.added.append(list(ids))
        return True

    def remove_from_playlist(self, pid, entry_ids):
        self.removed.extend(entry_ids)
        return True


class TestQueryItemsPartialPage(unittest.TestCase):
    def test_query_items_does_not_return_partial_list_as_complete(self):
        jf = PartialPagingJellyfin()
        with self.assertRaises(RuntimeError):
            jf.query_items(["Movie"], tags=["Netflix Movies"])

    def test_a_complete_multi_page_answer_is_returned(self):
        jf = PartialPagingJellyfin()
        jf._get = lambda path, params=None: {
            "Items": [{"Id": f"m{i}"} for i in range(params["StartIndex"],
                                                      min(params["StartIndex"] + params["Limit"], TOTAL))],
            "TotalRecordCount": TOTAL}
        self.assertEqual(len(jf.query_items(["Movie"])), TOTAL)

    def test_a_failed_first_page_is_still_empty(self):
        """Unchanged: the M5 guard in _process_single_playlist handles this one."""
        jf = PartialPagingJellyfin()
        jf._get = lambda path, params=None: None
        self.assertEqual(jf.query_items(["Movie"]), [])

    def test_playlist_is_not_trimmed_on_a_failed_page(self):
        jf = PartialPagingJellyfin()
        stats = {"updated": 0, "processed": 0, "changed": 0, "errors": 0, "item_counts": {}}
        config = {"Name": "Netflix Movies", "JellyfinPlaylistId": "pl-1", "MediaTypes": ["Movie"],
                  "ExpressionSets": [{"Expressions": [
                      {"MemberName": "Tags", "Operator": "Contains", "TargetValue": "Netflix Movies"}]}],
                  "Order": {"SortOptions": [{"SortBy": "ReleaseDate", "SortOrder": "Descending"}]}}
        sl._process_single_playlist(jf, Path("/nonexistent"), config, "u1", stats)
        self.assertEqual(len(jf.removed), 0,
                         f"{len(jf.removed)} of {TOTAL} playlist entries removed after one page timed out")


if __name__ == "__main__":
    unittest.main()
