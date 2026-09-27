"""Music module, phase 1: settings, the services' Test buttons, the Lidarr
client's guard rails, and the Lidarr webhook.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import urlparse, parse_qs

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


class _Fake(BaseHTTPRequestHandler):
    """A scriptable HTTP service: routes[(method, path)] = (status, body[, headers])."""
    routes = {}
    seen = []
    delay = 0.0
    active = [0]
    max_active = [0]
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _serve(self, method):
        parsed = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
        with _Fake.lock:
            _Fake.active[0] += 1
            _Fake.max_active[0] = max(_Fake.max_active[0], _Fake.active[0])
        try:
            _Fake.seen.append((method, parsed.path, parse_qs(parsed.query), dict(self.headers), raw))
            if _Fake.delay:
                time.sleep(_Fake.delay)
            answer = _Fake.routes.get((method, parsed.path), (404, {"message": "no route"}))
            status, body = answer[0], answer[1]
            if callable(body):
                body = body()
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if isinstance(body, bytes) else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            with _Fake.lock:
                _Fake.active[0] -= 1

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


def _server(test):
    from http.server import ThreadingHTTPServer
    _Fake.routes, _Fake.seen, _Fake.delay = {}, [], 0.0
    _Fake.active[0] = _Fake.max_active[0] = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_port}"


class _DB(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)

    def set(self, key, value):
        from models.database import set_setting
        set_setting(self.db, key, value)


# ── Settings storage ──────────────────────────────────────────────────────

class TestMusicSettingsDefaults(_DB):
    def test_seeded_off_with_the_documented_defaults(self):
        from models.database import seed_defaults, get_setting
        seed_defaults(self.db)
        g = lambda k: get_setting(self.db, k)
        self.assertEqual(g("music_enabled"), "false")
        self.assertEqual(g("jellyfin_music_enabled"), "false")
        self.assertEqual(g("navidrome_enabled"), "false")
        self.assertEqual(g("navidrome_upload_artist_images"), "true")
        self.assertEqual(g("navidrome_rescan_after_import"), "true")
        self.assertEqual(g("deezer_enabled"), "true")
        self.assertEqual(g("music_preferred_countries"), "US,GB,CA,XW")
        self.assertEqual(g("music_preferred_formats"), "CD,Digital Media")
        self.assertEqual(g("music_edition_words").split(","),
                         ["deluxe", "expanded", "anniversary", "bonus", "live", "mono", "stereo",
                          "box", "collector", "legacy", "super"])
        self.assertEqual(g("music_reconcile_time"), "04:00")
        self.assertEqual(g("musicbrainz_cache_days"), "30")
        for k in ("music_auto_repin", "music_auto_repin_trim", "music_auto_repin_download"):
            self.assertEqual(g(k), "false", k)
        self.assertGreaterEqual(len(g("music_webhook_secret")), 24)

    def test_no_default_quality_profile_is_seeded(self):
        # A seeded "1" would bring back exactly the bug this replaces.
        from models.database import seed_defaults, get_setting
        seed_defaults(self.db)
        self.assertEqual(get_setting(self.db, "radarr_quality_profile_id"), "")
        self.assertEqual(get_setting(self.db, "sonarr_quality_profile_id"), "")

    def test_cleared_numeric_music_settings_read_as_their_default(self):
        from models.database import get_setting
        self.set("musicbrainz_cache_days", "")
        self.set("music_reconcile_time", "")
        self.assertEqual(get_setting(self.db, "musicbrainz_cache_days"), "30")
        self.assertEqual(get_setting(self.db, "music_reconcile_time"), "04:00")

    def test_new_secrets_are_masked_and_a_masked_save_keeps_them(self):
        import routers.settings as rs
        from models.database import get_setting
        self.set("lidarr_api_key", "abcdef0123456789abcdef")
        self.set("navidrome_password", "correct horse battery")
        self.set("music_webhook_secret", "s3cr3t-s3cr3t-s3cr3t-s3cr3t")
        shown = rs.get_settings(self.db)
        for k in ("lidarr_api_key", "navidrome_password", "music_webhook_secret"):
            self.assertIn("...", shown[k], k)
        rs.update_settings(rs.SettingsUpdate(settings={k: shown[k] for k in
                                                       ("lidarr_api_key", "navidrome_password")}), self.db)
        self.assertEqual(get_setting(self.db, "lidarr_api_key"), "abcdef0123456789abcdef")
        self.assertEqual(get_setting(self.db, "navidrome_password"), "correct horse battery")


# ── Lidarr client guard rails ─────────────────────────────────────────────

class TestLidarrGuardRails(unittest.TestCase):
    def setUp(self):
        self.url = _server(self)

    def client(self):
        from services.lidarr import LidarrClient
        return LidarrClient(self.url, "key")

    def test_unsafe_requests_are_refused_before_anything_is_sent(self):
        c = self.client()
        for path, params in (("/api/v1/album", {"includeAllArtistAlbums": "true"}),
                             ("/api/v1/artist", {"IncludeStatistics": "true"}),
                             ("/api/v1/history", {"page": 1}),
                             ("/api/v1/history/since", {"date": "2020-01-01"}),
                             ("/api/v1/wanted/missing", {"since": "2020-01-01"}),
                             ("/api/v1/wanted/missing", {"pageSize": 51}),
                             ("/api/v3/movie", None)):
            with self.assertRaises(ValueError, msg=f"{path} {params}"):
                c.get(path, params)
        self.assertEqual(_Fake.seen, [])

    def test_page_size_of_50_is_allowed(self):
        _Fake.routes[("GET", "/api/v1/wanted/missing")] = (200, {"records": []})
        self.assertEqual(self.client().get("/api/v1/wanted/missing", {"page": 1, "pageSize": 50}),
                         {"records": []})

    def test_every_call_uses_a_15_second_timeout(self):
        import services.lidarr as lidarr
        with mock.patch.object(lidarr.requests, "request") as req:
            req.return_value = mock.Mock(status_code=200, content=b"{}", json=lambda: {})
            self.client().system_status()
        self.assertEqual(req.call_args.kwargs["timeout"], 15)

    def test_one_request_at_a_time(self):
        _Fake.routes[("GET", "/api/v1/rootfolder")] = (200, [])
        _Fake.delay = 0.15
        threads = [threading.Thread(target=self.client().root_folders) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(_Fake.seen), 4)
        self.assertEqual(_Fake.max_active[0], 1, "Lidarr saw overlapping requests")

    def test_retries_twice_then_gives_up_with_a_logged_reason(self):
        from services.lidarr import LidarrError
        _Fake.routes[("GET", "/api/v1/rootfolder")] = (503, {"message": "busy"})
        with mock.patch("services.lidarr.time.sleep") as sleep, \
             self.assertLogs("services.lidarr", "WARNING") as logs:
            with self.assertRaises(LidarrError):
                self.client().root_folders()
        self.assertEqual(len(_Fake.seen), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 5])
        self.assertIn("Giving up", logs.output[-1])

    def test_client_errors_and_writes_are_not_retried(self):
        from services.lidarr import LidarrError
        _Fake.routes[("GET", "/api/v1/album/1")] = (404, {"message": "NotFound"})
        _Fake.routes[("POST", "/api/v1/command")] = (500, {"message": "boom"})
        c = self.client()
        with mock.patch("services.lidarr.time.sleep"):
            with self.assertRaises(LidarrError):
                c.get("/api/v1/album/1")
            with self.assertRaises(LidarrError):
                c.post("/api/v1/command", {"name": "RescanFolders"})
        self.assertEqual(len(_Fake.seen), 2)

    def test_a_bad_key_is_reported_as_such(self):
        from services.lidarr import LidarrError
        _Fake.routes[("GET", "/api/v1/system/status")] = (401, {})
        with self.assertRaises(LidarrError) as e:
            self.client().system_status()
        self.assertEqual(e.exception.status, 401)


# ── MusicBrainz ───────────────────────────────────────────────────────────

class TestMusicBrainzClient(unittest.TestCase):
    def test_requires_a_contact_and_sends_it_in_the_user_agent(self):
        import services.musicbrainz as mb
        with mock.patch.object(mb.requests, "get") as get:
            with self.assertRaises(mb.MusicBrainzError):
                mb.get("/artist/x", contact="")
            get.assert_not_called()
            get.return_value = mock.Mock(status_code=200, json=lambda: {"name": "Nirvana"})
            mb.get("/artist/x", contact="me@example.com")
        ua = get.call_args.kwargs["headers"]["User-Agent"]
        self.assertIn("Tentacle/", ua)
        self.assertIn("me@example.com", ua)
        self.assertEqual(get.call_args.kwargs["params"]["fmt"], "json")
        self.assertEqual(get.call_args.kwargs["timeout"], 15)

    def test_at_most_one_request_per_second(self):
        import services.musicbrainz as mb
        stamps = []

        def fake_get(*a, **k):
            stamps.append(time.monotonic())
            return mock.Mock(status_code=200, json=lambda: {})
        with mock.patch.object(mb.requests, "get", side_effect=fake_get):
            for _ in range(3):
                mb.get("/artist/x", contact="me@example.com")
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.99 for g in gaps), gaps)

    def test_rate_limited_answers_are_retried_then_given_up(self):
        import services.musicbrainz as mb
        with mock.patch.object(mb.requests, "get", return_value=mock.Mock(status_code=503)) as get, \
             mock.patch.object(mb.time, "sleep"), self.assertLogs("services.musicbrainz", "WARNING"):
            with self.assertRaises(mb.MusicBrainzError):
                mb.get("/artist/x", contact="me@example.com")
        self.assertEqual(get.call_count, 3)


# ── Navidrome parsing ─────────────────────────────────────────────────────

class TestNavidromeParsing(unittest.TestCase):
    def test_app_config_is_a_json_string_inside_a_js_string(self):
        # What Go's html/template renders for window.__APP_CONFIG__ = {{ .AppConfig }}
        from services.navidrome import parse_app_config
        inner = json.dumps({"version": "0.61.2 (aa84e64)", "enableArtworkUpload": True})
        html = f"<script>\n      window.__APP_CONFIG__ = {json.dumps(inner)}\n    </script>"
        self.assertEqual(parse_app_config(html), {"version": "0.61.2 (aa84e64)", "enableArtworkUpload": True})
        self.assertIsNone(parse_app_config("<html>not navidrome</html>"))

    def test_ignored_credit_roles_any_case(self):
        from services.navidrome import ignored_credit_roles
        cfg = {"Tags": {"composer": {"Ignore": True}, "Lyricist": {"ignore": True},
                        "producer": {"Ignore": False}, "genre": {"Ignore": True}}}
        self.assertEqual(ignored_credit_roles(cfg), ["composer", "lyricist"])
        self.assertEqual(ignored_credit_roles({}), [])


# ── The Test buttons ──────────────────────────────────────────────────────

def _statuses(result):
    return {c["label"]: c["status"] for c in result["checks"]}


class TestServiceChecks(_DB):
    def setUp(self):
        super().setUp()
        self.url = _server(self)

    def test_lidarr_all_good(self):
        from services.service_checks import check_lidarr
        _Fake.routes.update({
            ("GET", "/api/v1/system/status"): (200, {"appName": "Lidarr", "version": "2.9.6"}),
            ("GET", "/api/v1/rootfolder"): (200, [{"id": 1, "path": "/data/music"}]),
            ("GET", "/api/v1/qualityprofile"): (200, [{"id": 3, "name": "FLAC Preferred"}]),
            ("GET", "/api/v1/metadataprofile"): (200, [{"id": 1, "name": "Standard"}]),
        })
        r = check_lidarr(self.db, self.url, "key", {"lidarr_root_folder": "/data/music",
                                                    "lidarr_quality_profile_id": "3",
                                                    "lidarr_metadata_profile_id": "1"})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["message"], "Lidarr 2.9.6 connected")
        self.assertEqual(set(_statuses(r).values()), {"ok"})
        self.assertIn(("X-Api-Key", "key"), [(k, v) for k, v in _Fake.seen[0][3].items() if k == "X-Api-Key"])

    def test_lidarr_says_exactly_what_failed(self):
        from services.service_checks import check_lidarr
        _Fake.routes[("GET", "/api/v1/system/status")] = (401, {})
        r = check_lidarr(self.db, self.url, "bad", None)
        self.assertEqual(_statuses(r), {"Reachable": "ok", "API key": "fail"})
        self.assertFalse(r["success"])

        r = check_lidarr(self.db, "http://127.0.0.1:9", "k", None)
        self.assertEqual(_statuses(r), {"Reachable": "fail"})

        _Fake.routes[("GET", "/api/v1/system/status")] = (200, {"appName": "Radarr", "version": "5"})
        r = check_lidarr(self.db, self.url, "k", None)
        self.assertEqual(_statuses(r)["Version"], "fail")
        self.assertIn("Radarr", r["message"])

    def test_lidarr_unpicked_defaults_are_hints_and_stale_ones_fail(self):
        from services.service_checks import check_lidarr
        _Fake.routes.update({
            ("GET", "/api/v1/system/status"): (200, {"appName": "Lidarr", "version": "2.9.6"}),
            ("GET", "/api/v1/rootfolder"): (200, [{"id": 1, "path": "/data/music"}]),
            ("GET", "/api/v1/qualityprofile"): (200, [{"id": 3, "name": "FLAC Preferred"}]),
            ("GET", "/api/v1/metadataprofile"): (200, [{"id": 1, "name": "Standard"}]),
        })
        r = check_lidarr(self.db, self.url, "k", None)
        self.assertTrue(r["success"])
        self.assertEqual(_statuses(r)["Root folder"], "warn")
        r = check_lidarr(self.db, self.url, "k", {"lidarr_quality_profile_id": "99"})
        self.assertEqual(_statuses(r)["Quality profile"], "fail")

    def test_radarr_check_reports_the_request_defaults(self):
        from services.service_checks import check_arr
        _Fake.routes.update({
            ("GET", "/api/v3/system/status"): (200, {"appName": "Radarr", "version": "5.14"}),
            ("GET", "/api/v3/qualityprofile"): (200, [{"id": 1, "name": "Any"}, {"id": 6, "name": "HD-1080p"}]),
            ("GET", "/api/v3/rootfolder"): (200, [{"path": "/movies"}]),
        })
        r = check_arr(self.db, "radarr", self.url, "k", None)
        self.assertEqual(_statuses(r)["Default quality profile"], "fail")
        self.assertIn("Pick a default Radarr quality profile", r["message"])
        r = check_arr(self.db, "radarr", self.url, "k", {"radarr_quality_profile_id": "6"})
        self.assertTrue(r["success"], r)
        self.assertEqual([c["detail"] for c in r["checks"] if c["label"] == "Default quality profile"], ["HD-1080p"])
        self.assertIn("Automatic: /movies", [c["detail"] for c in r["checks"]])

    def _navidrome(self, admin=True, upload=False, tags=None):
        inner = json.dumps({"version": "0.61.2 (aa84e64)", "enableArtworkUpload": upload})
        _Fake.routes.update({
            ("GET", "/app/"): (200, f"<script>window.__APP_CONFIG__ = {json.dumps(inner)}</script>".encode()),
            ("POST", "/auth/login"): (200, {"token": "jwt", "isAdmin": admin, "username": "tentacle"}),
            ("GET", "/api/config"): ((200, {"config": {"EnableArtworkUpload": upload, "Tags": tags or {}}})
                                     if admin else (403, {})),
        })

    def test_navidrome_admin_with_credit_roles_ignored(self):
        from services.service_checks import check_navidrome
        self._navidrome(admin=True, upload=False, tags={"composer": {"Ignore": True}})
        r = check_navidrome(self.db, self.url, "tentacle", "pw")
        self.assertTrue(r["success"], r)
        st = _statuses(r)
        self.assertEqual(st["Artwork upload"], "ok")  # admins may upload even when it's off
        self.assertEqual(st["Credit roles"], "ok")
        self.assertEqual(r["message"], "Navidrome 0.61.2 (aa84e64) connected")
        auth = [h for m, p, q, h, b in _Fake.seen if p == "/api/config"][0]
        self.assertEqual(auth.get("X-ND-Authorization"), "Bearer jwt")

    def test_navidrome_non_admin_without_upload_fails_that_step_only(self):
        from services.service_checks import check_navidrome
        self._navidrome(admin=False, upload=False)
        r = check_navidrome(self.db, self.url, "tentacle", "pw")
        st = _statuses(r)
        self.assertEqual(st["Sign-in"], "ok")
        self.assertEqual(st["Artwork upload"], "fail")
        self.assertEqual(st["Credit roles"], "warn")  # a hint, never a failure

    def test_navidrome_wrong_password(self):
        from services.service_checks import check_navidrome
        self._navidrome()
        _Fake.routes[("POST", "/auth/login")] = (401, {"error": "Invalid username or password"})
        r = check_navidrome(self.db, self.url, "tentacle", "bad")
        self.assertEqual(_statuses(r), {"Reachable": "ok", "Sign-in": "fail"})

    def test_jellyfin_music_library_and_folder(self):
        from services.service_checks import check_jellyfin_music
        self.set("jellyfin_url", self.url)
        self.set("jellyfin_api_key", "jf")
        _Fake.routes[("GET", "/Library/VirtualFolders")] = (200, [
            {"Name": "Music", "ItemId": "m1", "CollectionType": "music", "Locations": ["/data/music/"]},
            {"Name": "Movies", "ItemId": "v1", "CollectionType": "movies", "Locations": ["/movies"]},
        ])
        r = check_jellyfin_music(self.db, "m1", {"lidarr_root_folder": "/data/music"})
        self.assertTrue(r["success"], r)
        self.assertEqual(_statuses(r)["Folder"], "ok")
        r = check_jellyfin_music(self.db, "m1", {"lidarr_root_folder": "/music"})
        self.assertEqual(_statuses(r)["Folder"], "warn")
        r = check_jellyfin_music(self.db, "v1", None)
        self.assertEqual(_statuses(r)["Music library"], "fail")
        r = check_jellyfin_music(self.db, "gone", None)
        self.assertIn("no longer exists", r["message"])

    def test_musicbrainz_needs_a_contact_before_any_request(self):
        from services.service_checks import check_musicbrainz
        import services.musicbrainz as mb
        with mock.patch.object(mb.requests, "get") as get:
            r = check_musicbrainz(self.db, "")
            get.assert_not_called()
        self.assertEqual(_statuses(r), {"Contact email": "fail"})
        with mock.patch.object(mb.requests, "get",
                               return_value=mock.Mock(status_code=200, json=lambda: {"name": "Nirvana"})):
            r = check_musicbrainz(self.db, "me@example.com")
        self.assertTrue(r["success"], r)

    def test_deezer(self):
        from services import service_checks as sc
        with mock.patch.object(sc.requests, "get",
                               return_value=mock.Mock(status_code=200, json=lambda: {"data": [{"id": 1}]})):
            self.assertTrue(sc.check_deezer()["success"])
        with mock.patch.object(sc.requests, "get", side_effect=sc.requests.exceptions.ConnectionError()):
            self.assertEqual(_statuses(sc.check_deezer()), {"Reachable": "fail"})


# ── The Lidarr webhook ────────────────────────────────────────────────────

class TestMusicWebhook(_DB):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db
        app = FastAPI()
        app.include_router(music.router)
        app.include_router(music.webhook_router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        self.client = TestClient(app)  # no users yet: admin routes are open (bootstrap)
        self.set("music_webhook_secret", "the-secret")

    def test_the_secret_is_always_required(self):
        for url, headers in (("/api/music/webhook", {}), ("/api/music/webhook?secret=wrong", {}),
                             ("/api/music/webhook", {"X-Tentacle-Secret": "nope"}),
                             ("/api/music/webhook?secret=%C3%A9", {})):
            r = self.client.post(url, json={"eventType": "Test"}, headers=headers)
            self.assertEqual(r.status_code, 401, url)

    def test_test_event_is_recorded(self):
        from models.database import get_setting
        r = self.client.post("/api/music/webhook?secret=the-secret", json={"eventType": "Test"})
        self.assertEqual(r.json(), {"ok": True, "event": "Test"})
        self.db.expire_all()
        self.assertTrue(get_setting(self.db, "music_webhook_last_test"))
        r = self.client.post("/api/music/webhook", json={"eventType": "Test"},
                             headers={"X-Tentacle-Secret": "the-secret"})
        self.assertEqual(r.status_code, 200)

    def test_other_events_are_ignored_while_the_module_is_off(self):
        r = self.client.post("/api/music/webhook?secret=the-secret", json={"eventType": "Download"})
        self.assertEqual(r.json().get("ignored"), "the music module is off")

    def test_webhook_info_builds_the_url_lidarr_needs(self):
        self.set("youtube_base_url", "http://192.168.2.75:8888/")
        info = self.client.get("/api/music/webhook-info").json()
        self.assertEqual(info["url"], "http://192.168.2.75:8888/api/music/webhook?secret=the-secret")
        self.assertEqual(info["triggers"], ["On Artist Add", "On Release Import", "On Upgrade"])
        new = self.client.post("/api/music/webhook/regenerate").json()
        self.assertNotEqual(new["secret"], "the-secret")
        self.assertEqual(self.client.post("/api/music/webhook?secret=the-secret",
                                          json={"eventType": "Test"}).status_code, 401)

    def _hook(self, secret="the-secret", **triggers):
        n = {"id": 4, "name": "Tentacle", "implementation": "Webhook",
             "fields": [{"name": "url", "value": f"http://t:8888/api/music/webhook?secret={secret}"},
                        {"name": "method", "value": 1}]}
        n.update({"onArtistAdd": True, "onReleaseImport": True, "onUpgrade": True, **triggers})
        return n

    def test_send_test_confirms_lidarr_reached_tentacle(self):
        from services.lidarr import LidarrClient
        self.set("lidarr_url", "http://lidarr:8686")
        self.set("lidarr_api_key", "k")
        client = self.client

        def lidarr_fires_its_test(_self, notification):
            # What Lidarr does: POST {eventType: Test} to the webhook's URL.
            url = notification["fields"][0]["value"].split("8888", 1)[1]
            client.post(url, json={"eventType": "Test"})

        with mock.patch.object(LidarrClient, "notifications", return_value=[self._hook()]), \
             mock.patch.object(LidarrClient, "test_notification", lidarr_fires_its_test):
            r = self.client.post("/api/music/webhook/test").json()
        self.assertTrue(r["success"], r)
        self.assertEqual(_statuses(r)["Lidarr reached Tentacle"], "ok")

    def test_send_test_spots_an_old_secret_and_missing_triggers(self):
        from services.lidarr import LidarrClient
        self.set("lidarr_url", "http://lidarr:8686")
        self.set("lidarr_api_key", "k")
        with mock.patch.object(LidarrClient, "notifications",
                               return_value=[self._hook(secret="old", onUpgrade=False)]), \
             mock.patch.object(LidarrClient, "test_notification", return_value=None):
            r = self.client.post("/api/music/webhook/test").json()
        st = _statuses(r)
        self.assertEqual(st["Secret"], "fail")
        self.assertEqual(st["Triggers"], "warn")
        self.assertEqual(st["Lidarr reached Tentacle"], "fail")

    def test_send_test_when_lidarr_has_no_such_webhook(self):
        from services.lidarr import LidarrClient
        self.set("lidarr_url", "http://lidarr:8686")
        self.set("lidarr_api_key", "k")
        with mock.patch.object(LidarrClient, "notifications", return_value=[]):
            r = self.client.post("/api/music/webhook/test").json()
        self.assertEqual(_statuses(r)["Webhook in Lidarr"], "fail")


if __name__ == "__main__":
    unittest.main()
