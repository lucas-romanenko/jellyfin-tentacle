"""What the Live TV sync status and provider test hand to the dashboard.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Found live: #82 cleaned the LOGS, but `GET /api/live/sync-status` still
returned the raw exception text of a failed sync -- for an Xtream or M3U
provider that is the request URL, username, password and token included.
"""
import unittest

URL = "http://panel.example/player_api.php?username=alice&password=hunter2&action=get_live_streams"
M3U = "http://panel.example/get.php?username=alice&password=hunter2&type=m3u_plus&token=abc123"
PATH = "http://panel.example/live/alice/hunter2/1234.ts"


class SyncStatusRedaction(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        self.addCleanup(livetv._sync_status.clear)

    def _stored(self, message):
        self.livetv._set_sync_status(7, {"phase": "error", "progress": 0, "message": message})
        return self.livetv._get_sync_status(7)["message"]

    def test_an_error_message_does_not_carry_the_provider_password(self):
        for leaky in (f"500 Server Error for url: {URL}", f"404 Client Error for url: {M3U}",
                      f"timed out fetching {PATH}"):
            shown = self._stored(leaky)
            self.assertNotIn("hunter2", shown, shown)
            self.assertNotIn("abc123", shown, shown)

    def test_the_username_is_redacted_too(self):
        """For an Xtream account it is half of the credential."""
        self.assertNotIn("alice", self._stored(f"500 Server Error for url: {URL}"))

    def test_the_rest_of_the_message_survives(self):
        shown = self._stored(f"500 Server Error for url: {URL}")
        self.assertIn("500 Server Error", shown)
        self.assertIn("panel.example", shown)
        self.assertIn("action=get_live_streams", shown)

    def test_an_ordinary_message_is_untouched(self):
        self.assertEqual("Saving 2 groups...", self._stored("Saving 2 groups..."))

    def test_the_caller_s_dict_is_not_modified(self):
        status = {"phase": "error", "message": f"boom {URL}"}
        self.livetv._set_sync_status(7, status)
        self.assertIn("hunter2", status["message"])


if __name__ == "__main__":
    unittest.main()
