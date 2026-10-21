"""The sign-in screens: who is listed, where, and signing in with a code.

Run from the tentacle/ directory:  python -m unittest tests.test_sign_in_screen

Lucas, 2026-10-21: both sign-in pages "only show 3 users". Jellyfin's
/Users/Public, which both read, leaves out every account "hidden from login
screens", and that is Jellyfin's default for a new account: 4 of his 7 had
it, Tentacle's own invites included. So invites now make shown accounts and
the People page has a switch for it ("On sign-in screen", Jellyfin's
IsHidden, the one place it is kept).

Names are shown only at home: a server on the internet would otherwise tell
anyone who opens its address who lives there. Tentacle decides "at home"
from the browser's address, through any proxy or tunnel in between; the
plugin does the same for Jellyfin's own page with Jellyfin's rule
(PublicUsersFilter, checked here by its source as there is no C# test host).
"""
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import family as F, sign_in
from tests.test_family import ANN, Case, FakeJellyfin
from tmp_dirs import temp_dir

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"
STATIC = Path(__file__).resolve().parents[1] / "static"


def req(peer, **headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None,
                           headers={k.lower().replace("_", "-"): v for k, v in headers.items()})


class AtHome(unittest.TestCase):
    def test_a_browser_on_the_home_network(self):
        for peer in ("192.168.2.40", "10.0.0.7", "172.20.1.1", "127.0.0.1", "::1", "fd00::5", "::ffff:192.168.1.9"):
            self.assertTrue(sign_in.at_home(req(peer)), peer)

    def test_tailscale_counts_as_home(self):
        self.assertTrue(sign_in.at_home(req("100.101.12.3")))

    def test_a_browser_on_the_internet(self):
        for peer in ("8.8.8.8", "2001:db8::1", None, "not-an-address"):
            self.assertFalse(sign_in.at_home(req(peer)), peer)

    def test_through_a_tunnel_the_browser_counts_not_the_tunnel(self):
        # cloudflared connects from inside the home; the real browser is in its headers
        self.assertFalse(sign_in.at_home(req("172.18.0.5", cf_connecting_ip="203.0.113.9",
                                             x_forwarded_for="203.0.113.9")))
        self.assertFalse(sign_in.at_home(req("192.168.2.1", x_real_ip="203.0.113.9")))
        self.assertFalse(sign_in.at_home(req("192.168.2.1", forwarded='for="[2001:db8::7]:4711";proto=https')))

    def test_a_forged_home_address_in_front_of_the_real_one_does_not_count(self):
        # a proxy appends the address it saw: "what the browser claimed, what it was"
        self.assertFalse(sign_in.at_home(req("192.168.2.1", x_forwarded_for="192.168.2.50, 203.0.113.9")))

    def test_a_reverse_proxy_at_home_for_a_browser_at_home(self):
        self.assertTrue(sign_in.at_home(req("172.18.0.5", x_forwarded_for="192.168.2.50",
                                            forwarded="for=192.168.2.50:5000")))

    def test_an_unreadable_header_means_away(self):
        self.assertFalse(sign_in.at_home(req("192.168.2.1", x_forwarded_for="unknown")))


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            import requests
            r = requests.Response()
            r.status_code = self.status_code
            raise requests.HTTPError(response=r)

    def json(self):
        return self.body


PUBLIC = [{"Id": "u1", "Name": "Ann", "HasPassword": True, "PrimaryImageTag": None},
          {"Id": "u2", "Name": "Ben", "HasPassword": False, "PrimaryImageTag": "t"}]


class DbCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "jellyfin_url", "http://jf.lan:8096")
        self.db.commit()


class ThePicker(DbCase):
    def users(self, request, codes=True):
        from routers import auth

        def get(url, **kw):
            if url.endswith("/Users/Public"):
                return _Resp(PUBLIC)
            if url.endswith("/QuickConnect/Enabled"):
                return _Resp(codes)
            raise AssertionError(url)
        with mock.patch.object(auth.requests, "get", side_effect=get):
            return auth.get_jellyfin_users(request, db=self.db)

    def test_at_home_everyone_jellyfin_shows(self):
        out = self.users(req("192.168.2.40"))
        self.assertEqual(["Ann", "Ben"], [p["name"] for p in out["people"]])
        self.assertEqual((True, True), (out["at_home"], out["quick_connect"]))

    def test_away_no_names_but_still_a_way_in(self):
        out = self.users(req("192.168.2.1", cf_connecting_ip="203.0.113.9"))
        self.assertEqual([], out["people"])
        self.assertFalse(out["at_home"])
        self.assertNotIn("Ann", json.dumps(out))

    def test_codes_off(self):
        self.assertFalse(self.users(req("192.168.2.40"), codes=False)["quick_connect"])


