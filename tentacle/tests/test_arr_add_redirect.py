"""Radarr/Sonarr add through a redirect (#40): requests turns a POST answered with 301
(or 302/303) into a GET of the Location. Behind a proxy that forces HTTPS (or a
canonical host), POST /api/v3/movie comes back as GET /api/v3/movie: 200 and a
JSON *list* of every movie. Nothing was added, but the answer is a 2xx and
parses as JSON.
Run from tentacle/:  python -m unittest discover -s tests
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import services.arr_add as arr_add


class _Proxy(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        _Proxy.seen.append(("POST", self.path))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # e.g. http:// -> https://, or radarr.lan -> radarr.example.com
        self.send_response(301)
        self.send_header("Location", "/canonical" + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        _Proxy.seen.append(("GET", self.path))
        if "/series/lookup" in self.path:   # Sonarr's lookup is not redirected
            body = json.dumps([{"title": "Show", "tvdbId": 5, "tmdbId": 7, "seasons": []}]).encode()
        else:                               # the list endpoint the redirected POST lands on
            body = json.dumps([{"id": 1, "tmdbId": 111, "title": "Something else"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestRadarrAddThroughRedirect(unittest.TestCase):
    def setUp(self):
        _Proxy.seen = []
        self.srv = HTTPServer(("127.0.0.1", 0), _Proxy)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def test_a_redirected_add_is_not_reported_as_added(self):
        outcome, reason = arr_add.add_movie_to_radarr(self.url, "k", 949, 1, "/movies")
        self.assertEqual(_Proxy.seen, [("POST", "/api/v3/movie"), ("GET", "/canonical/api/v3/movie")])
        self.assertNotEqual(outcome, arr_add.ADDED,
                            "the POST became a GET of the movie list, yet the add was reported as done")

    def test_a_redirected_sonarr_add_is_a_failure_with_a_reason(self):
        from services.sonarr import SonarrService
        sonarr = SonarrService(self.url, "k")
        result = sonarr.add_series(tmdb_id=7, quality_profile_id=1, root_folder="/tv")
        self.assertIn(("GET", "/canonical/api/v3/series"), _Proxy.seen)
        self.assertIsNone(result)
        self.assertIn("redirects", sonarr.last_error or "")


if __name__ == "__main__":
    unittest.main()
