"""▶ Play and Open in Jellyfin on every title that can be watched now
(services/watch_links.py, Oct 2026).

Run from the tentacle/ directory:  python -m unittest tests.test_watch_links

- Links use the public Jellyfin address; Tentacle's own address only for a
  browser at home; neither: no links.
- A title gets links only while Jellyfin has it and it is ready (not while a
  download is still being got ready), and only for people who may open it.
- Library, its details, Following, Activity's Recently downloaded and
  Discover (in-library cards only) carry them; cached lists are not changed.
- Episodes of a season, asked of Jellyfin as the person.
- Jellyfin Web (Inject/tentacle-play.js) reads the Play flag and drops it.
"""
import json
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import download_readiness, watch_links as W
from tmp_dirs import temp_dir

ADMIN, KID = "a" * 32, "b" * 32
JF_MOVIE, JF_SHOW, JF_STORED = "1" * 32, "2" * 32, "3" * 32
PUBLIC = "https://jellyfin.example.com"
SERVER = "f" * 32


def _request(host="203.0.113.9", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


class Case(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id=KID, display_name="Mia", is_admin=False, kids_level="young")
        self.db.add_all([self.admin, self.kid])
        self.db.add_all([
            mdb.Movie(tmdb_id=10, title="City Heist", source="provider_1"),
            mdb.Movie(tmdb_id=11, title="Fresh Download", source="radarr", jellyfin_item_id=JF_STORED),
            mdb.Movie(tmdb_id=12, title="Still Downloading", source="radarr"),
            mdb.Series(tmdb_id=20, title="Space Show", source="sonarr"),
        ])
        self.db.commit()
        for k, v in (("jellyfin_url", "http://jellyfin:8096"), ("jellyfin_api_key", "k"),
                     ("jellyfin_user_id", ADMIN), ("jellyfin_public_url", PUBLIC + "/")):
            mdb.set_setting(self.db, k, v)
        self.maps = {"movie": {10: JF_MOVIE}, "series": {20: JF_SHOW}}
        from routers import discover
        for p in (mock.patch.object(discover, "_search_jellyfin_ids", side_effect=lambda k, d: self.maps[k]),
                  mock.patch.object(discover, "_jf_server_id_cache", {"id": SERVER, "checked": True})):
            p.start()
            self.addCleanup(p.stop)
        download_readiness.reset()
        self.addCleanup(download_readiness.reset)

    def links(self, item):
        return W.urls(PUBLIC, item, SERVER)


class Addresses(Case):
    def test_the_public_address_is_used_from_anywhere(self):
        self.assertEqual(PUBLIC, W.browser_base(self.db, _request()))
        self.assertEqual(PUBLIC, W.browser_base(self.db, _request("192.168.1.20")))

    def test_without_one_tentacles_address_only_at_home(self):
        mdb.set_setting(self.db, "jellyfin_public_url", "")
        self.assertEqual("http://jellyfin:8096", W.browser_base(self.db, _request("192.168.1.20")))
        self.assertIsNone(W.browser_base(self.db, _request()))
        # Through a tunnel the connection is local but the browser is not.
        self.assertIsNone(W.browser_base(self.db, _request("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"})))
        self.assertIsNone(W.linker(self.db, _request(), self.admin).links("movie", 10))

    def test_play_opens_the_details_page_with_the_play_flag(self):
        links = W.linker(self.db, _request(), self.admin).links("movie", 10)
        self.assertEqual(f"{PUBLIC}/web/#/details?id={JF_MOVIE}&serverId={SERVER}", links["open"])
        self.assertEqual(links["open"] + "&tentaclePlay=1", links["play"])


