"""Music: the Jellyfin rescan after a Lidarr import (#677).

Jellyfin's /Items/{id}/Refresh defaults to MetadataRefreshMode=None: new
albums and tracks are created without reading their files (folder and file
names, no MusicBrainz ids) and marked refreshed, so later scans skip them.
A library created on an empty folder has nothing under it for that refresh
to look in; only a full /Library/Refresh finds the first album.

A fake Jellyfin over real HTTP records what Tentacle asks.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class _FakeJellyfin(BaseHTTPRequestHandler):
    log = []          # (method, path, {param: value})
    children = []     # what /Items?ParentId=<library> lists
    items_status = 200

    def _record(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        type(self).log.append((self.command, u.path, q, self.headers.get("X-Emby-Token")))
        return u.path, q

    def do_GET(self):
        path, q = self._record()
        if path == "/Items" and self.items_status == 200:
            items = [c for c in self.children if q.get("ParentId") == "lib1"]
            limit = int(q.get("Limit") or len(items) or 1)
            body = json.dumps({"Items": items[:limit], "TotalRecordCount": len(items)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(self.items_status if path == "/Items" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._record()
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


class TestJellyfinMusicRescan(unittest.TestCase):
    def setUp(self):
        _FakeJellyfin.log, _FakeJellyfin.children, _FakeJellyfin.items_status = [], [], 200
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeJellyfin)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        from services.music.players import JellyfinPlayer
        self.player = JellyfinPlayer.__new__(JellyfinPlayer)
        self.player.url = f"http://127.0.0.1:{srv.server_port}"
        self.player.key, self.player.library_id, self.player.db = "k", "lib1", None

    def posts(self):
        return [(path, q) for method, path, q, _ in _FakeJellyfin.log if method == "POST"]

    def test_the_library_refresh_reads_new_files(self):
        _FakeJellyfin.children = [{"Id": "artist1", "Name": "Big Star", "Type": "MusicArtist"}]
        self.player.rescan()
        posts = self.posts()
        self.assertEqual([p for p, _ in posts], ["/Items/lib1/Refresh"])
        q = posts[0][1]
        self.assertEqual(q.get("Recursive"), "true")
        self.assertEqual(q.get("MetadataRefreshMode"), "Default")
        self.assertEqual(q.get("ImageRefreshMode"), "Default")
        # Default modes, never a replace: tags already in Jellyfin survive.
        self.assertEqual(q.get("ReplaceAllMetadata"), "false")
        self.assertEqual(q.get("ReplaceAllImages"), "false")
        self.assertTrue(all(token == "k" for *_, token in _FakeJellyfin.log))

    def test_an_empty_library_gets_a_full_library_scan(self):
        _FakeJellyfin.children = []
        self.player.rescan()
        self.assertEqual([p for p, _ in self.posts()], ["/Library/Refresh"])
        gets = [(p, q) for m, p, q, _ in _FakeJellyfin.log if m == "GET"]
        self.assertTrue(any(p == "/Items" and q.get("ParentId") == "lib1" for p, q in gets), gets)

    def test_when_jellyfin_cannot_list_the_library_it_refreshes_the_library(self):
        _FakeJellyfin.items_status = 500
        self.player.rescan()
        posts = self.posts()
        self.assertEqual([p for p, _ in posts], ["/Items/lib1/Refresh"])
        self.assertEqual(posts[0][1].get("MetadataRefreshMode"), "Default")

    def test_no_library_picked_posts_nothing(self):
        self.player.library_id = ""
        self.player.rescan()
        self.assertEqual(_FakeJellyfin.log, [])


if __name__ == "__main__":
    unittest.main()
