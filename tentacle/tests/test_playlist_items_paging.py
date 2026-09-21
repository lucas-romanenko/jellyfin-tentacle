"""get_playlist_items must return every entry, however many there are (#31).

One request with Limit=50000 silently truncated a longer playlist. A TV
playlist stores episodes, so a few hundred series is tens of thousands of
entries, and a truncated listing read as "the rest of the entries are gone".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

import services.jellyfin as jellyfin


class FakePages:
    def __init__(self, total, fail_at=None):
        self.total = total
        self.fail_at = fail_at
        self.calls = []

    def __call__(self, service, path, params=None):
        self.calls.append(dict(params))
        start = int(params["StartIndex"])
        if self.fail_at is not None and start >= self.fail_at:
            return None  # timeout / transport error
        limit = int(params["Limit"])
        page = [{"Id": f"e{i}"} for i in range(start, min(start + limit, self.total))]
        return {"Items": page, "TotalRecordCount": self.total}


class TestPlaylistItemsPaging(unittest.TestCase):
    def setUp(self):
        self._saved = jellyfin.JellyfinService._get
        self.addCleanup(setattr, jellyfin.JellyfinService, "_get", self._saved)

    def _svc(self, fake):
        jellyfin.JellyfinService._get = lambda s, path, params=None: fake(s, path, params)
        return jellyfin.JellyfinService("http://jf", "k", "u")

    def test_a_playlist_longer_than_one_page_is_read_in_full(self):
        fake = FakePages(total=120_001)
        items = self._svc(fake).get_playlist_items("p")
        self.assertEqual(len(items), 120_001)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual([c["StartIndex"] for c in fake.calls], [0, 50000, 100000])

    def test_a_short_playlist_costs_one_request(self):
        fake = FakePages(total=40)
        items = self._svc(fake).get_playlist_items("p")
        self.assertEqual(len(items), 40)
        self.assertEqual(len(fake.calls), 1)

    def test_a_failed_page_makes_the_whole_read_unknown(self):
        fake = FakePages(total=120_001, fail_at=100000)
        self.assertIsNone(self._svc(fake).get_playlist_items("p"),
                          "a partial playlist was returned as if complete")

    def test_an_empty_playlist_is_an_empty_list(self):
        self.assertEqual(self._svc(FakePages(total=0)).get_playlist_items("p"), [])


if __name__ == "__main__":
    unittest.main()