class WhatPlays(Case):
    def test_a_title_jellyfin_has_plays(self):
        L = W.linker(self.db, _request(), self.admin)
        self.assertEqual(self.links(JF_MOVIE), L.links("movie", 10))
        self.assertEqual(self.links(JF_SHOW), L.links("series", 20))
        self.assertIsNone(L.links("series", 10), "movie 10 is not show 10")

    def test_a_download_jellyfin_just_added_plays_by_its_stored_id(self):
        self.assertEqual(self.links(JF_STORED), W.linker(self.db, _request(), self.admin).links("movie", 11))

    def test_a_title_not_in_jellyfin_or_still_being_got_ready_has_no_links(self):
        L = W.linker(self.db, _request(), self.admin)
        self.assertIsNone(L.links("movie", 12))
        download_readiness.begin("movie", 11, "Fresh Download")
        self.assertIsNone(W.linker(self.db, _request(), self.admin).links("movie", 11))

    def test_a_kid_gets_links_only_for_what_jellyfin_lets_them_open(self):
        with mock.patch("services.family.visible_titles", return_value={("movie", 10)}):
            L = W.linker(self.db, _request(), self.kid)
            self.assertIsNotNone(L.links("movie", 10))
            self.assertIsNone(L.links("series", 20))

    def test_nobody_signed_in_gets_nothing(self):
        self.assertIsNone(W.linker(self.db, _request(), None).links("movie", 10))

    def test_attach_copies_and_marks_only_in_library_cards(self):
        shared = [{"tmdb_id": 10, "media_type": "movie", "in_library": True},
                  {"tmdb_id": 20, "media_type": "series", "in_library": False},
                  {"media_type": "channel", "title": "News"}]
        out = W.linker(self.db, _request(), self.admin).attach(shared, only_in_library=True)
        self.assertEqual(self.links(JF_MOVIE), out[0]["jellyfin"])
        self.assertNotIn("jellyfin", out[1])
        self.assertNotIn("jellyfin", out[2])
        self.assertNotIn("jellyfin", shared[0], "a cached list must not carry one person's links")

    def test_attach_drops_links_a_cached_item_carried(self):
        out = W.linker(self.db, _request(), self.kid).attach(
            [{"tmdb_id": 20, "media_type": "series", "jellyfin": self.links(JF_SHOW)}])
        self.assertNotIn("jellyfin", out[0])


class Pages(Case):
    def client(self, user, router):
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        return TestClient(app)

    def test_library_cards_and_details(self):
        from routers import library
        c = self.client(self.admin, library.router)
        items = {i["tmdb_id"]: i for i in c.get("/api/library/items").json()["items"]}
        self.assertEqual(self.links(JF_MOVIE), items[10]["jellyfin"])
        self.assertEqual(self.links(JF_STORED), items[11]["jellyfin"])
        self.assertNotIn("jellyfin", items[12])
        self.assertEqual(self.links(JF_SHOW), c.get("/api/library/item/series/20").json()["jellyfin"])
        self.assertNotIn("jellyfin", c.get("/api/library/item/movie/12").json())

    def test_following(self):
        from routers import library
        show = self.db.query(mdb.Series).filter_by(tmdb_id=20).one()
        show.sonarr_monitored = True
        self.db.commit()
        rows = self.client(self.admin, library.router).get("/api/library/following").json()
        self.assertEqual(self.links(JF_SHOW), rows[0]["jellyfin"])

    def test_recently_downloaded_gets_play_once_ready(self):
        from datetime import datetime
        from routers import activity
        m = self.db.query(mdb.Movie).filter_by(tmdb_id=11).one()
        m.date_added = datetime.utcnow()
        self.db.commit()
        c = self.client(self.admin, activity.router)
        with mock.patch.object(activity, "_build_downloads", return_value=[]), \
                mock.patch.object(activity, "_get_wanted", return_value={"unreleased": [], "searching": []}), \
                mock.patch.object(activity, "_watch_arr_searches"), \
                mock.patch("services.arr_insight.searching_problems", return_value=[]), \
                mock.patch("services.arr_insight.coming_up", return_value=[]):
            token = download_readiness.begin("movie", 11, "Fresh Download")
            self.assertEqual([], c.get("/api/activity").json()["recently_downloaded"])
            download_readiness.end(token, True)
            recent = c.get("/api/activity").json()["recently_downloaded"]
        self.assertEqual(self.links(JF_STORED), recent[0]["jellyfin"])

    def test_discover_cards_in_the_library(self):
        from routers import discover
        sections = [{"items": [{"tmdb_id": 10, "media_type": "movie", "in_library": True},
                               {"tmdb_id": 99, "media_type": "movie", "in_library": False}]}]
        discover._watch_links(self.db, _request(), sections, self.admin)
        self.assertEqual(self.links(JF_MOVIE), sections[0]["items"][0]["jellyfin"])
        self.assertNotIn("jellyfin", sections[0]["items"][1])


