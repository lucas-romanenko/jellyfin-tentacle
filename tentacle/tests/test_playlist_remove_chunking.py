"""Clearing a big playlist must not take one slow DELETE per 25 entries.

Jellyfin rewrites the whole playlist on every DELETE /Playlists/{id}/Items, so
each call costs the same (~23 s on a 40k-entry playlist) however many entries
it removes. An order-changing rebuild clears the playlist first: at 25 entries
a call a 44k-entry playlist needed ~1,760 calls -- over ten hours, with the
playlist half-empty and the refresh lock held the whole time. Measured live,
400 ids in one URL is refused (414); 200 is accepted.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from urllib.parse import urlencode

import services.jellyfin as jellyfin


class FakeResponse:
    status_code = 204
    text = ""


class FakeSession:
    def __init__(self):
        self.calls = []

    def delete(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        return FakeResponse()


def _entry_ids(n):
    return [f"{i:032x}" for i in range(n)]


class RemoveChunking(unittest.TestCase):
    def _svc(self):
        svc = jellyfin.JellyfinService("http://jf", "k", "0" * 32)
        svc.session = FakeSession()
        svc._check_401 = lambda *a, **k: None
        return svc

    def test_a_large_clear_uses_few_calls(self):
        svc = self._svc()
        self.assertTrue(svc.remove_from_playlist("p", _entry_ids(44_030)))
        self.assertLessEqual(len(svc.session.calls), 44_030 // 100 + 1,
                             "one DELETE per 25 entries makes a big rebuild take hours")

    def test_every_entry_is_removed_exactly_once(self):
        svc = self._svc()
        ids = _entry_ids(1_234)
        svc.remove_from_playlist("p", ids)
        sent = [e for _, p in svc.session.calls for e in p["EntryIds"].split(",")]
        self.assertEqual(ids, sent)

    def test_each_request_line_stays_under_kestrels_8kb_limit(self):
        svc = self._svc()
        svc.remove_from_playlist("p" * 32, _entry_ids(1_000))
        for url, params in svc.session.calls:
            line = f"DELETE {url.replace('http://jf', '')}?{urlencode(params)} HTTP/1.1"
            self.assertLess(len(line), 8 * 1024, "Jellyfin answers 414 past its request-line limit")


if __name__ == "__main__":
    unittest.main()
