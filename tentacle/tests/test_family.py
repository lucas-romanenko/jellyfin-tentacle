"""Family features (Roadmap 1c): invites, kids profiles, the defaults new
people start with, TV sign-in codes, requests that wait for an admin,
problems told to the admin in plain words for the family, and the TV app's
download link.

Run from the tentacle/ directory:  python -m unittest tests.test_family

Jellyfin is a fake that answers like 10.11.8 did where it matters
(checked against its source, Jellyfin.Api/Controllers/UserController.cs and
QuickConnectController.cs): /Users/New takes {Name, Password}; a policy POST
replaces the whole policy; /QuickConnect/Authorize?code=&userId= answers 404
for a code no device is showing and 401 while Quick Connect is off.
"""
import json
from datetime import datetime
import logging
import re
import unittest
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import admin_alerts, approvals, family as F, livetv_lineups as L, tv_app
from tmp_dirs import temp_dir

ADMIN, ANN, BEN = "a" * 32, "b" * 32, "c" * 32


def _http_error(status):
    r = requests.Response()
    r.status_code = status
    return requests.HTTPError(response=r)


class FakeJellyfin:
    def __init__(self, users):
        self.users = {}
        for uid, name, admin in users:
            self.users[uid] = {"Id": uid, "Name": name, "Policy": {
                "IsAdministrator": admin, "EnableLiveTvAccess": True, "EnableLiveTvManagement": admin,
                "BlockedTags": ["Horror", L.ALL_TAG], "MaxParentalRating": None, "MaxParentalSubRating": None,
                "BlockUnratedItems": ["Book"], "IsDisabled": False, "EnableAllFolders": True}}
        self.passwords = {}
        self.codes = {"123456"}
        self.authorized = []
        self.quick_connect = True
        self.calls = []

    def get(self, path, **p):
        self.calls.append(("GET", path, p))
        if path == "/Users":
            return [json.loads(json.dumps(u)) for u in self.users.values()]
        if path.startswith("/Users/"):
            return json.loads(json.dumps(self.users[L.norm_user_id(path.split("/")[2])]))
        if path == "/QuickConnect/Enabled":
            return self.quick_connect
        if path == "/System/Configuration":
            return {"QuickConnectAvailable": self.quick_connect, "ServerName": "home"}
        if path == "/Devices":
            return {"Items": []}
        if path == "/System/Info/Public":
            return {"Version": "10.11.8"}
        if path == "/Library/VirtualFolders":
            return []
        raise AssertionError(path)

    def post(self, path, body=None, **p):
        self.calls.append(("POST", path, body, p))
        if path == "/Users/New":
            uid = "d" * 32
            self.users[uid] = {"Id": f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}",
                               "Name": body["Name"], "Policy": {"IsAdministrator": False, "EnableLiveTvAccess": True,
                                                                "BlockedTags": [], "BlockUnratedItems": []}}
            self.passwords[uid] = body["Password"]
            return json.loads(json.dumps(self.users[uid]))
        if path.endswith("/Policy"):
            self.users[L.norm_user_id(path.split("/")[2])]["Policy"] = json.loads(json.dumps(body))
            return None
        if path == "/Users/Password":
            self.passwords[L.norm_user_id(p["userId"])] = body["NewPw"]
            return None
        if path == "/QuickConnect/Authorize":
            if not self.quick_connect:
                raise _http_error(401)
            if p["code"] not in self.codes:
                raise _http_error(404)
            self.authorized.append((p["code"], p["userId"]))
            return True
        if path == "/System/Configuration":
            self.quick_connect = bool(body["QuickConnectAvailable"])
            return None
        raise AssertionError(path)


class Case(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        for i, name in enumerate(["News", "Cartoons", "Sport", "Toons 2"], 1):
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=str(100 + i),
                                        stream_url=f"http://192.0.2.10/live/{i}.ts", enabled=True, sort_order=i))
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.ann = mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=False)
        self.ben = mdb.TentacleUser(jellyfin_user_id=BEN, display_name="Ben", is_admin=False, request_mode="direct")
        self.db.add_all([self.admin, self.ann, self.ben])
        self.db.commit()
        for k, v in (("jellyfin_url", "http://jf.lan:8096"), ("jellyfin_api_key", "k"),
                     ("jellyfin_user_id", ADMIN), ("youtube_base_url", "http://tentacle.lan:8888")):
            mdb.set_setting(self.db, k, v)
        self.jf = FakeJellyfin([(ADMIN, "Lucas", True), (ANN, "Ann", False), (BEN, "Ben", False)])
        patches = [
            mock.patch("services.livetv_tuners.jellyfin_for", return_value=self.jf),
            mock.patch("services.livetv_tuners.schedule_sync"),
            mock.patch("services.pushover.send", return_value=False),
        ]
        for pt in patches:
            self.mocks = getattr(self, "mocks", []) + [pt.start()]
            self.addCleanup(pt.stop)
        self.push = self.mocks[2]


# ── Kids profiles ───────────────────────────────────────────────────────────

