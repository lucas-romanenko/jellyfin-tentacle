"""Moving a playlist entry must go through the Tentacle plugin (#32).

Jellyfin 10.11's own `POST /Playlists/{id}/Items/{entry}/Move/{index}` takes the
user from the caller's token and ignores `?UserId=`. Tentacle talks to Jellyfin
with a server API key, which has no user, so that endpoint answers HTTP 400
("Guid can't be empty") before it even looks the entry up — found live on
10.11.8. The webhook's move-to-front therefore never worked. The plugin runs
inside Jellyfin and can call IPlaylistManager.MoveItemAsync with an explicit
user, so move_playlist_item() asks the plugin first and only falls back to the
native endpoint when the plugin is missing or too old to have the route (404).

A real (loopback) HTTP server plays Jellyfin so the request that actually goes
over the wire is what is asserted.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests

import services.jellyfin as jellyfin

PLUGIN_MOVE = re.compile(r"^/Tentacle/Playlists/([^/]+)/Items/([^/]+)/Move/(\d+)$")
NATIVE_MOVE = re.compile(r"^/Playlists/([^/]+)/Items/([^/]+)/Move/(\d+)$")


class FakeJellyfin:
    """Jellyfin 10.11 as seen by an API key, with or without the new plugin."""

    def __init__(self, plugin_status):
        self.plugin_status = plugin_status      # None = plugin has no such route
        self.native_status = 400                # what 10.11.8 answers an API key
        self.calls = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                token = self.headers.get("X-Emby-Token")
                status = 404
                m = PLUGIN_MOVE.match(url.path)
                if m and fake.plugin_status is not None:
                    fake.calls.append(("plugin", m.groups(), query, token))
                    status = fake.plugin_status
                elif m:
                    fake.calls.append(("plugin-missing", m.groups(), query, token))
                elif NATIVE_MOVE.match(url.path):
                    fake.calls.append(("native", NATIVE_MOVE.match(url.path).groups(), query, token))
                    status = fake.native_status
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestMovePlaylistItem(unittest.TestCase):
    def serve(self, plugin_status):
        fake = FakeJellyfin(plugin_status)
        self.addCleanup(fake.close)
        return fake, jellyfin.JellyfinService(fake.url, "api-key", "user-1")

    def test_move_is_done_by_the_plugin_for_the_owning_user(self):
        fake, svc = self.serve(plugin_status=204)

        self.assertTrue(svc.move_playlist_item("pl-1", "entry-9", 0))

        self.assertEqual([c[0] for c in fake.calls], ["plugin"],
                         "the native endpoint cannot work with an API key; it must not be needed")
        _, ids, query, token = fake.calls[0]
        self.assertEqual(ids, ("pl-1", "entry-9", "0"))
        self.assertEqual({k.lower(): v for k, v in query.items()}, {"userid": "user-1"})
        self.assertEqual(token, "api-key")

    def test_falls_back_to_the_native_endpoint_when_the_plugin_has_no_such_route(self):
        """An older plugin DLL (or none) answers 404: keep today's behaviour."""
        fake, svc = self.serve(plugin_status=None)
        fake.native_status = 204

        self.assertTrue(svc.move_playlist_item("pl-1", "entry-9", 0))

        self.assertEqual([c[0] for c in fake.calls], ["plugin-missing", "native"])
        self.assertEqual(fake.calls[1][2], {"UserId": "user-1"})

    def test_old_plugin_on_jellyfin_10_11_reports_failure_without_raising(self):
        fake, svc = self.serve(plugin_status=None)      # native answers 400

        with self.assertLogs("services.jellyfin", level="WARNING"):
            self.assertFalse(svc.move_playlist_item("pl-1", "entry-9", 0))

        self.assertEqual([c[0] for c in fake.calls], ["plugin-missing", "native"])

    def test_a_refusal_from_the_plugin_is_final(self):
        """403/400 from the plugin is an answer, not a missing route: retrying the
        native endpoint would only add a second, misleading failure."""
        for status in (400, 403, 500):
            fake, svc = self.serve(plugin_status=status)
            with self.assertLogs("services.jellyfin", level="WARNING"):
                self.assertFalse(svc.move_playlist_item("pl-1", "entry-9", 0))
            self.assertEqual([c[0] for c in fake.calls], ["plugin"], status)

    def test_an_invalid_api_key_is_still_reported_as_such(self):
        fake, svc = self.serve(plugin_status=401)
        with self.assertLogs("services.jellyfin", level="ERROR") as logs:
            self.assertFalse(svc.move_playlist_item("pl-1", "entry-9", 0))
        self.assertTrue(any("API key is invalid" in line for line in logs.output))

    def test_without_a_user_the_plugin_is_not_asked(self):
        """The plugin endpoint needs the owning user; a service with none keeps
        using the native call exactly as before."""
        fake = FakeJellyfin(plugin_status=204)
        self.addCleanup(fake.close)
        svc = jellyfin.JellyfinService(fake.url, "api-key")
        fake.native_status = 204

        self.assertTrue(svc.move_playlist_item("pl-1", "entry-9", 0))
        self.assertEqual([c[0] for c in fake.calls], ["native"])

    def test_unreachable_jellyfin_is_a_false_not_an_exception(self):
        svc = jellyfin.JellyfinService("http://127.0.0.1:9", "api-key", "user-1")
        with self.assertLogs("services.jellyfin", level="WARNING"):
            self.assertFalse(svc.move_playlist_item("pl-1", "entry-9", 0))


if __name__ == "__main__":
    unittest.main()
