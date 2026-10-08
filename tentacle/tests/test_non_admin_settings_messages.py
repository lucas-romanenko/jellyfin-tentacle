"""#554: a non-admin is never told to fix something in Settings.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Settings is admin-only (hidden from the sidebar, its API answers 403), yet a
non-admin who pressed Add to Radarr / Sonarr with no default quality profile
picked was told "Pick a default Radarr quality profile in Tentacle's settings
(Settings → Connections)", the picker read "set one in Settings", an empty
Discover said "Check your TMDB bearer token in Settings", and the sign-in
screen (nobody signed in yet) said "Cannot connect to Jellyfin. Check Settings.".
A non-admin is now told an admin has to set it; an admin keeps the precise text.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from dashboard_js import HAVE_NODE, Page, functions, render  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


class _FakeArr(BaseHTTPRequestHandler):
    profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
    posts = []

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
        if path == "/api/v3/qualityprofile":
            return self._answer(200, _FakeArr.profiles)
        if path == "/api/v3/rootfolder":
            return self._answer(200, [{"id": 1, "path": "/media"}])
        self._answer(200, [])

    def do_POST(self):
        _FakeArr.posts.append(urlparse(self.path).path)
        self._answer(201, {"id": 1})


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import TentacleUser, set_setting
        _FakeArr.posts = []
        srv = HTTPServer(("127.0.0.1", 0), _FakeArr)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}"

        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = TentacleUser(id=1, jellyfin_user_id="a1", display_name="admin", is_admin=True)
        self.nora = TentacleUser(id=2, jellyfin_user_id="n2", display_name="nora", is_admin=False)
        self.db.add_all([self.admin, self.nora])
        self.db.commit()
        for k, v in (("radarr_url", url), ("radarr_api_key", "k"),
                     ("sonarr_url", url), ("sonarr_api_key", "k"), ("data_dir", tmp)):
            set_setting(self.db, k, v)

    def refused(self, service, user):
        """The 400/503 detail the add route answers this user with."""
        from fastapi import HTTPException
        import routers.lists as lists
        fn = lists.add_to_radarr if service == "radarr" else lists.add_to_sonarr
        try:
            resp = fn(lists.AddMissingBody(tmdb_ids=[7]), self.db, user)
        except HTTPException as e:
            self.assertEqual([], _FakeArr.posts, "a refused request still reached the *arr")
            return e.status_code, e.detail
        self.fail(f"the add was not refused: {resp}")

    def assert_tells_an_admin(self, detail, name):
        self.assertNotIn("Settings →", detail, f"a non-admin is sent to Settings: {detail!r}")
        self.assertNotIn("Pick ", detail, f"a non-admin is told to pick something: {detail!r}")
        self.assertIn("admin", detail.lower(), f"a non-admin isn't told an admin has to set it: {detail!r}")
        self.assertIn(name, detail, detail)
        self.assertIn("quality profile", detail, detail)


class NoDefaultProfile(_Base):
    def test_non_admin_is_told_an_admin_has_to_pick_one(self):
        for service, name in (("radarr", "Radarr"), ("sonarr", "Sonarr")):
            with self.subTest(service):
                status, detail = self.refused(service, self.nora)
                self.assertEqual(400, status)
                self.assert_tells_an_admin(detail, name)

    def test_admin_keeps_the_precise_text(self):
        from services.media_requests import no_default_message
        for service, name in (("radarr", "Radarr"), ("sonarr", "Sonarr")):
            with self.subTest(service):
                self.assertEqual(
                    f"Pick a default {name} quality profile in Tentacle's settings (Settings → Connections). "
                    f"Tentacle won't guess one: {name}'s first profile is usually \"Any\".",
                    no_default_message(service))
                self.assertEqual((400, no_default_message(service)), self.refused(service, self.admin))


class DefaultProfileGone(_Base):
    """The default picked in Settings no longer exists in the *arr."""

    def setUp(self):
        super().setUp()
        from models.database import set_setting
        set_setting(self.db, "radarr_quality_profile_id", "9")
        set_setting(self.db, "sonarr_quality_profile_id", "9")

    def test_non_admin_is_told_an_admin_has_to_pick_another(self):
        for service, name in (("radarr", "Radarr"), ("sonarr", "Sonarr")):
            with self.subTest(service):
                status, detail = self.refused(service, self.nora)
                self.assertEqual(400, status)
                self.assert_tells_an_admin(detail, name)

    def test_admin_keeps_the_precise_text(self):
        for service, name in (("radarr", "Radarr"), ("sonarr", "Sonarr")):
            with self.subTest(service):
                self.assertEqual(
                    (400, f"Your default {name} quality profile (id 9) no longer exists in {name}. "
                          f"Pick another in Tentacle's settings (Settings → Connections)."),
                    self.refused(service, self.admin))


class LidarrDefaultsMissing(_Base):
    def test_each_user_gets_their_text(self):
        from services.media_requests import RequestRefused, _lidarr_defaults
        with self.assertRaises(RequestRefused) as cm:
            _lidarr_defaults(self.db)
        e = cm.exception
        self.assertIn("Settings →", e.for_user(self.admin))
        text = e.for_user(self.nora)
        self.assertNotIn("Settings", text, text)
        self.assertIn("an admin has to pick a Lidarr", text)
        # No user (an internal caller): the admin text, as in the log.
        self.assertEqual(e.message, e.for_user(None))


ESC = functions("pages.js", ["escapeAttr"]) + functions("app.js", ["escHtml"])


def _user(is_admin):
    return "const state = {currentUser: {is_admin: %s}};" % json.dumps(is_admin)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DashboardTexts(unittest.TestCase):
    def picker_first_option(self, is_admin):
        src = _user(is_admin) + ESC + functions("pages.js", ["_profileOptionsHtml"])
        html = render(src, "", "written.x = [_profileOptionsHtml([{id: 1, name: 'Any', is_default: false}])]")["x"][0]
        return Page(html.split("</option>")[0] + "</option>").text

    def test_quality_picker_without_a_default(self):
        self.assertEqual("Default (none picked yet — set one in Settings)", self.picker_first_option(True))
        text = self.picker_first_option(False)
        self.assertNotIn("Settings", text, f"a non-admin is told to set one in Settings: {text!r}")
        self.assertIn("admin", text.lower(), text)

    def empty_discover(self, is_admin):
        src = (_user(is_admin) + ESC
               + "let _discoverType = 'movies', _activityData = {}, _discoverSections = [];\n"
               + functions("pages.js", ["loadDiscover"]))
        html = render(src, "API['/api/discover'] = {sections: []};", "await loadDiscover()")["discover-grid"][-1]
        return Page(html).text

    def test_empty_discover(self):
        self.assertEqual("No content found. Check your TMDB bearer token in Settings.", self.empty_discover(True))
        text = self.empty_discover(False)
        self.assertNotIn("Settings", text, f"a non-admin is sent to Settings: {text!r}")
        self.assertIn("admin", text.lower(), text)

    def test_sign_in_screen_when_jellyfin_does_not_answer(self):
        # Nobody is signed in on the sign-in screen, so nobody can open Settings.
        src = ESC + functions("app.js", ["showLoginOverlay"])
        setup = ("async function fetch() { return {ok: false, status: 502, json: async () => ({})}; }\n"
                 "function loginShowManual() {}")
        text = Page(render(src, setup, "await showLoginOverlay()")["login-user-grid"][-1]).text
        self.assertIn("Cannot connect to Jellyfin", text)
        self.assertNotIn("Settings", text, f"the sign-in screen sends people to Settings: {text!r}")


if __name__ == "__main__":
    unittest.main()
