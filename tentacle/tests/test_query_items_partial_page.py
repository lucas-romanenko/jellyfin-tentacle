"""A smart playlist must not lose entries when a later page of its query fails (#167).

Run from the tentacle/ directory:  python -m unittest discover -s tests

query_items() pages through /Items 2,000 at a time. A page after the first that
timed out ended the loop, and the pages that DID arrive came back as the whole
answer; the playlist rebuild then removed everything after them. A later-page
failure now raises, which every playlist caller turns into "leave the playlist
unchanged". A failed first page still returns [] (the empty-result guard).
"""
import logging
import unittest

from services.jellyfin import JellyfinService, PartialListing


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _service(fail_at=None, total=3600):
    jf = JellyfinService("http://jf:8096", "k", "u1")

    def _get(path, params=None):
        start = params["StartIndex"]
        if fail_at is not None and start >= fail_at:
            return None
        n = min(params["Limit"], total - start)
        return {"Items": [{"Id": str(start + i)} for i in range(n)], "TotalRecordCount": total}
    jf._get = _get
    return jf


class QueryItemsPaging(unittest.TestCase):
    def test_a_complete_listing_pages_through(self):
        self.assertEqual(3600, len(_service().query_items(include_types=["Movie"], tags=["Netflix Movies"])))

    def test_a_later_page_failure_raises_instead_of_returning_part(self):
        with self.assertRaises(PartialListing):
            _service(fail_at=2000).query_items(include_types=["Movie"], tags=["Netflix Movies"])

    def test_a_failed_first_page_is_still_an_empty_answer(self):
        self.assertEqual([], _service(fail_at=0).query_items(include_types=["Movie"]))

    def test_the_playlist_is_left_unchanged(self):
        """The real playlist builder, with a Jellyfin that loses page 2."""
        from pathlib import Path
        from services import smartlists
        jf = _service(fail_at=2000)
        writes = []
        jf.add_to_playlist = lambda *a, **k: writes.append(("add", a)) or True
        jf.remove_from_playlist = lambda *a, **k: writes.append(("remove", a)) or True
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        config = {"Name": "Netflix Movies", "MediaTypes": ["Movie"], "JellyfinPlaylistId": "pl1",
                  "ExpressionSets": [{"Expressions": [{"MemberName": "Tags", "Operator": "Contains",
                                                       "TargetValue": "Netflix Movies"}]}]}
        smartlists._process_single_playlist_locked(jf, Path("/nonexistent"), config, "u1", stats)
        self.assertEqual([], writes)
        self.assertEqual(1, stats["errors"])


if __name__ == "__main__":
    unittest.main()
