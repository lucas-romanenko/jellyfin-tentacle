"""A new download must go to the FRONT of a recently-added row, not the end (#32).

Jellyfin's POST /Playlists/{id}/Items/{entryId}/Move/{index} checks the caller
against the playlist owner; the Tentacle server only holds the server API key,
so the call answered 400 and every new download sat at the end of a DateCreated
row until the nightly rebuild (and off the home row past its 30-item cap). The
plugin now does the move server-side as the owner; the backend prefers that
route and falls back to Jellyfin's own for an older plugin.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

import services.jellyfin as jellyfin


def _resp(status):
    r = mock.Mock()
    r.status_code = status
    r.headers = {}
    return r


class TestMoveViaPlugin(unittest.TestCase):
    def setUp(self):
        self.jf = jellyfin.JellyfinService("http://jf:8096", "adminkey", "u1")
        self.post = mock.patch.object(self.jf.session, "post").start()
        self.addCleanup(mock.patch.stopall)

    def urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_the_plugin_route_is_used_first(self):
        self.post.return_value = _resp(204)
        self.assertTrue(self.jf.move_playlist_item("p1", "e1", 0))
        self.assertEqual(self.urls(), ["http://jf:8096/Tentacle/Playlists/p1/Items/e1/Move/0"])

    def test_an_older_plugin_falls_back_to_jellyfins_route(self):
        self.post.side_effect = [_resp(404), _resp(204)]
        self.assertTrue(self.jf.move_playlist_item("p1", "e1", 0))
        self.assertEqual(self.urls(), ["http://jf:8096/Tentacle/Playlists/p1/Items/e1/Move/0",
                                       "http://jf:8096/Playlists/p1/Items/e1/Move/0"])
        self.assertEqual(self.post.call_args_list[1].kwargs["params"], {"UserId": "u1"})

    def test_a_plugin_failure_other_than_not_found_is_reported_not_retried(self):
        self.post.return_value = _resp(500)
        self.assertFalse(self.jf.move_playlist_item("p1", "e1", 0))
        self.assertEqual(len(self.urls()), 1)

    def test_jellyfins_400_is_a_failure(self):
        self.post.side_effect = [_resp(404), _resp(400)]
        self.assertFalse(self.jf.move_playlist_item("p1", "e1", 0))


if __name__ == "__main__":
    unittest.main()
