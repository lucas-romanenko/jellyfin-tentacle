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
