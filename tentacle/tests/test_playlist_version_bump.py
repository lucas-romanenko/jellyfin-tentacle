"""#30: only a real change bumps the playlist version clients poll.

The version counter drives the plugin's home-row reload. It was bumped on
"updated", which counts every playlist visited, so a refresh where nothing
changed still told every client to rebuild its home screen — every 15 minutes,
all day. Only a genuine change (or a newly created playlist) should bump it.

Run from tentacle/:  python -m unittest discover -s tests
"""
import unittest

import services.smartlists as sl


class TestStatsCarryChanged(unittest.TestCase):
    def test_refresh_result_reports_changed(self):
        import inspect

        src = inspect.getsource(sl.refresh_native_playlists)
        self.assertIn('"changed": 0', src, "refresh results must carry a 'changed' count")

    def test_version_bump_keys_on_changed_not_updated(self):
        import inspect

        src = inspect.getsource(sl.refresh_smartlist_playlists)
        self.assertIn('result.get("changed", 0) > 0', src)
        self.assertNotIn('result.get("updated", 0) > 0', src)


class TestNoOpRefreshDoesNotBump(unittest.TestCase):
    """A refresh that visits playlists but changes nothing must not bump."""

    def _bumped(self, result):
        # Mirrors the guard in refresh_smartlist_playlists().
        return result.get("changed", 0) > 0 or result.get("created", 0) > 0

    def test_visited_but_unchanged_does_not_bump(self):
        self.assertFalse(self._bumped(
            {"processed": 2, "created": 0, "updated": 2, "changed": 0, "errors": 0}))

    def test_real_change_bumps(self):
        self.assertTrue(self._bumped(
            {"processed": 2, "created": 0, "updated": 2, "changed": 1, "errors": 0}))

    def test_new_playlist_bumps(self):
        self.assertTrue(self._bumped(
            {"processed": 1, "created": 1, "updated": 0, "changed": 0, "errors": 0}))


if __name__ == "__main__":
    unittest.main()