class KidsPolicy(unittest.TestCase):
    def test_kids_policy_sets_the_age_and_blocks_unrated(self):
        p = F.kids_policy({"BlockUnratedItems": ["Book"], "BlockedTags": ["x"], "EnableLiveTvAccess": True,
                           "MaxParentalRating": None}, "kids", live_tv=False)
        self.assertEqual(10, p["MaxParentalRating"])
        self.assertEqual(sorted({"Book", *F.KIDS_BLOCK_UNRATED}), p["BlockUnratedItems"])
        self.assertFalse(p["EnableLiveTvAccess"])
        self.assertEqual(["x"], p["BlockedTags"])

    def test_kids_with_live_tv_see_only_kids_channels_at_once(self):
        p = F.kids_policy({"BlockedTags": ["x"]}, "kids", live_tv=True)
        self.assertEqual(["x", L.ALL_TAG], p["BlockedTags"])
        self.assertEqual(["x", L.ALL_TAG], F.kids_policy(p, "teens", live_tv=True)["BlockedTags"])
        self.assertNotIn("LiveTvProgram", p["BlockUnratedItems"])   # Tentacle's guide has no ratings

    def test_levels_are_jellyfins_scores(self):
        # G/TV-Y 0 and TV-Y7 7; PG/TV-PG 10; PG-13 13, TV-14 14 (us.json, ca.json).
        self.assertEqual({"young": 7, "kids": 10, "teens": 14},
                         {k: v["score"] for k, v in F.KIDS_LEVELS.items()})

    def test_restore_puts_back_only_what_kids_changed(self):
        saved = {"MaxParentalRating": 17, "MaxParentalSubRating": None, "BlockUnratedItems": [],
                 "EnableLiveTvAccess": True}
        now = {"MaxParentalRating": 10, "BlockUnratedItems": ["Movie"], "EnableLiveTvAccess": False,
               "BlockedTags": ["new"]}
        p = F.restored_policy(now, saved)
        self.assertEqual((17, [], True, ["new"]), (p["MaxParentalRating"], p["BlockUnratedItems"],
                                                   p["EnableLiveTvAccess"], p["BlockedTags"]))


