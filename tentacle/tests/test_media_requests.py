"""One request path: every Radarr/Sonarr request uses the defaults from settings.

A movie once arrived as a disc image because requests fell back to quality
profile 1 ("Any"): the settings the code read never existed, and every client
preselected the *arr's first profile and sent it. Now every entry point goes
through services.media_requests:

* the configured default profile and root folder are applied;
* changing the setting changes the next request from every entry point;
* only `quality_profile_override` (an explicit per-request choice) beats it,
  and it is logged; the legacy `quality_profile_id` is ignored;
* with no default configured the request is refused and nothing is sent.

Entry points covered: /api/lists/add-to-radarr and add-to-sonarr (Tentacle's
dashboard; the Jellyfin plugin on web and Android TV, which proxy to them with
?api_key=; API callers), a list's add-missing-to-radarr / -sonarr, and a list
fetch's auto-add.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

PROFILES = [{"id": 1, "name": "Any"}, {"id": 6, "name": "HD-1080p"}, {"id": 9, "name": "Ultra-HD"}]
SERIES_LOOKUP = [{"title": "Show", "tvdbId": 5, "tmdbId": 7, "year": 2020,
                  "seasons": [{"seasonNumber": 1, "monitored": True}], "images": [], "titleSlug": "show"}]


class _FakeArr(BaseHTTPRequestHandler):
    """Radarr and Sonarr in one."""
    routes = {}
    requests = []  # (method, path, body)

    def log_message(self, *a):
        pass

    def _answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        _FakeArr.requests.append(("GET", path, None))
        default = (200, PROFILES) if path.endswith("/qualityprofile") else (200, [])
        self._answer(*_FakeArr.routes.get(("GET", path), default))

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        _FakeArr.requests.append(("POST", path, body))
        default = (201, {"id": 1, "path": "/tv/Show"}) if path.endswith("/series") else (201, {"id": 1})
        self._answer(*_FakeArr.routes.get(("POST", path), default))


PLUGIN = types.SimpleNamespace(query_params={"api_key": "tok", "userId": "u1"})
DASHBOARD = types.SimpleNamespace(query_params={})


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import TentacleUser, set_setting
        _FakeArr.routes = {
            ("GET", "/api/v3/rootfolder"): (200, [{"path": "/vod/movies", "id": 1}, {"path": "/movies", "id": 2}]),
            ("GET", "/api/v3/series/lookup"): (200, SERIES_LOOKUP),
        }
        _FakeArr.requests = []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeArr)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        url = f"http://127.0.0.1:{self.srv.server_port}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(self.user)
        self.db.commit()
        for k, v in (("radarr_url", url), ("radarr_api_key", "k"), ("sonarr_url", url),
                     ("sonarr_api_key", "k"), ("data_dir", tmp.name)):
            set_setting(self.db, k, v)
        # No TMDB lookups: tvdb resolution is not what these tests are about.
        p = mock.patch("services.media_requests._tmdb_service", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        # Discover's "requested" badge cache: not what these tests are about.
        import routers.discover
        p2 = mock.patch.object(routers.discover, "bust_arr_ids_cache", lambda: None)
        p2.start()
        self.addCleanup(p2.stop)

    def set(self, key, value):
        from models.database import set_setting
        set_setting(self.db, key, value)

    def lists(self):
        import routers.lists as lists
        return lists

    def call(self, fn, *args, **kw):
        from fastapi import HTTPException
        try:
            return fn(*args, **kw)
        except HTTPException as e:
            return {"_http": e.status_code, "detail": e.detail}

    def posts(self, path):
        return [b for m, p, b in _FakeArr.requests if m == "POST" and p == path]

    def add_list(self, items):
        from models.database import ListSubscription, ListItem
        self.db.add(ListSubscription(id=1, user_id=1, name="L", type="trakt", url="x", tag="t",
                                     auto_add_radarr=True))
        for tmdb_id, media_type in items:
            self.db.add(ListItem(list_id=1, tmdb_id=tmdb_id, media_type=media_type, title=f"T{tmdb_id}"))
        self.db.commit()

    # Each movie entry point, driven the way its caller drives it.
    def movie_entry_points(self):
        lists = self.lists()
        B = lists.AddMissingBody
        return {
            "dashboard": lambda tid, **kw: lists.add_to_radarr(B(tmdb_ids=[tid], **kw), self.db, self.user, DASHBOARD),
            "plugin (web / Android TV)": lambda tid, **kw: lists.add_to_radarr(B(tmdb_ids=[tid], **kw), self.db,
                                                                              self.user, PLUGIN),
            "API": lambda tid, **kw: lists.add_to_radarr(B(tmdb_ids=[tid], **kw), self.db, self.user),
            "list add-missing": lambda tid, **kw: lists.add_missing_to_radarr(1, B(tmdb_ids=[tid], **kw),
                                                                             self.db, self.user),
        }

    def series_entry_points(self):
        # Sonarr's lookup only accepts the show whose id was asked for, so these
        # use the fake's show (tmdb 7 / tvdb 5); the id argument is ignored.
        lists = self.lists()
        B = lists.AddMissingBody
        return {
            "dashboard": lambda _, **kw: lists.add_to_sonarr(B(tmdb_ids=[7], **kw), self.db, self.user, DASHBOARD),
            "plugin (web / Android TV)": lambda _, **kw: lists.add_to_sonarr(B(tmdb_ids=[7], **kw), self.db,
                                                                            self.user, PLUGIN),
            "plugin, TVDB-only title": lambda _, **kw: lists.add_to_sonarr(B(tvdb_ids=[5], **kw), self.db,
                                                                          self.user, PLUGIN),
            "list add-missing": lambda _, **kw: lists.add_missing_to_sonarr(1, B(tmdb_ids=[7], **kw),
                                                                           self.db, self.user),
        }

    def fetch_list(self):
        lists = self.lists()
        items = [{"tmdb_id": 101, "media_type": "movie"}, {"tmdb_id": 202, "media_type": "series"}]
        with mock.patch.object(lists, "refresh_list", return_value=(items, {"stored": 2, "tagged": 0,
                                                                            "skipped_no_tmdb": 0,
                                                                            "skipped_duplicate": 0})), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None):
            return lists.fetch_list(1, self.db, self.user)


class TestDefaultsApplyEverywhere(_Base):
    def test_every_movie_entry_point_uses_the_default_profile(self):
        self.add_list([])
        self.set("radarr_quality_profile_id", "6")
        for n, (name, add) in enumerate(self.movie_entry_points().items()):
            _FakeArr.requests = []
            resp = self.call(add, 1000 + n)
            sent = self.posts("/api/v3/movie")
            self.assertEqual(len(sent), 1, f"{name}: {resp}")
            self.assertEqual(sent[0]["qualityProfileId"], 6, name)

    def test_list_auto_add_uses_the_default_profile_and_only_movies(self):
        self.add_list([])
        self.set("radarr_quality_profile_id", "6")
        resp = self.fetch_list()
        sent = self.posts("/api/v3/movie")
        # 202 is a series: its TMDB id names something else in Radarr's movie namespace.
        self.assertEqual([b["tmdbId"] for b in sent], [101], resp)
        self.assertEqual(sent[0]["qualityProfileId"], 6)
        self.assertEqual(resp["radarr_added"], 1, resp)

    def test_every_series_entry_point_uses_the_default_profile(self):
        self.add_list([])
        self.set("sonarr_quality_profile_id", "6")
        for n, (name, add) in enumerate(self.series_entry_points().items()):
            _FakeArr.requests = []
            resp = self.call(add, 2000 + n)
            sent = self.posts("/api/v3/series")
            self.assertEqual(len(sent), 1, f"{name}: {resp}")
            self.assertEqual(sent[0]["qualityProfileId"], 6, name)

    def test_changing_the_setting_changes_the_next_request_from_every_entry_point(self):
        self.add_list([])
        for profile in ("6", "9"):
            self.set("radarr_quality_profile_id", profile)
            self.set("sonarr_quality_profile_id", profile)
            for n, (name, add) in enumerate(self.movie_entry_points().items()):
                _FakeArr.requests = []
                self.call(add, 3000 + n)
                self.assertEqual(self.posts("/api/v3/movie")[0]["qualityProfileId"], int(profile), name)
            for n, (name, add) in enumerate(self.series_entry_points().items()):
                _FakeArr.requests = []
                self.call(add, 4000 + n)
                self.assertEqual(self.posts("/api/v3/series")[0]["qualityProfileId"], int(profile), name)
            _FakeArr.requests = []
            self.db.query(__import__("models.database", fromlist=["Movie"]).DownloadRequest).delete()
            self.fetch_list()
            self.assertEqual(self.posts("/api/v3/movie")[0]["qualityProfileId"], int(profile), "list auto-add")


class TestOverrides(_Base):
    def test_legacy_quality_profile_id_is_ignored(self):
        # Every older client sent the *arr's first profile ("Any") whether or not
        # the user touched the picker.
        self.add_list([])
        self.set("radarr_quality_profile_id", "6")
        self.set("sonarr_quality_profile_id", "6")
        with self.assertLogs("services.media_requests", "INFO") as logs:
            for n, (name, add) in enumerate(self.movie_entry_points().items()):
                _FakeArr.requests = []
                self.call(add, 5000 + n, quality_profile_id=1)
                self.assertEqual(self.posts("/api/v3/movie")[0]["qualityProfileId"], 6, name)
            for n, (name, add) in enumerate(self.series_entry_points().items()):
                _FakeArr.requests = []
                self.call(add, 6000 + n, quality_profile_id=1)
                self.assertEqual(self.posts("/api/v3/series")[0]["qualityProfileId"], 6, name)
        self.assertTrue(any("Ignored quality_profile_id=1" in line for line in logs.output))

    def test_explicit_override_wins_and_is_logged(self):
        self.add_list([])
        self.set("radarr_quality_profile_id", "6")
        with self.assertLogs("services.media_requests", "INFO") as logs:
            for n, (name, add) in enumerate(self.movie_entry_points().items()):
                _FakeArr.requests = []
                self.call(add, 7000 + n, quality_profile_override=9)
                self.assertEqual(self.posts("/api/v3/movie")[0]["qualityProfileId"], 9, name)
        self.assertTrue(any("Ultra-HD (chosen for this request)" in line for line in logs.output), logs.output)

    def test_the_log_says_where_the_request_came_from(self):
        self.set("radarr_quality_profile_id", "6")
        lists = self.lists()
        with self.assertLogs("services.media_requests", "INFO") as logs:
            lists.add_to_radarr(lists.AddMissingBody(tmdb_ids=[1]), self.db, self.user, PLUGIN)
            lists.add_to_radarr(lists.AddMissingBody(tmdb_ids=[2]), self.db, self.user, DASHBOARD)
        text = "\n".join(logs.output)
        self.assertIn("via the Jellyfin plugin", text)
        self.assertIn("via Tentacle's dashboard", text)
        self.assertIn("HD-1080p (default)", text)

    def test_an_explicit_choice_works_without_a_default(self):
        lists = self.lists()
        resp = self.call(lists.add_to_radarr, lists.AddMissingBody(tmdb_ids=[1], quality_profile_override=9),
                         self.db, self.user)
        self.assertEqual(resp.get("added"), 1, resp)
        self.assertEqual(self.posts("/api/v3/movie")[0]["qualityProfileId"], 9)


class TestNoDefaultIsRefused(_Base):
    def test_every_entry_point_refuses_and_sends_nothing(self):
        self.add_list([])
        for name, add in {**self.movie_entry_points(), **self.series_entry_points()}.items():
            _FakeArr.requests = []
            resp = self.call(add, 42, quality_profile_id=1)  # a legacy id is not a choice
            self.assertEqual(resp.get("_http"), 400, f"{name}: {resp}")
            self.assertIn("Pick a default", resp["detail"], name)
            self.assertEqual(_FakeArr.requests, [], f"{name} still talked to the *arr")

    def test_list_auto_add_reports_the_refusal_without_failing_the_fetch(self):
        self.add_list([])
        resp = self.fetch_list()
        self.assertTrue(resp["success"])
        self.assertIn("Pick a default", resp["radarr_error"])
        self.assertEqual(self.posts("/api/v3/movie"), [])

    def test_a_default_deleted_in_radarr_is_refused(self):
        self.set("radarr_quality_profile_id", "4")
        lists = self.lists()
        resp = self.call(lists.add_to_radarr, lists.AddMissingBody(tmdb_ids=[1]), self.db, self.user)
        self.assertEqual(resp.get("_http"), 400, resp)
        self.assertIn("no longer exists", resp["detail"])
        self.assertEqual(self.posts("/api/v3/movie"), [])

    def test_sonarr_service_has_no_silent_profile(self):
        from services.sonarr import SonarrService
        svc = SonarrService("http://127.0.0.1:9", "k")
        self.assertIsNone(svc.add_series(tvdb_id=5, root_folder="/tv"))
        self.assertIn("No quality profile", svc.last_error)

    def test_radarr_service_has_no_add_with_a_default_profile(self):
        from services.radarr import RadarrService
        self.assertFalse(hasattr(RadarrService, "add_movie"),
                         "RadarrService.add_movie defaulted to profile 1; adds go through media_requests")


class TestRootFolders(_Base):
    def test_configured_root_folder_is_used(self):
        self.set("radarr_quality_profile_id", "6")
        self.set("radarr_root_folder", "/movies-4k")
        lists = self.lists()
        lists.add_to_radarr(lists.AddMissingBody(tmdb_ids=[1]), self.db, self.user)
        self.assertEqual(self.posts("/api/v3/movie")[0]["rootFolderPath"], "/movies-4k")
        self.assertNotIn(("GET", "/api/v3/rootfolder", None), _FakeArr.requests)

    def test_automatic_root_folder_skips_vod_folders(self):
        self.set("radarr_quality_profile_id", "6")
        lists = self.lists()
        lists.add_to_radarr(lists.AddMissingBody(tmdb_ids=[1]), self.db, self.user)
        self.assertEqual(self.posts("/api/v3/movie")[0]["rootFolderPath"], "/movies")

    def test_sonarr_configured_root_folder_is_used(self):
        self.set("sonarr_quality_profile_id", "6")
        self.set("sonarr_root_folder", "/tv-anime")
        lists = self.lists()
        lists.add_to_sonarr(lists.AddMissingBody(tmdb_ids=[7]), self.db, self.user)
        self.assertEqual(self.posts("/api/v3/series")[0]["rootFolderPath"], "/tv-anime")


class TestProfilesEndpoint(_Base):
    def test_profiles_mark_the_default_for_client_pickers(self):
        self.set("radarr_quality_profile_id", "6")
        lists = self.lists()
        profiles = lists.radarr_profiles(self.db, self.user)
        self.assertEqual([p["id"] for p in profiles if p["is_default"]], [6])
        self.assertTrue(all("is_default" in p for p in lists.sonarr_profiles(self.db, self.user)))


class TestPickedEpisodesNotApplied(_Base):
    """#532: a series Sonarr added whose picked episodes could not be monitored
    reads as failed with the reason, and is still recorded as requested."""

    REASON = "Added to Sonarr, but Sonarr had not finished setting the show up."

    def request(self, **ids):
        from services import media_requests
        from services.sonarr import SonarrService

        def add_series(svc, *a, **kw):
            svc.last_error = self.REASON
            return {"id": 3, "path": "/tv/Show", "tmdbId": 7}

        self.set("sonarr_quality_profile_id", "6")
        with mock.patch.object(SonarrService, "add_series", autospec=True, side_effect=add_series), \
             mock.patch.object(media_requests, "_bust_discover_cache") as bust:
            out = media_requests.request_series(self.db, user_id=1, via="test",
                                                selected_episodes=[{"season": 1, "episode": 2}], **ids)
        return out, bust

    def check(self, out, bust, added_id, request_id):
        from models.database import DownloadRequest
        resp = out.as_response()
        self.assertEqual((resp["added"], resp["failed"]), (0, 1), resp)
        self.assertEqual(resp["detail"], self.REASON)
        self.assertEqual(out.added, [added_id])
        bust.assert_called_once()
        self.assertEqual([r.tmdb_id for r in self.db.query(DownloadRequest).all()], [request_id])

    def test_tmdb_title(self):
        self.check(*self.request(tmdb_ids=[7]), 7, 7)

    def test_tvdb_only_title(self):
        self.check(*self.request(tvdb_ids=[5]), -5, 7)


ROOT = Path(__file__).resolve().parents[2]


class TestClientsSendNoProfileOfTheirOwn(unittest.TestCase):
    """The clients' pickers start at "Default" and send only an explicit choice."""

    def read(self, rel):
        return (ROOT / rel).read_text()

    def test_dashboard_and_plugin_never_send_the_legacy_field(self):
        for rel in ("tentacle/static/js/pages.js", "tentacle-plugin/Inject/tentacle-discover.js"):
            src = self.read(rel)
            self.assertNotIn("quality_profile_id", src, rel)
            self.assertIn("quality_profile_override", src, rel)

    def test_pickers_offer_default_first(self):
        self.assertIn('Default — ${escapeAttr(def.name)}', self.read("tentacle/static/js/pages.js"))
        self.assertIn("'<option value=\"\">Default \\u2014 ' + esc(def.name)",
                      self.read("tentacle-plugin/Inject/tentacle-discover.js"))

    def test_plugin_forwards_the_body_untouched(self):
        cs = self.read("tentacle-plugin/Api/DiscoverController.cs")
        for action in ("AddToRadarr", "AddToSonarr"):
            block = cs[cs.index(f'[HttpPost("{action}")]'):]
            block = block[:block.index("[HttpGet(")]
            self.assertIn("body.GetRawText()", block, action)


if __name__ == "__main__":
    unittest.main()