class QuickConnect(DbCase):
    SECRET = "s3cr3t-from-jellyfin"

    def setUp(self):
        super().setUp()
        from routers import auth
        self.auth = auth
        self.approved = False
        self.posts = []

        def post(url, **kw):
            self.posts.append((url, kw))
            if url.endswith("/QuickConnect/Initiate"):
                return _Resp({"Code": "482913", "Secret": self.SECRET})
            if url.endswith("/Users/AuthenticateWithQuickConnect"):
                assert kw["json"] == {"Secret": self.SECRET}
                return _Resp({"User": {"Id": "a" * 32, "Name": "Ann", "Policy": {"IsAdministrator": True}},
                              "AccessToken": "t"})
            raise AssertionError(url)

        def get(url, params=None, **kw):
            if url.endswith("/QuickConnect/Connect"):
                if params["secret"] != self.SECRET:
                    return _Resp({}, 404)
                return _Resp({"Authenticated": self.approved})
            raise AssertionError(url)
        for name, fn in (("post", post), ("get", get)):
            p = mock.patch.object(auth.requests, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_the_browser_gets_the_code_and_a_handle_never_the_secret(self):
        out = self.auth.quick_connect_start(db=self.db)
        self.assertEqual("482913", out["code"])
        self.assertNotIn(self.SECRET, json.dumps(out))
        self.assertIn("Authorization", self.posts[0][1]["headers"])

    def test_waiting_then_signed_in(self):
        handle = self.auth.quick_connect_start(db=self.db)["handle"]
        self.assertEqual({"approved": False}, self.auth.quick_connect_poll(handle, mock.Mock(), req("8.8.8.8"), self.db))
        self.approved = True
        with mock.patch.object(self.auth, "_finish_sign_in", return_value={"display_name": "Ann"}) as fin:
            out = self.auth.quick_connect_poll(handle, mock.Mock(), req("8.8.8.8"), self.db)
        self.assertEqual({"approved": True, "user": {"display_name": "Ann"}}, out)
        self.assertEqual("Ann", fin.call_args[0][0]["User"]["Name"])
        with self.assertRaises(HTTPException) as e:   # used up
            self.auth.quick_connect_poll(handle, mock.Mock(), req("8.8.8.8"), self.db)
        self.assertEqual(410, e.exception.status_code)

    def test_an_approved_code_really_signs_in(self):
        """End to end through _finish_sign_in: a Tentacle user and a session cookie."""
        from fastapi import Request, Response
        mdb.set_setting(self.db, "session_secret", "s" * 64)
        self.db.commit()
        handle = self.auth.quick_connect_start(db=self.db)["handle"]
        self.approved = True
        response = Response()
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        with mock.patch.object(self.auth, "_build_playlists_for_new_user"), \
                mock.patch.object(self.auth, "migrate_orphaned_data_to_user"):
            out = self.auth.quick_connect_poll(handle, response, request, self.db)
        self.assertEqual("Ann", out["user"]["display_name"])
        self.assertEqual(1, self.db.query(mdb.TentacleUser).count())
        self.assertIn("tentacle_session=", response.headers.get("set-cookie", ""))

    def test_a_made_up_handle(self):
        with self.assertRaises(HTTPException) as e:
            self.auth.quick_connect_poll("guess", mock.Mock(), req("8.8.8.8"), self.db)
        self.assertEqual(410, e.exception.status_code)

    def test_codes_switched_off_in_jellyfin(self):
        with mock.patch.object(self.auth.requests, "post", return_value=_Resp({}, 401)):
            with self.assertRaises(HTTPException) as e:
                self.auth.quick_connect_start(db=self.db)
        self.assertEqual(409, e.exception.status_code)
        self.assertIn("People", e.exception.detail)

    def test_pending_codes_are_capped(self):
        handles = [sign_in.remember(f"s{i}") for i in range(sign_in._QC_MAX + 5)]
        self.assertIsNone(sign_in.secret_for(handles[0]))
        self.assertEqual(f"s{sign_in._QC_MAX + 4}", sign_in.secret_for(handles[-1]))


class HiddenJellyfin(FakeJellyfin):
    """Jellyfin's own default: a new account is hidden from login screens."""
    def post(self, path, body=None, **p):
        out = super().post(path, body, **p)
        if path == "/Users/New":
            self.users["d" * 32]["Policy"]["IsHidden"] = True
            out["Policy"]["IsHidden"] = True
        return out


class OnTheSignInScreen(Case):
    def test_an_invited_person_is_shown(self):
        self.jf.__class__ = HiddenJellyfin
        with mock.patch("services.family.start_new_person"):
            out = F.invite(self.db, self.admin, "Grandma Rose")
        policy = self.jf.users["d" * 32]["Policy"]
        self.assertIs(False, policy["IsHidden"])
        self.assertEqual([], policy["BlockedTags"], "the rest of the policy is kept")
        self.assertTrue(out["invite"]["password"])

    def test_the_admin_hides_and_shows_someone(self):
        before = dict(self.jf.users[ANN]["Policy"])
        F.set_on_sign_in(self.db, self.ann, False)
        self.assertIs(True, self.jf.users[ANN]["Policy"]["IsHidden"])
        self.assertEqual(before, {k: v for k, v in self.jf.users[ANN]["Policy"].items() if k != "IsHidden"})
        F.set_on_sign_in(self.db, self.ann, True)
        self.assertIs(False, self.jf.users[ANN]["Policy"]["IsHidden"])

    def test_nothing_is_written_when_nothing_changes(self):
        self.jf.users[ANN]["Policy"]["IsHidden"] = False
        F.set_on_sign_in(self.db, self.ann, True)
        self.assertNotIn("/Policy", " ".join(c[1] for c in self.jf.calls if c[0] == "POST"))

    def test_the_people_page_says_who_is_on_it(self):
        self.jf.users[ANN]["Policy"]["IsHidden"] = True
        self.assertIs(False, F.person(self.db, self.ann, self.jf.users[ANN])["on_sign_in"])
        self.jf.users[ANN]["Policy"]["IsHidden"] = False
        self.assertIs(True, F.person(self.db, self.ann, self.jf.users[ANN])["on_sign_in"])

    def test_the_route_takes_the_switch(self):
        from routers import family as R
        out = R.update_person(self.ann.id, R.PersonBody(on_sign_in=False), admin=self.admin, db=self.db)
        self.assertIs(False, out["on_sign_in"])
        self.assertIs(True, self.jf.users[ANN]["Policy"]["IsHidden"])


class PluginLoginPage(unittest.TestCase):
    def setUp(self):
        self.filter = (PLUGIN / "Services" / "PublicUsersFilter.cs").read_text(encoding="utf-8")

    def test_jellyfins_public_list_is_the_one_filtered(self):
        self.assertIn('action.ControllerName == "User"', self.filter)
        self.assertIn('action.ActionName == "GetPublicUsers"', self.filter)

    def test_with_jellyfins_own_idea_of_the_local_network(self):
        self.assertIn("GetNormalizedRemoteIP()", self.filter)
        self.assertIn("_network.IsInLocalNetwork(http.GetNormalizedRemoteIP())", self.filter)

    def test_a_tunnel_does_not_make_the_internet_local(self):
        # Through Cloudflare Tunnel Jellyfin sees the tunnel's home address;
        # the browser's is in the headers (Known proxies is usually empty).
        headers = re.search(r"ForwardHeaders\s*=\s*\{([^}]*)\}", self.filter).group(1)
        for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded"):
            self.assertIn(f'"{h}"', headers)
        self.assertIn("ParseAddress(addr) is not IPAddress ip || !_network.IsInLocalNetwork(ip)", self.filter)

    def test_both_rules_read_the_same_headers(self):
        headers = re.search(r"ForwardHeaders\s*=\s*\{([^}]*)\}", self.filter).group(1)
        plugin = sorted(h.lower() for h in re.findall(r'"([^"]+)"', headers))
        self.assertEqual(sorted(sign_in._FORWARD_HEADERS), plugin)

    def test_unsure_means_no_names(self):
        catch = self.filter[self.filter.index("catch (Exception"):]
        self.assertIn("Array.Empty<object>()", catch[:catch.index("}")])

    def test_the_filter_is_registered(self):
        reg = (PLUGIN / "PluginServiceRegistrator.cs").read_text(encoding="utf-8")
        self.assertIn("options.Filters.AddService<PublicUsersFilter>()", reg)
        self.assertIn("AddSingleton<PublicUsersFilter>()", reg)

    def test_the_login_look_is_embedded_served_and_injected(self):
        proj = (PLUGIN / "Jellyfin.Plugin.Tentacle.csproj").read_text(encoding="utf-8")
        ctrl = (PLUGIN / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")
        patch = (PLUGIN / "Patching" / "IndexHtmlPatch.cs").read_text(encoding="utf-8")
        for ext in ("js", "css"):
            self.assertIn(f'Include="Inject/tentacle-login.{ext}"', proj)
            self.assertIn(f'"/Tentacle/login.{ext}"', ctrl)
            self.assertIn(f"/Tentacle/login.{ext}?v={{cacheBust}}", patch)
        self.assertRegex(patch, r"\{loginCssTag\}</head>")
        self.assertRegex(patch, r"\{loginJsTag\}</body>")

    def test_both_pages_give_a_person_the_same_colour(self):
        app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        login = (PLUGIN / "Inject" / "tentacle-login.js").read_text(encoding="utf-8")
        colours = lambda src, name: re.search(name + r"\s*=\s*(\[[^\]]*\])", src).group(1)
        self.assertEqual(json.loads(colours(app, "LOGIN_COLORS").replace("'", '"')),
                         json.loads(colours(login, "COLORS").replace("'", '"')))

    def test_the_login_styles_touch_only_the_login_page(self):
        css = (PLUGIN / "Inject" / "tentacle-login.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        css = re.sub(r"@media[^{]*\{", "", css)
        sels = re.findall(r"([^{}]+)\{", css)
        self.assertGreater(len(sels), 20)
        for sel in sels:
            for part in sel.split(","):
                self.assertTrue(part.strip().startswith("#loginPage.tentacle-login"), part)


if __name__ == "__main__":
    unittest.main()