class Kids(Case):
    def test_on_without_kids_channels_turns_live_tv_off(self):
        F.set_kids(self.db, self.ann, "young")
        p = self.jf.users[ANN]["Policy"]
        self.assertEqual(7, p["MaxParentalRating"])
        self.assertFalse(p["EnableLiveTvAccess"])
        self.assertIn("Movie", p["BlockUnratedItems"])
        self.assertEqual(["Horror", L.ALL_TAG], p["BlockedTags"])   # Live TV tags left to the tuner sync
        self.assertEqual("young", self.ann.kids_level)

    def test_off_restores_the_policy(self):
        before = json.loads(json.dumps(self.jf.users[ANN]["Policy"]))
        F.set_kids_channels(self.db, ["c2", "c4"])
        F.set_kids(self.db, self.ann, "kids")
        self.assertTrue(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])
        self.mocks[1].assert_called()     # the sync blocks the other channels by tag
        F.set_kids(self.db, self.ann, None)
        self.assertEqual(before, self.jf.users[ANN]["Policy"])
        self.assertIsNone(self.ann.kids_level)
        self.assertIsNone(self.ann.kids_saved_policy)

    def test_changing_the_level_keeps_the_first_saved_policy(self):
        F.set_kids(self.db, self.ann, "young")
        F.set_kids(self.db, self.ann, "teens")
        self.assertEqual(14, self.jf.users[ANN]["Policy"]["MaxParentalRating"])
        F.set_kids(self.db, self.ann, None)
        self.assertIsNone(self.jf.users[ANN]["Policy"]["MaxParentalRating"])
        self.assertEqual(["Book"], self.jf.users[ANN]["Policy"]["BlockUnratedItems"])

    def test_a_kid_always_asks(self):
        F.set_kids(self.db, self.ben, "kids")
        self.assertTrue(approvals.needs_approval(self.ben))
        F.set_kids(self.db, self.ben, None)
        self.assertEqual("direct", self.ben.request_mode)

    def test_kids_channels_follow_for_every_kid(self):
        F.set_kids(self.db, self.ann, "kids")
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])
        F.set_kids_channels(self.db, ["c4", "c2", "nope", "c4"])
        self.assertEqual(["c4", "c2"], F.kids_channels(self.db))
        self.assertIn(L.ALL_TAG, self.jf.users[ANN]["Policy"]["BlockedTags"])
        self.assertTrue(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])
        F.set_kids_channels(self.db, [])
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])

    def test_an_admin_cant_be_a_kid(self):
        with self.assertRaises(F.FamilyError):
            F.set_kids(self.db, self.admin, "kids")
        self.assertIsNone(self.jf.users[ADMIN]["Policy"]["MaxParentalRating"])

    def test_my_channels_shows_a_kid_only_kids_channels(self):
        from routers import my_livetv
        from routers.auth import get_user_from_request
        F.set_kids_channels(self.db, ["c2", "c4"])
        F.set_kids(self.db, self.ann, "kids")
        app = FastAPI()
        app.include_router(my_livetv.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        with mock.patch("routers.my_livetv._favourites", return_value=set()):
            c = TestClient(app)
            d = c.get("/api/my/channels").json()
            self.assertEqual(["Cartoons", "Toons 2"], [x["name"] for x in d["channels"]])
            self.assertEqual({"chosen_by": "Lucas"}, d["kids"])


class KidsInTentaclesOwnPages(Case):
    def _client(self, user, router):
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        return TestClient(app)

    def test_library_shows_a_kid_only_what_jellyfin_lets_them_open(self):
        from routers import library
        for t, title in ((1, "Little Bear"), (2, "Night Terror")):
            self.db.add(mdb.Movie(tmdb_id=t, title=title, source="radarr"))
        self.db.commit()
        self.ann.kids_level = "young"
        self.db.commit()
        with mock.patch("services.family.visible_titles", return_value={("movie", 1)}):
            c = self._client(self.ann, library.router)
            self.assertEqual(["Little Bear"], [i["title"] for i in c.get("/api/library/items").json()["items"]])
            self.assertEqual(404, c.get("/api/library/item/movie/2").status_code)
        c = self._client(self.ben, library.router)   # not a kid: everything
        self.assertEqual(2, c.get("/api/library/items").json()["total"])

    def test_visible_titles_come_from_jellyfin_as_the_kid(self):
        self.jf.get = mock.Mock(return_value={"Items": [
            {"Type": "Movie", "ProviderIds": {"Tmdb": "1"}}, {"Type": "Series", "ProviderIds": {"Tmdb": "9"}},
            {"Type": "Movie", "ProviderIds": {}}]})
        F.forget_visible(ANN)
        self.assertEqual({("movie", 1), ("series", 9)}, F.visible_titles(self.db, self.ann))
        self.assertEqual("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", self.jf.get.call_args.kwargs["userId"])
        self.jf.get.side_effect = requests.ConnectionError()
        F.forget_visible(ANN)
        self.assertEqual(set(), F.visible_titles(self.db, self.ann))   # nothing rather than everything

    def test_no_discover_on_a_kids_profile(self):
        from routers import discover
        from fastapi import HTTPException
        self.ann.kids_level = "kids"
        with mock.patch("routers.discover.get_user_from_request", return_value=self.ann):
            with self.assertRaises(HTTPException) as e:
                discover._not_on_kids_profiles(mock.Mock(), self.db)
        self.assertEqual(403, e.exception.status_code)
        with mock.patch("routers.discover.get_user_from_request", return_value=self.ben):
            self.assertIsNone(discover._not_on_kids_profiles(mock.Mock(), self.db))

    def test_a_kids_navbar_has_no_discover_and_gets_it_back(self):
        cfg = {"toolbar": [{"id": "search", "enabled": True}, {"id": "discover", "enabled": True},
                           {"id": "activity", "enabled": False}]}
        written = []
        with mock.patch("services.smartlists.get_home_config", side_effect=lambda db, user_id: cfg), \
                mock.patch("services.smartlists._user_home_config_path", return_value="p"), \
                mock.patch("services.smartlists.write_home_json", side_effect=lambda p, c: written.append(json.loads(json.dumps(c)))), \
                mock.patch("services.smartlists.bump_playlist_version"), \
                mock.patch("services.smartlists._notify_jellyfin_plugin"):
            F.set_kids(self.db, self.ann, "kids")
            self.assertEqual([True, False, False], [t["enabled"] for t in written[-1]["toolbar"]])
            F.set_kids(self.db, self.ann, None)
            self.assertEqual([True, True, False], [t["enabled"] for t in written[-1]["toolbar"]])


# ── Invites, passwords, TV codes, defaults ──────────────────────────────────

class Invites(Case):
    def test_invite_makes_the_account_and_a_card(self):
        with mock.patch("services.family.start_new_person") as start:
            out = F.invite(self.db, self.admin, "  Grandma Rose ")
        pw = self.jf.passwords["d" * 32]
        self.assertRegex(pw, r"^grandmarose\d{5}!$")
        card = dict(out["invite"])
        self.assertIn(pw, card.pop("message"))
        self.assertEqual({"name": "Grandma Rose", "password": pw, "jellyfin_address": "http://jf.lan:8096",
                          "tentacle_address": "http://tentacle.lan:8888",
                          "tv_app_address": "http://tentacle.lan:8888/tv"}, card)
        row = self.db.query(mdb.TentacleUser).filter(mdb.TentacleUser.jellyfin_user_id == "d" * 32).one()
        self.assertEqual(("Grandma Rose", False, "ask", "Lucas"),
                         (row.display_name, row.is_admin, row.request_mode, row.invited_by))
        start.assert_called_once_with(row.id, None, None)

    def test_invite_a_kid(self):
        with mock.patch("services.family.start_new_person"):
            F.invite(self.db, self.admin, "Mia", kids_level="young")
        self.assertEqual(7, self.jf.users["d" * 32]["Policy"]["MaxParentalRating"])

    def test_a_name_already_taken_is_refused(self):
        with self.assertRaises(F.FamilyError) as e:
            F.invite(self.db, self.admin, "ann")
        self.assertEqual(409, e.exception.status)
        self.assertNotIn("/Users/New", [c[1] for c in self.jf.calls])

    def test_names_jellyfin_wont_take_are_refused_first(self):
        for bad in ("", "a" * 65, "<b>x</b>", "x/y"):
            with self.assertRaises(F.FamilyError):
                F.invite(self.db, self.admin, bad)

    def test_new_password(self):
        card = F.new_password(self.db, self.ann)
        self.assertEqual(self.jf.passwords[ANN], card["password"])
        with self.assertRaises(F.FamilyError):
            F.new_password(self.db, self.admin)

    def test_passwords_differ(self):
        self.assertGreater(len({F.make_password("Cam") for _ in range(50)}), 40)

    def test_tv_code_signs_the_tv_in_as_that_person(self):
        self.assertEqual({"ok": True, "name": "Ann"}, F.tv_sign_in(self.db, self.ann, " 123 456 "))
        self.assertEqual([("123456", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")], self.jf.authorized)

    def test_a_wrong_code_says_so_plainly(self):
        with self.assertRaises(F.FamilyError) as e:
            F.tv_sign_in(self.db, self.ann, "654321")
        self.assertIn("No TV is showing that code", e.exception.message)
        with self.assertRaises(F.FamilyError):
            F.tv_sign_in(self.db, self.ann, "12; drop")

    def test_codes_switched_off(self):
        self.jf.quick_connect = False
        self.assertFalse(F.quick_connect_enabled(self.db))
        with self.assertRaises(F.FamilyError) as e:
            F.tv_sign_in(self.db, self.ann, "123456")
        self.assertEqual(409, e.exception.status)
        F.enable_quick_connect(self.db)
        self.assertTrue(self.jf.quick_connect)

    def test_defaults_copy_someones_favourites(self):
        new = mdb.TentacleUser(jellyfin_user_id="e" * 32, display_name="New")
        self.db.add(new)
        self.db.commit()
        favs = {ANN: {"i3", "i1"}, "e" * 32: {"i1"}, BEN: set()}
        with mock.patch("services.livetv_tuners.favourite_ids", lambda jf, u: set(favs[u])), \
                mock.patch("services.livetv_tuners.set_favourite",
                           lambda jf, u, i, on: favs[u].add(i)):
            self.assertEqual(1, F.copy_channels(self.db, self.ann, new))
            self.assertEqual({"i3", "i1"}, favs["e" * 32])
            self.assertEqual(0, F.copy_channels(self.db, self.ben, new))   # Ben has none

    def test_starter_favourites(self):
        self.assertIsNone(F.starter_favourites(self.db))          # nobody chosen: no favourites
        F.save_defaults(self.db, None, self.ben.id)
        self.assertEqual(self.ben.id, F.starter_favourites(self.db).id)

    def test_defaults_are_checked(self):
        self.assertEqual({"home_from": None, "channels_from": None}, F.defaults(self.db))
        F.save_defaults(self.db, self.ann.id, None)
        self.assertEqual(self.ann.id, F.defaults(self.db)["home_from"])
        with self.assertRaises(F.FamilyError):
            F.save_defaults(self.db, 999, None)


class CopyHome(Case):
    def test_rows_follow_by_name_and_personal_ones_are_left_out(self):
        new = mdb.TentacleUser(jellyfin_user_id="e" * 32, display_name="New")
        self.db.add(new)
        self.db.add(mdb.AutoPlaylistToggle(user_id=self.ann.id, key="source:Netflix:movies", enabled=True))
        self.db.commit()
        template = {"rows": [
            {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1},
            {"type": "playlist", "playlist_id": "ann-netflix", "display_name": "Netflix Movies", "order": 2},
            {"type": "playlist", "playlist_id": "ann-dl", "display_name": "Ann's Downloads", "order": 3},
        ], "hero": {"enabled": True, "playlist_id": "ann-netflix", "display_name": "Netflix Movies"},
            "toolbar": [{"id": "search", "enabled": True}]}
        written = {}
        with mock.patch("services.smartlists.sync_smartlists"), \
                mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                mock.patch("services.smartlists.write_home_config"), \
                mock.patch("services.smartlists._notify_jellyfin_plugin"), \
                mock.patch("services.smartlists.bump_playlist_version"), \
                mock.patch("services.smartlists._user_home_config_path", return_value="p"), \
                mock.patch("services.smartlists.get_home_config", return_value=template), \
                mock.patch("services.smartlists._get_smartlists_with_playlist_ids",
                           return_value=[{"name": "Netflix Movies", "playlist_id": "new-netflix"},
                                         {"name": "New's Downloads", "playlist_id": "new-dl"}]), \
                mock.patch("services.smartlists.write_home_json", side_effect=lambda p, c: written.update(c)):
            self.assertEqual(2, F.copy_home(self.db, self.ann, new))
        self.assertEqual([("builtin", None, 1), ("playlist", "new-netflix", 2)],
                         [(r["type"], r.get("playlist_id"), r["order"]) for r in written["rows"]])
        self.assertEqual("new-netflix", written["hero"]["playlist_id"])
        self.assertEqual([{"id": "search", "enabled": True}], written["toolbar"])
        self.assertEqual(["source:Netflix:movies"], [t.key for t in self.db.query(mdb.AutoPlaylistToggle)
                                                     .filter(mdb.AutoPlaylistToggle.user_id == new.id)])


# ── Requests ────────────────────────────────────────────────────────────────

class Requests(Case):
    def setUp(self):
        super().setUp()
        p = mock.patch("services.approvals._details", return_value={"title": "Dune", "year": "2021",
                                                                    "poster_path": "/d.jpg"})
        p.start()
        self.addCleanup(p.stop)

    def _notes(self, user):
        return [(n.kind, n.title, n.message) for n in
                self.db.query(mdb.Notification).filter(mdb.Notification.user_id == user.id)]

    def test_who_asks(self):
        self.assertFalse(approvals.needs_approval(self.admin))
        self.assertTrue(approvals.needs_approval(self.ann))      # the default
        self.assertFalse(approvals.needs_approval(self.ben))     # set to straight through
        self.assertEqual({"mode": "ask", "approver": "Lucas"}, approvals.request_mode(self.db, self.ann))

    def test_hold_keeps_it_and_tells_the_admins(self):
        self.admin.notifications_enabled = False     # their download messages off: requests still reach them
        self.db.commit()
        r = approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [438631]}, tmdb_ids=[438631])
        self.assertEqual({"added": 0, "already_exists": 0, "failed": 0, "pending": 1,
                          "detail": "Sent to Lucas to approve. You'll see it in Activity."}, r)
        row = self.db.query(mdb.MediaRequest).one()
        self.assertEqual(("pending", "Dune", 438631), (row.status, row.title, row.tmdb_id))
        self.assertEqual([("request", "Dune", "Ann asked for this. Approve or decline it in Tentacle → Requests.")],
                         self._notes(self.admin))
        self.push.assert_called_once()
        self.assertEqual("Ann asked for Dune (2021)", self.push.call_args[0][1])
        # Asking again changes nothing.
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [438631]}, tmdb_ids=[438631])
        self.assertEqual(1, self.db.query(mdb.MediaRequest).count())

    def test_approve_runs_it_as_the_person(self):
        approvals.hold(self.db, self.ann, "series", {"tmdb_ids": [1399], "monitor": "pilot"}, tmdb_ids=[1399])
        row = self.db.query(mdb.MediaRequest).one()
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1, "already_exists": 0, "failed": 0}
        with mock.patch("services.media_requests.request_series", return_value=outcome) as rs:
            out = approvals.approve(self.db, row.id, self.admin)
        self.assertTrue(out["ok"])
        kw = rs.call_args.kwargs
        self.assertEqual((self.ann.id, [1399], "pilot", "approved by Lucas"),
                         (kw["user_id"], kw["tmdb_ids"], kw["monitor"], kw["via"]))
        self.assertEqual("approved", row.status)
        self.assertIn(("request", "Dune", "Lucas said yes. It's downloading now; you'll get a message when it's "
                                          "ready to watch."), self._notes(self.ann))
        with self.assertRaises(approvals.NotPending):
            approvals.approve(self.db, row.id, self.admin)

    def _approve_movie(self, coming_out, payload=None, kind="movie"):
        approvals.hold(self.db, self.ann, kind, payload or {"tmdb_ids": [1003596]}, tmdb_ids=[1003596])
        row = self.db.query(mdb.MediaRequest).one()
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1, "already_exists": 0, "failed": 0}
        fn = "request_movies" if kind == "movie" else "request_series"
        with mock.patch(f"services.media_requests.{fn}", return_value=outcome), \
                mock.patch("services.media_requests.coming_out", return_value=coming_out) as co:
            self.assertTrue(approvals.approve(self.db, row.id, self.admin)["ok"])
        return [n[2] for n in self._notes(self.ann) if "said yes" in n[2]], co

    def test_the_yes_for_a_title_not_out_yet_says_when_it_downloads(self):
        # Sony check 23 Oct 2026: "It's downloading now" for films out in 2026-12 and 2027-12
        said, co = self._approve_movie("2026-12-18")
        self.assertEqual(["Lucas said yes. It'll download when it comes out (18 December 2026); "
                          "you'll get a message when it's ready to watch."], said)
        co.assert_called_once_with(self.db, "movie", 1003596)

    def test_the_yes_without_a_release_date(self):
        said, _ = self._approve_movie("")
        self.assertEqual(["Lucas said yes. It'll download when it comes out; "
                          "you'll get a message when it's ready to watch."], said)

    def test_the_yes_for_a_released_title_says_downloading_now(self):
        said, _ = self._approve_movie(None)
        self.assertEqual(["Lucas said yes. It's downloading now; you'll get a message when it's ready to watch."], said)

    def test_the_yes_for_future_episodes_only(self):
        said, co = self._approve_movie(None, {"tmdb_ids": [1003596], "monitor": "future"}, kind="series")
        self.assertEqual(["Lucas said yes. New episodes will download as they come out; "
                          "you'll get a message when one's ready to watch."], said)
        co.assert_not_called()

    def test_whether_a_title_is_out_yet(self):
        from services.media_requests import movie_coming_out, series_coming_out
        now = datetime(2026, 10, 23)
        # The earliest future date, as Activity's Upcoming Releases shows it
        self.assertEqual("2026-12-18", movie_coming_out(
            {"inCinemas": "2026-12-18T00:00:00Z", "digitalRelease": "2027-03-01T00:00:00Z"}, now))
        self.assertEqual("2027-03-01", movie_coming_out(
            {"inCinemas": "2026-09-01T00:00:00Z", "digitalRelease": "2027-03-01T00:00:00Z"}, now))
        self.assertIsNone(movie_coming_out({"inCinemas": "2021-10-22T00:00:00Z", "isAvailable": True}, now))
        self.assertEqual("", movie_coming_out({"isAvailable": False, "status": "announced"}, now))
        self.assertIsNone(movie_coming_out({"hasFile": True, "digitalRelease": "2027-01-01T00:00:00Z"}, now))
        self.assertEqual("2027-01-10", series_coming_out({"firstAired": "2027-01-10T00:00:00Z"}, now))
        self.assertEqual("", series_coming_out({"status": "upcoming"}, now))
        self.assertIsNone(series_coming_out({"firstAired": "2011-04-17T00:00:00Z",
                                             "nextAiring": "2026-11-01T00:00:00Z"}, now))
        self.assertIsNone(series_coming_out({"statistics": {"episodeFileCount": 3}, "status": "upcoming"}, now))

    def test_out_yet_when_radarr_cant_be_read(self):
        from services.media_requests import coming_out
        self.assertIsNone(coming_out(self.db, "movie", 1003596))   # no Radarr configured

    def test_an_approval_the_arr_refuses_stays_for_another_try(self):
        from services.media_requests import RequestRefused
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        row = self.db.query(mdb.MediaRequest).one()
        with mock.patch("services.media_requests.request_movies", side_effect=RequestRefused("Radarr not configured")):
            out = approvals.approve(self.db, row.id, self.admin)
        self.assertEqual(("failed", "Radarr not configured"), (row.status, row.note))
        self.assertFalse(out["ok"])
        self.assertEqual([], [n for n in self._notes(self.ann) if "said" in n[2]])
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1}
        with mock.patch("services.media_requests.request_movies", return_value=outcome):
            self.assertTrue(approvals.approve(self.db, row.id, self.admin)["ok"])

    def test_decline_with_a_reason(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        row = self.db.query(mdb.MediaRequest).one()
        approvals.decline(self.db, row.id, self.admin, "Too scary")
        self.assertEqual(("declined", "Too scary"), (row.status, row.note))
        self.assertIn(("request", "Dune", 'Lucas said no this time. "Too scary"'), self._notes(self.ann))
        self.assertEqual({"id": row.id, "status": "declined", "note": "Too scary", "decided_by": "Lucas"},
                         approvals.status_for(self.db, self.ann, "movie", 1))
        self.assertIsNone(approvals.status_for(self.db, self.ben, "movie", 1))

    def test_notifications_say_what_kind_they_are(self):
        # The TV app and the Jellyfin toast head a decline "Request", not "Ready to Watch".
        from routers import notifications
        from routers.auth import get_user_from_request
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        approvals.decline(self.db, self.db.query(mdb.MediaRequest).one().id, self.admin)
        mdb.create_notification(self.db, self.ann.id, 2, "movie", "Up", "Up is ready to watch")
        self.db.add(mdb.Notification(user_id=self.ann.id, tmdb_id=3, title="Old", message="m", kind=None))
        self.db.commit()
        app = FastAPI()
        app.include_router(notifications.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        got = TestClient(app).get("/api/notifications").json()["notifications"]
        self.assertEqual({("Dune", "request"), ("Up", "download"), ("Old", "download")},
                         {(n["title"], n["kind"]) for n in got})

    def test_take_back(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        row = self.db.query(mdb.MediaRequest).one()
        self.assertFalse(approvals.cancel(self.db, row.id, self.ben))
        self.assertTrue(approvals.cancel(self.db, row.id, self.ann))
        self.assertEqual(0, self.db.query(mdb.MediaRequest).count())

    def test_listing(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1, 2]}, tmdb_ids=[1, 2])
        approvals.decline(self.db, self.db.query(mdb.MediaRequest).first().id, self.admin)
        everyone = approvals.listing(self.db)
        self.assertEqual((1, 1), (len(everyone["pending"]), len(everyone["decided"])))
        self.assertEqual("Ann", everyone["pending"][0]["requested_by"])
        self.assertEqual(0, len(approvals.listing(self.db, user=self.ben)["pending"]))
        self.assertEqual(1, approvals.pending_count(self.db))

    def _client(self, user):
        from routers import family, lists
        from routers.auth import get_user_from_request, require_admin
        app = FastAPI()
        app.include_router(lists.router)
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        if user.is_admin:
            app.dependency_overrides[require_admin] = lambda: user
        return TestClient(app)

    def test_a_family_request_never_reaches_radarr(self):
        with mock.patch("services.media_requests.request_movies") as rm:
            r = self._client(self.ann).post("/api/lists/add-to-radarr", json={"tmdb_ids": [438631]})
        self.assertEqual(200, r.status_code)
        self.assertEqual(1, r.json()["pending"])
        rm.assert_not_called()

    def test_an_admin_request_goes_straight_through(self):
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1, "already_exists": 0, "failed": 0}
        with mock.patch("services.media_requests.request_movies", return_value=outcome) as rm:
            r = self._client(self.admin).post("/api/lists/add-to-radarr", json={"tmdb_ids": [438631]})
        self.assertEqual(1, r.json()["added"])
        rm.assert_called_once()
        self.assertEqual(0, self.db.query(mdb.MediaRequest).count())

    def test_episodes_wait_too(self):
        body = {"tvdb_ids": [81189], "selected_episodes": [{"season": 1, "episode": 1}]}
        with mock.patch("services.media_requests.request_series") as rs:
            r = self._client(self.ann).post("/api/lists/add-to-sonarr", json=body)
        rs.assert_not_called()
        self.assertEqual(1, r.json()["pending"])
        row = self.db.query(mdb.MediaRequest).one()
        self.assertEqual((81189, None), (row.tvdb_id, row.tmdb_id))
        self.assertEqual([{"season": 1, "episode": 1}], json.loads(row.payload)["selected_episodes"])

    def test_admin_routes(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        rid = self.db.query(mdb.MediaRequest).one().id
        c = self._client(self.admin)
        self.assertEqual(1, c.get("/api/requests/count").json()["pending"])
        self.assertEqual(200, c.post(f"/api/requests/{rid}/decline", json={"note": ""}).status_code)
        self.assertEqual(409, c.post(f"/api/requests/{rid}/approve").status_code)
        mine = self._client(self.ann).get("/api/requests/mine").json()
        self.assertEqual("declined", mine["decided"][0]["status"])


# ── Problems ────────────────────────────────────────────────────────────────

class Alerts(Case):
    def _problem_notes(self):
        return [(n.user_id, n.title) for n in self.db.query(mdb.Notification).filter(mdb.Notification.kind == "problem")]

    def test_told_once_and_once_more_when_over(self):
        self.assertTrue(admin_alerts.raise_alert(self.db, "disk:/media", "downloads", "A disk is almost full", "5 GB free"))
        self.assertFalse(admin_alerts.raise_alert(self.db, "disk:/media", "downloads", "A disk is almost full", "4 GB free"))
        self.assertEqual([(self.admin.id, "A disk is almost full")], self._problem_notes())
        self.assertTrue(admin_alerts.clear_alert(self.db, "disk:/media"))
        self.assertFalse(admin_alerts.clear_alert(self.db, "disk:/media"))
        self.assertEqual([(self.admin.id, "A disk is almost full"), (self.admin.id, "Fixed: A disk is almost full")],
                         self._problem_notes())
        self.assertEqual(2, self.push.call_count)

    def test_family_get_plain_words(self):
        admin_alerts.raise_alert(self.db, "provider:1", "livetv", "P is failing",
                                 "Login refused by http://192.0.2.10 (HTTP 403)", push=False)
        admin_alerts.raise_alert(self.db, "arr:radarr", "requests", "Radarr can't be reached", "…")
        msgs = admin_alerts.family_messages(self.db)
        self.assertEqual([{"area": "livetv", "text": "Live TV is unavailable right now. Lucas knows."},
                          {"area": "requests", "text": "New requests will wait a little: downloads are paused "
                                                       "for now. Lucas knows."}], msgs)
        for m in msgs:
            self.assertNotRegex(m["text"], r"(?i)radarr|sonarr|http|403|provider")
        self.assertEqual([], [n for n in self.db.query(mdb.Notification)
                              .filter(mdb.Notification.user_id.in_([self.ann.id, self.ben.id]))])

    def test_check_raises_and_clears(self):
        found = {"x": {"downloads:stuck": ("downloads", "2 downloads are stuck", "Open Health", True)}}

        def seen_downloads(db):
            return found["x"]
        seen_downloads.__name__ = "_seen_downloads"

        def broken(db):
            raise RuntimeError("down")
        broken.__name__ = "_seen_arr"
        with mock.patch.object(admin_alerts, "CHECKS", (seen_downloads, broken)):
            admin_alerts.raise_alert(self.db, "arr:sonarr", "requests", "Sonarr can't be reached", "")
            self.assertEqual(1, admin_alerts.check(self.db)["raised"])
            found["x"] = {}
            out = admin_alerts.check(self.db)
        self.assertEqual(1, out["cleared"])
        # A check that failed leaves its own alerts alone.
        self.assertEqual(["arr:sonarr"], [a.key for a in admin_alerts.active(self.db)])

    def test_status_routes_for_everyone(self):
        from routers import family
        from routers.auth import get_user_from_request
        admin_alerts.raise_alert(self.db, "jellyfin:down", "server", "Jellyfin can't be reached", "")
        app = FastAPI()
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        c = TestClient(app)
        self.assertEqual("server", c.get("/api/family/status").json()["messages"][0]["area"])
        me = c.get("/api/family/me").json()
        self.assertEqual(("ask", "Lucas", None), (me["mode"], me["approver"], me["kids"]))
        self.assertEqual("http://tentacle.lan:8888/tv", me["tv_app_address"])


# ── The TV app ──────────────────────────────────────────────────────────────

class TvApp(unittest.TestCase):
    def setUp(self):
        tv_app._cache.update(at=0.0, value=None)
        self.addCleanup(tv_app._cache.update, at=0.0, value=None)

    def test_versions_in_order(self):
        k = tv_app.version_key
        self.assertTrue(k("1.21.0") > k("1.21.0-rc.2") > k("1.21.0-rc.1") > k("1.20.2"))
        self.assertEqual(k("v1.21.0"), k("1.21.0"))
        self.assertTrue(k("1.10.0") > k("1.9.9"))

    def test_the_release_apk_never_a_debug_one(self):
        assets = [{"name": "tentacle-androidtv-v1.21.0-github-debug.apk"},
                  {"name": "notes.txt"},
                  {"name": "tentacle-androidtv-v1.21.0-github-release.apk"}]
        self.assertEqual("tentacle-androidtv-v1.21.0-github-release.apk", tv_app.pick_asset(assets)["name"])
        self.assertIsNone(tv_app.pick_asset(assets[:2]))

    def test_latest_is_cached_and_survives_github_down(self):
        r = mock.Mock(status_code=200)
        r.json.return_value = {"tag_name": "v1.21.0", "html_url": "https://github.com/x/releases/tag/v1.21.0",
                               "assets": [{"name": "tentacle-androidtv-v1.21.0-github-release.apk",
                                           "browser_download_url": "https://github.com/x/a.apk"}]}
        fetch = mock.Mock(return_value=r)
        self.assertEqual("1.21.0", tv_app.latest(fetch)["version"])
        tv_app.latest(fetch)
        self.assertEqual(1, fetch.call_count)
        tv_app._cache.update(at=0.0, value=None)
        self.assertIsNone(tv_app.latest(mock.Mock(side_effect=requests.ConnectionError())))

    def test_who_runs_which_version(self):
        devices = [{"AppName": "Tentacle Android TV", "AppVersion": "1.20.2", "LastUserId": "B" * 32},
                   {"AppName": "Tentacle Android TV", "AppVersion": "1.21.0", "LastUserId": "b" * 32},
                   {"AppName": "Tentacle Android TV (debug)", "AppVersion": "9.9.9", "LastUserId": "b" * 32},
                   {"AppName": "Jellyfin Web", "AppVersion": "10.11.8", "LastUserId": "c" * 32}]
        self.assertEqual({"b" * 32: "1.21.0"}, tv_app.versions_by_user(devices))

    def test_tv_redirects_to_the_newest_apk(self):
        from routers import family
        app = FastAPI()
        app.include_router(family.tv_router)
        c = TestClient(app)
        with mock.patch("services.tv_app.latest", return_value={"version": "1.21.0", "url": "https://github.com/x/a.apk"}):
            r = c.get("/tv", follow_redirects=False)
        self.assertEqual((302, "https://github.com/x/a.apk"), (r.status_code, r.headers["location"]))
        with mock.patch("services.tv_app.latest", return_value=None):
            r = c.get("/tv", follow_redirects=False)
        self.assertEqual(tv_app.RELEASES_PAGE, r.headers["location"])


class PeopleRoute(Case):
    def test_people_lists_everyone_and_makes_missing_rows(self):
        from routers import family
        from routers.auth import require_admin
        self.jf.users["e" * 32] = {"Id": "e" * 32, "Name": "Zed", "Policy": {"IsAdministrator": False}}
        app = FastAPI()
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: self.admin
        with mock.patch("services.family.start_new_person") as start, \
                mock.patch("services.tv_app.latest", return_value={"version": "1.21.0", "url": ""}):
            d = TestClient(app).get("/api/family/people").json()
        self.assertEqual(["Ann", "Ben", "Lucas", "Zed"], [p["name"] for p in d["people"]])
        start.assert_called_once()
        lucas = next(p for p in d["people"] if p["name"] == "Lucas")
        self.assertEqual((True, True, "direct"), (lucas["is_owner"], lucas["is_admin"], lucas["request_mode"]))
        self.assertEqual("1.21.0", d["tv_app"]["newest"])
        self.assertTrue(d["quick_connect"])


if __name__ == "__main__":
    unittest.main()
