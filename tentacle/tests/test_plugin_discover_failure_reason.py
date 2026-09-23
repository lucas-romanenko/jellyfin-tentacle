"""#120 (3): Discover proxies say why a list is empty.

The plugin turned a timeout, a 401 or a missing Tentacle URL into a plain empty
list, so clients showed "no results" or a blank Discover page. Every list proxy
now answers through Unavailable()/ActivityUnavailable(), which keeps the empty
list (older clients are unaffected) and adds "error" + "message". Verified
against a real Jellyfin 10.11.11 with stub backends for each failure.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

SRC = Path("../tentacle-plugin/Api/DiscoverController.cs").read_text(encoding="utf-8")
LIST_ENDPOINTS = ["Items", "Activity", "Lists", "ListMissing", "Genres", "Genre",
                  "Providers", "Streaming", "Search"]


def _endpoint(name):
    start = SRC.index(f'[HttpGet("{name}")]')
    nxt = SRC.find("[Http", start + 10)
    return SRC[start:nxt if nxt != -1 else len(SRC)]


class DiscoverFailureReason(unittest.TestCase):
    def test_no_list_endpoint_swallows_a_failure_into_a_bare_empty_list(self):
        for name in LIST_ENDPOINTS:
            body = _endpoint(name)
            for block in re.findall(r"catch \(Exception ex\)\s*\{(.*?)\n        \}", body, re.S):
                self.assertIn("Unavailable(", block, f"{name}: catch returns a bare empty list")
            if "IsNullOrEmpty(baseUrl)" in body:
                after = body[body.index("IsNullOrEmpty(baseUrl)"):][:200]
                self.assertIn("Unavailable(", after, f"{name}: unconfigured returns a bare empty list")

    def test_every_reason_is_covered(self):
        for reason in ("not_configured", "timeout", "unauthorized", "server_error", "unreachable"):
            self.assertIn(f'"{reason}"', SRC)


if __name__ == "__main__":
    unittest.main()