class Episodes(Case):
    def test_each_episode_jellyfin_shows_the_person(self):
        from routers import library
        jf = mock.Mock()
        jf.get.return_value = {"Items": [
            {"Id": "e1" * 16, "IndexNumber": 1, "ParentIndexNumber": 2},
            {"Id": "e2" * 16, "IndexNumber": 2, "ParentIndexNumber": 2, "LocationType": "Virtual"},
            {"Id": "e3" * 16, "IndexNumber": 3, "ParentIndexNumber": 2},
        ]}
        c = Pages.client(self, self.kid, library.router)
        with mock.patch("services.family.visible_titles", return_value={("series", 20)}), \
                mock.patch("services.family.jellyfin", return_value=jf):
            eps = c.get("/api/library/watch/series/20/season/2").json()["episodes"]
        self.assertEqual({"1": self.links("e1" * 16), "3": self.links("e3" * 16)}, eps)
        path, params = jf.get.call_args[0][0], jf.get.call_args[1]
        self.assertEqual(f"/Shows/{JF_SHOW}/Episodes", path)
        self.assertEqual(2, params["season"])
        self.assertEqual(KID, params["userId"].replace("-", ""), "asked as the person, not the admin")

    def test_a_show_the_person_may_not_open_has_none(self):
        from routers import library
        jf = mock.Mock()
        c = Pages.client(self, self.kid, library.router)
        with mock.patch("services.family.visible_titles", return_value=set()), \
                mock.patch("services.family.jellyfin", return_value=jf):
            self.assertEqual({}, c.get("/api/library/watch/series/20/season/1").json()["episodes"])
        jf.get.assert_not_called()


