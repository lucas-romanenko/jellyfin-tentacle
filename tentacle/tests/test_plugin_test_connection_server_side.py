"""Plugin settings "Test Connection" tests the connection the plugin uses (#290).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The button sent an XHR from the admin's browser to <Tentacle URL>/api/widget/status,
but the plugin calls that URL from the Jellyfin server. In the usual Docker setup
the browser can't resolve the server-side name (http://tentacle:8888), the
backend's CORS allowlist blocks any Jellyfin origin other than its jellyfin_url,
and an https page can't call an http URL: "Connection failed" for a URL the plugin
uses fine, and the reverse. Now the page asks the plugin, which tests from the
server. Source-level: the repository has no C# test host.
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"
CONTROLLER = PLUGIN / "Api" / "TentacleController.cs"
PAGE = PLUGIN / "Configuration" / "configPage.html"


def _action(src: str, route: str) -> str:
    start = src.index('[HttpGet("%s")]' % route)
    end = src.find("\n    /// <summary>", start)
    return src[start:end if end != -1 else len(src)]


class ServerSideTest(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text(encoding="utf-8")

    def test_the_plugin_has_a_test_route_for_admins_only(self):
        body = _action(self.src, "TestConnection")
        self.assertIn('[Authorize(Policy = "RequiresElevation")]', body)

    def test_it_only_ever_asks_the_status_path_over_http(self):
        body = _action(self.src, "TestConnection")
        self.assertIn('"/api/widget/status"', body)
        self.assertRegex(body, r"Uri\.UriSchemeHttp\b")
        self.assertRegex(body, r"Uri\.UriSchemeHttps\b")

    def test_it_has_a_short_timeout(self):
        body = _action(self.src, "TestConnection")
        self.assertRegex(body, r"Timeout\s*=\s*TimeSpan\.FromSeconds\(\s*[1-9]\s*\)")

    def test_it_answers_only_the_status_fields(self):
        """Admin-only, but still: never echo an arbitrary URL's body back."""
        body = _action(self.src, "TestConnection")
        self.assertNotRegex(body, r"Content\(\s*(body|json|text)\s*,")
        # The fields /api/widget/status really has (the page read total_movies /
        # total_series, which it never had, so it always showed 0).
        for field in ('Count("movies")', 'Count("series")', '"last_sync"'):
            self.assertIn(field, body)


class PageUsesTheServer(unittest.TestCase):
    def setUp(self):
        self.page = PAGE.read_text(encoding="utf-8")
        start = self.page.index("function TentacleTest()")
        self.test_fn = self.page[start:self.page.index("</script>", start)]

    def test_the_browser_no_longer_calls_the_tentacle_url(self):
        self.assertNotIn("XMLHttpRequest", self.test_fn)
        self.assertNotIn("'/api/widget/status'", self.test_fn)

    def test_it_asks_the_plugin(self):
        self.assertIn("'Tentacle/TestConnection'", self.test_fn)

    def test_the_script_stays_inside_the_page_div(self):
        page_div = self.page.index('data-role="page"')
        self.assertGreater(self.page.index("function TentacleTest()"), page_div)
        self.assertLess(self.page.index("</script>"), self.page.rindex("</div>"))


if __name__ == "__main__":
    unittest.main()
