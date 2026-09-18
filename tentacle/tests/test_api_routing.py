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


class TestVersionEndpoint(unittest.TestCase):
    """"Which version am I running?" has to have a one-line answer.

    A container that looks up to date but is not is indistinguishable from a
    bug — both present as a feature simply not being there — and comparing
    two installs used to mean reading Docker image digests by hand.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        import main
        self.main = main
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_it_needs_no_login(self):
        r = self.client.get("/api/version")
        self.assertEqual(r.status_code, 200)

    def test_it_says_what_the_build_is_and_what_it_can_do(self):
        body = self.client.get("/api/version").json()
        for key in ("commit", "built", "assets", "endpoints"):
            self.assertIn(key, body)
        self.assertEqual(body["assets"], self.main._asset_version())
        # The endpoint list is how two installs are compared feature by feature.
        self.assertIn("/api/youtube/ping", body["endpoints"])
        self.assertIn("/api/version", body["endpoints"])

    def test_it_reports_nothing_configured(self):
        body = self.client.get("/api/version").json()
        text = str(body).lower()
        for secret in ("api_key", "password", "jellyfin_url", "token"):
            self.assertNotIn(secret, text)

    def test_health_carries_the_same_stamp(self):
        h = self.client.get("/api/health").json()
        v = self.client.get("/api/version").json()
        self.assertEqual(h["commit"], v["commit"])


class TestRunningCodeMatchesTheImage(unittest.TestCase):
    """An install can report the right version and run the wrong code.

    A bind mount of a modified checkout over /app leaves the image's version
    stamp intact while every page executes the mounted files. One install
    reported the current build for a day while running an older, patched
    services module underneath, and nothing on any page could say so. The
    image now carries a fingerprint of its own code, and this compares the
    files being executed against it.
    """

    def setUp(self):
        import hashlib
        import os
        import tempfile as _tf
        self.dir = _tf.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        os.makedirs("services")
        open("services/a.py", "w").write("print(1)\n")
        open("main.py", "w").write("app\n")
        lines = []
        for name in ("./services/a.py", "./main.py"):
            lines.append(f"{hashlib.sha256(open(name, 'rb').read()).hexdigest()}  {name}")
        open(".build-manifest", "w").write("\n".join(lines) + "\n")

    def tearDown(self):
        import os
        os.chdir(self.cwd)

    def _drift(self):
        from main import code_drift
        return code_drift(".build-manifest")

    def test_untouched_files_match(self):
        self.assertEqual(self._drift(), {"matches": True, "modified": [], "missing": []})

    def test_a_modified_file_is_named(self):
        open("services/a.py", "a").write("# patched locally\n")
        d = self._drift()
        self.assertFalse(d["matches"])
        self.assertEqual(d["modified"], ["./services/a.py"])

    def test_a_missing_file_is_named(self):
        import os
        os.remove("main.py")
        d = self._drift()
        self.assertFalse(d["matches"])
        self.assertEqual(d["missing"], ["./main.py"])

    def test_no_manifest_means_unknown_not_an_error(self):
        # A development checkout has no image and no manifest.
        import os
        os.remove(".build-manifest")
        self.assertIsNone(self._drift())

    def test_version_carries_the_answer(self):
        from fastapi.testclient import TestClient
        import main
        body = TestClient(main.app).get("/api/version").json()
        self.assertIn("code", body)