class PlayScript(unittest.TestCase):
    """Inject/tentacle-play.js under node."""

    def setUp(self):
        from inject_js import HAVE_NODE
        if not HAVE_NODE:
            self.skipTest("node is not installed")

    def run_js(self, body):
        from inject_js import INJECT, node
        src = (INJECT / "tentacle-play.js").read_text(encoding="utf-8")
        return node("""
var store = {};
var window = { addEventListener: function () {} };
var document = { addEventListener: function () {} };
var sessionStorage = { getItem: function (k) { return store[k] || null; },
  setItem: function (k, v) { store[k] = v; }, removeItem: function (k) { delete store[k]; } };
var location = { hash: '#/home', pathname: '/web/', search: '' };
var history = { state: null, replaceState: function (s, t, u) { location.hash = u.slice(u.indexOf('#')); } };
""" + src + "\nvar T = window.TentaclePlay.internals;\n" + body)

    def test_reads_the_flag_and_takes_it_out(self):
        out = self.run_js("""
var id = '%s';
console.log(JSON.stringify({
  play: T.parse('#/details?id=' + id + '&serverId=s1&tentaclePlay=1'),
  dashed: T.parse('#/details?id=11111111-1111-1111-1111-111111111111&tentaclePlay=1'),
  plain: T.parse('#/details?id=' + id + '&serverId=s1'),
  other: T.parse('#/home?id=' + id + '&tentaclePlay=1'),
  bad: T.parse('#/details?id=../Users&tentaclePlay=1'),
  stripped: T.withoutFlag('#/details?id=' + id + '&serverId=s1&tentaclePlay=1'),
  first: T.withoutFlag('#/details?tentaclePlay=1&id=' + id)
}));""" % JF_MOVIE)
        self.assertEqual({"id": JF_MOVIE, "serverId": "s1"}, out["play"])
        self.assertEqual("1" * 32, out["dashed"]["id"])
        self.assertIsNone(out["plain"])
        self.assertIsNone(out["other"])
        self.assertIsNone(out["bad"])
        self.assertEqual(f"#/details?id={JF_MOVIE}&serverId=s1", out["stripped"])
        self.assertEqual(f"#/details?id={JF_MOVIE}", out["first"])

    def test_a_show_plays_the_episode_in_progress_then_next_up_then_the_first(self):
        out = self.run_js("""
var answers;
window.ApiClient = { getCurrentUserId: function () { return 'u'; },
  getUrl: function (p) { return p; },
  getJSON: function (p) {
    for (var k in answers) if (p.indexOf(k) === 0) return Promise.resolve(answers[k]);
    return Promise.reject(new Error(p));
  } };
var show = { Id: 'show', Type: 'Series', IsFolder: true };
var results = {};
answers = { 'Users/u/Items?': { Items: [{ Id: 'ep5', UserData: { PlaybackPositionTicks: 900 } }] } };
T.choose(show).then(function (r) {
  results.resume = r;
  answers = { 'Users/u/Items?': { Items: [] }, 'Shows/NextUp': { Items: [{ Id: 'ep6', UserData: {} }] } };
  return T.choose(show);
}).then(function (r) {
  results.next = r;
  answers = { 'Users/u/Items?': { Items: [] }, 'Shows/NextUp': { Items: [] },
    'Shows/show/Episodes': { Items: [{ Id: 'special', ParentIndexNumber: 0 }, { Id: 'ep1', ParentIndexNumber: 1 }] } };
  return T.choose(show);
}).then(function (r) {
  results.first = r;
  return T.choose({ Id: 'm', Type: 'Movie', UserData: { PlaybackPositionTicks: 42 } });
}).then(function (r) {
  results.movie = r;
  return T.choose({ Id: 'm', Type: 'Movie', UserData: { PlaybackPositionTicks: 42, Played: true } });
}).then(function (r) {
  results.watched = r;
  console.log(JSON.stringify(results));
});""")
        self.assertEqual(["ep5", 900], out["resume"])
        self.assertEqual(["ep6", 0], out["next"])
        self.assertEqual(["ep1", 0], out["first"])
        self.assertEqual(["m", 42], out["movie"])
        self.assertEqual(["m", 0], out["watched"])


class DashboardButtons(unittest.TestCase):
    """The links go into the markup as plain, escaped hrefs."""

    def setUp(self):
        from dashboard_js import HAVE_NODE
        if not HAVE_NODE:
            self.skipTest("node is not installed")

    def test_buttons_are_plain_links_and_refuse_anything_but_http(self):
        from dashboard_js import Page, functions, node
        src = functions("pages.js", ["_watchTarget", "_watchOk", "watchButtons", "watchCardButtons", "escapeAttr"])
        good = {"play": "https://jf.example/web/#/details?id=1&a=\"<x>&tentaclePlay=1", "open": "https://jf.example/web/#/details?id=1"}
        bad = {"play": "javascript:alert(1)", "open": "https://jf.example/web/"}
        out = node("const window = { matchMedia: () => ({ matches: false }) };\n"
                   "const _WATCH_ICON_PLAY = '', _WATCH_ICON_OPEN = '';\n" + src
                   + f"\nconsole.log(JSON.stringify([watchButtons({json.dumps(good)}), watchCardButtons({json.dumps(good)}, 'A \"b\"'),"
                     f" watchButtons({json.dumps(bad)}), watchCardButtons(null)]))")
        btns, card, refused, none = out
        links = [a for tag, a in Page(btns).elements if tag == "a"]
        self.assertEqual([good["play"], good["open"]], [a["href"] for a in links])
        self.assertTrue(all(a.get("target") == "_blank" for a in links), "a computer keeps Tentacle open")
        self.assertEqual(2, len([1 for tag, _ in Page(card).elements if tag == "a"]))
        self.assertEqual("", refused)
        self.assertEqual("", none)


if __name__ == "__main__":
    unittest.main()
