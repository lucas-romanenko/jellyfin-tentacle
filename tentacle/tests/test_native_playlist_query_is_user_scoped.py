"""Native (rule) playlists must query Jellyfin as the playlist's owner.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Measured on Jellyfin 10.11.8: a recursive /Items query WITHOUT a user filters
on metadata as it was several edits ago, while the same query with UserId sees
the current values. query_items() sent no user, so a film whose year, rating or
genre was edited in Jellyfin never entered (or left) the native playlists that
filter on it -- seen live: an edited film was still missing after three runs of
the 15-minute job. The same Jellyfin quirk is behind #107's stale tags.
"""
import unittest


class _Jf:
    def __init__(self):
        from services.jellyfin import JellyfinService
        self.jf = JellyfinService("http://jf:8096", "k", "owner-id")
        self.calls = []

        def _get(path, params=None):
            self.calls.append(dict(params or {}))
            return {"Items": [], "TotalRecordCount": 0}
        self.jf._get = _get


class QueryItems(unittest.TestCase):
    def test_a_user_id_is_sent_as_UserId(self):
        j = _Jf()
        j.jf.query_items(include_types=["Movie"], years=[1999], user_id="owner-id")
        self.assertEqual("owner-id", j.calls[0].get("UserId"))

    def test_without_a_user_id_nothing_changes(self):
        """Other callers (YouTube counts, the rule-builder preview) are untouched."""
        j = _Jf()
        j.jf.query_items(include_types=["Movie"], tags=["yt:x"])
        self.assertNotIn("UserId", j.calls[0])

    def test_paged_queries_keep_the_user(self):
        j = _Jf()
        j.jf.query_items(include_types=["Movie"], user_id="owner-id")
        self.assertTrue(all(c.get("UserId") == "owner-id" for c in j.calls))


class PlaylistBuilderPassesTheOwner(unittest.TestCase):
    def test_every_query_in_the_playlist_builder_is_user_scoped(self):
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "services" / "smartlists.py").read_text(encoding="utf-8")
        start = src.index("def _process_single_playlist_locked(")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn("jf.query_items(**query)", body)
        first_query = body.index("jf.query_items(**query)")
        self.assertIn('query["user_id"] = user_id', body[:first_query],
                      "the playlist builder queries Jellyfin without the owner's user id")


if __name__ == "__main__":
    unittest.main()
