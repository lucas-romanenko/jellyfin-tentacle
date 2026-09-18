"""An unknown /api path must not be answered by the single-page app.

The SPA catch-all serves index.html and is GET-only, so an unknown API path
answered a GET with the HTML page and anything else with "Method Not Allowed".
That is what a browser showed when the dashboard called an endpoint its backend
did not have yet — a stale image, most often — and the method was never the
problem, so the message sent people looking in the wrong place entirely.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest


class TestUnknownApiPaths(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import main
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_an_unknown_api_path_is_a_404_whatever_the_method(self):
        for method in ("get", "post", "put", "patch", "delete"):
            with self.subTest(method=method):
                r = getattr(self.client, method)("/api/youtube/no-such-thing")
                self.assertEqual(r.status_code, 404)

    def test_the_message_names_the_likely_cause(self):
        # "Method Not Allowed" pointed at the verb, which was never the problem.
        r = self.client.post("/api/youtube/no-such-thing")
        detail = r.json()["detail"]
        self.assertIn("No such endpoint", detail)
        self.assertIn("POST /api/youtube/no-such-thing", detail)
        self.assertIn("pull the latest image", detail)

    def test_an_unknown_api_path_never_returns_the_html_page(self):
        r = self.client.get("/api/nope")
        self.assertNotIn("<!DOCTYPE html>", r.text)
        self.assertEqual(r.headers.get("content-type", "").split(";")[0],
                         "application/json")

    def test_a_real_api_route_still_wins(self):
        # The handler is declared after every router, so routes that exist are
        # matched first — this must not shadow them.
        r = self.client.get("/api/youtube/ping")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("tentacle"))

    def test_a_real_post_route_is_not_shadowed(self):
        # Whatever this answers depends on the environment (auth, database) —
        # what matters is that the request reached the route rather than the
        # not-found handler, which is the bug this guards against.
        r = self.client.post("/api/youtube/channels", json={})
        self.assertNotEqual(r.status_code, 405)
        if r.status_code == 404:
            self.assertNotIn("No such endpoint", r.text)

    def test_ordinary_pages_are_still_the_single_page_app(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<!DOCTYPE html>", r.text)


class TestAssetCacheBusting(unittest.TestCase):
    """The dashboard's JS is cached by the browser on its ?v= URL.

    That number was edited by hand, which meant remembering on every change.
    Forgetting sent users new HTML with months-old JavaScript, where the page
    simply lacks whatever was added — and it looks like the feature was never
    shipped rather than like a caching problem.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        import main
        self.main = main
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def _versions(self):
        import re
        html = self.client.get("/").text
        return re.findall(r'/static/js/(\w+)\.js\?v=([\w.]+)', html)

    def test_the_scripts_carry_a_version(self):
        found = dict(self._versions())
        self.assertIn("app", found)
        self.assertIn("pages", found)
        self.assertTrue(all(v for v in found.values()))

    def test_it_is_derived_from_the_files_not_hardcoded(self):
        served = dict(self._versions())["pages"]
        self.assertEqual(served, self.main._asset_version())

    def test_changing_a_script_changes_the_version(self):
        from pathlib import Path
        before = dict(self._versions())["pages"]
        path = Path("static/js/pages.js")
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n// touched by a test\n")
            after = dict(self._versions())["pages"]
        finally:
            path.write_bytes(original)
        self.assertNotEqual(before, after)
        # ...and restoring the file restores the version, so the value depends
        # on content rather than on when it was last written.
        self.assertEqual(dict(self._versions())["pages"], before)

    def test_both_scripts_move_together(self):
        # One digest covers both files, so a change to either invalidates both
        # and they can never be served as a mismatched pair.
        found = dict(self._versions())
        self.assertEqual(found["app"], found["pages"])
