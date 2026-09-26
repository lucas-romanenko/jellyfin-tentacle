"""The hourly dead-entry prune waits for the plugin to finish (#181).

Run from the tentacle/ directory:  python -m unittest discover -s tests

On a library with ~30 playlists of up to 16k entries the plugin needs ~80 s,
and Tentacle gave up at 60 s on every run (8 of 8 in a day): a WARNING each
hour, the summary lost, and the playlist lock released while the plugin was
still rewriting playlists.
"""
import unittest

from services.jellyfin import PRUNE_DEAD_READ_TIMEOUT, JellyfinService


class _Resp:
    status_code = 200

    def json(self):
        return {"checkedPlaylists": 2, "prunedPlaylists": 1, "removed": 15}


class PruneDeadTimeout(unittest.TestCase):
    def test_the_read_timeout_is_long_enough_for_a_big_library(self):
        jf = JellyfinService("http://jf:8096", "k", "u1")
        seen = {}

        def post(url, json=None, timeout=None):
            seen["timeout"] = timeout
            return _Resp()
        jf.session.post = post
        self.assertEqual(15, jf.prune_dead_playlist_entries(["a", "b"])["removed"])
        connect, read = seen["timeout"]
        self.assertLessEqual(connect, 30)
        self.assertGreaterEqual(read, 600)
        self.assertEqual(PRUNE_DEAD_READ_TIMEOUT, read)


if __name__ == "__main__":
    unittest.main()
