"""Injected plugin assets must honour their own cache-buster.

`IndexHtmlPatch` stamps every injected <script>/<link> with `?v=<CacheBust>`
precisely so the browser can cache them, but `ServeAsset` answered every one
of them with `no-cache, no-store, must-revalidate`. `no-store` forbids storing
the response at all, so the stamp was inert and ~583 KB of JS/CSS was
re-downloaded on every page load, for every client.

There is no C# test host here, so this reads the controller source the way
tests/test_frontend_state.py reads the dashboard JS: the header must be chosen
by comparing the request's `v` against `IndexHtmlPatch.CacheBust`, and the
long-lived header must not be reachable without that comparison.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")
PATCH = Path("../tentacle-plugin/Patching/IndexHtmlPatch.cs")

NO_STORE = "no-cache, no-store, must-revalidate"
IMMUTABLE = "public, max-age=31536000, immutable"


def serve_asset_body(src: str) -> str:
    """The text of ServeAsset, from its signature to the next member."""
    start = src.index("private ActionResult ServeAsset(")
    rest = src[start:]
    # Members at this indent level start at column 4; stop at the next one.
    end = re.search(r"\n    (?:private|public|internal|protected|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestInjectedAssetCaching(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text()
        self.body = serve_asset_body(self.src)

    def test_serve_asset_reads_the_version_stamp_from_the_request(self):
        self.assertIn('Request.Query["v"]', self.body,
                      "ServeAsset ignores the ?v= stamp index.html sends it")

    def test_a_matching_stamp_is_cacheable(self):
        self.assertIn(IMMUTABLE, self.body,
                      "no request is ever allowed to be cached")
        self.assertIn("IndexHtmlPatch.CacheBust", self.body,
                      "the cacheable answer is not tied to the current boot stamp")

    def test_an_absent_or_stale_stamp_is_still_uncacheable(self):
        self.assertIn(NO_STORE, self.body,
                      "a request without the current stamp must not be cached")

    def test_the_two_headers_are_chosen_by_comparing_the_stamp(self):
        # Both headers present but unconditionally assigned would pass the
        # checks above while still being wrong.
        self.assertRegex(
            self.body.replace("\n", " "),
            r"string\.Equals\(\s*stamp\s*,\s*Patching\.IndexHtmlPatch\.CacheBust.*"
            rf"{re.escape(IMMUTABLE)}.*{re.escape(NO_STORE)}",
            "the long-lived header is not gated on an exact stamp match",
        )

    def test_the_stamp_the_controller_compares_is_the_one_injected(self):
        # Guards against the producer being renamed out from under the check.
        self.assertRegex(PATCH.read_text(),
                         r"internal static readonly string CacheBust\b")


if __name__ == "__main__":
    unittest.main()
