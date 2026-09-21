"""The tag lookup must carry the tags Jellyfin actually holds.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Measured against Jellyfin 10.11.8: a recursive `/Items` listing WITHOUT a user
returns stale Tags for an item (what it carried several writes ago), while the
same listing with `UserId`, `/Users/{id}/Items`, and `/Items?ids=` all return
the current ones. Tentacle lists without a user on purpose -- a user-scoped
listing hides items that user cannot see -- so every decision it made about an
item's tags was made on old data: a stale tag of its own looked already removed
and stayed for ever, and a merge could write back a list missing the tags
somebody had added since.
"""
import unittest


class _Service:
    def make(self, user_id="u1", user_listing_fails=False):
        from services.jellyfin import JellyfinService
        jf = JellyfinService("http://jf:8096", "k", user_id)
        calls = []

        def _get(path, params=None):
            params = params or {}
            calls.append(dict(params))
            if params.get("UserId"):
                if user_listing_fails:
                    return None
                return {"Items": [{"Id": "a", "Tags": ["Netflix", "Old List", "date-night"]}], "TotalRecordCount": 1}
            return {"Items": [
                {"Id": "a", "Name": "Film", "ProductionYear": 2000, "ProviderIds": {"Tmdb": "10"}, "Tags": ["Netflix"]},
                {"Id": "b", "Name": "Hidden", "ProductionYear": 2001, "ProviderIds": {"Tmdb": "11"}, "Tags": ["Kids"]},
            ], "TotalRecordCount": 2}
        jf._get = _get
        return jf, calls


class FreshTags(unittest.TestCase, _Service):
    def test_the_lookup_carries_current_tags(self):
        jf, _ = self.make()
        lookup, _titles = jf.get_tmdb_lookup_with_fallback("Movie")
        self.assertEqual({"Netflix", "Old List", "date-night"}, set(lookup[10]["Tags"]))

    def test_items_the_user_cannot_see_are_still_in_the_lookup(self):
        """The unscoped listing stays the source of WHAT exists."""
        jf, _ = self.make()
        lookup, _titles = jf.get_tmdb_lookup_with_fallback("Movie")
        self.assertIn(11, lookup)
        self.assertEqual(["Kids"], lookup[11]["Tags"])

    def test_a_failed_tag_listing_changes_nothing(self):
        jf, _ = self.make(user_listing_fails=True)
        lookup, _titles = jf.get_tmdb_lookup_with_fallback("Movie")
        self.assertEqual(["Netflix"], lookup[10]["Tags"])

    def test_no_user_configured_means_no_extra_request(self):
        jf, calls = self.make(user_id="")
        jf.get_tmdb_lookup_with_fallback("Movie")
        self.assertEqual(1, len(calls))

    def test_the_tag_listing_asks_for_little(self):
        jf, calls = self.make()
        jf.get_tmdb_lookup_with_fallback("Movie")
        scoped = [c for c in calls if c.get("UserId")]
        self.assertEqual(1, len(scoped))
        self.assertEqual("Tags", scoped[0]["Fields"])
        self.assertEqual("false", scoped[0].get("EnableImages"))


if __name__ == "__main__":
    unittest.main()
