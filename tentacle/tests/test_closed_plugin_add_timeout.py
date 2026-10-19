"""#22 part 2: the Jellyfin plugin must not give up on an add before the backend can answer.

The plugin forwarded add-to-radarr / add-to-sonarr on its shared HttpClient
(Timeout = 15 s) while the backend's own POST to the *arr waits ADD_TIMEOUT
(90 s) and then verifies. Plugin users therefore got "Error" for adds that
landed. test_arr_add_verify_budget.py compares the backend's budget with the
AddClient timeout, but not which client the add calls use; this pins that.

There is no C# test host in the repo; this reads the controller source.
Fails on 6e50661^, passes from 6e50661 on.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/DiscoverController.cs")


def _timeout_seconds(src: str, client: str) -> float:
    m = re.search(client + r"\s*=\s*new\(\)\s*\{\s*Timeout\s*=\s*TimeSpan\.From(Seconds|Minutes)\((\d+)\)", src)
    if not m:
        raise AssertionError(f"no timeout declaration for {client}")
    return int(m.group(2)) * (60 if m.group(1) == "Minutes" else 1)


class TestIssue22PluginWaitsForTheAdd(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text(encoding="utf-8")
        import services.arr_add as arr_add
        self.backend_wait = float(getattr(arr_add, "VERIFY_TOTAL_SECONDS", 0) or arr_add.ADD_TIMEOUT)

    def _client_for(self, route):
        m = re.search(r"await\s+(\w+)\.PostAsync\(\s*AppendUserId\(\$\"\{baseUrl\}" + re.escape(route), self.src)
        self.assertTrue(m, f"no forward to {route} found")
        return m.group(1)

    def test_add_forwards_outlast_the_backend(self):
        for route in ("/api/lists/add-to-radarr", "/api/lists/add-to-sonarr"):
            client = self._client_for(route)
            timeout = _timeout_seconds(self.src, client)
            self.assertGreater(timeout, self.backend_wait,
                               f"#22: {route} is forwarded on {client} ({timeout:.0f} s), which gives up "
                               f"before the backend's {self.backend_wait:.0f} s add + verification")


if __name__ == "__main__":
    unittest.main()
