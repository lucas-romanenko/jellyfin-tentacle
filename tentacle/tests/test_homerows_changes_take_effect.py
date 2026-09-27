"""Home-row changes made in the dashboard must reach the clients, and reach them right.

Found by driving a 755ea67 stack (Jellyfin 10.11.8 + plugin 2.266.0) from a headless browser:

1. add-row stored a playlist row with no sort_by / sort_order / shape. The plugin reads an unset sort as
   "keep the stored playlist order" (#59), so a release-date row showed a newly appended title LAST (19th of 20)
   until the next write_home_config(), and a YouTube row drew poster cards.
2. Switching a SmartList to Random never shuffled it: the refresh keeps the stored order when the set is unchanged,
   and the plugin shows a Random row in its stored order.
3. A new user who has playlists (every user gets the YouTube channel playlists) but no home config got a config
   with NO rows, and the starter built-in rows were then never seeded.
4. Plugin: the row/hero DTOs carried no Path, so the card-previews "local_only" policy treated every card as not
   local; HeroConfig did not say the hero's sort, so a sort change could not be detected by clients.
5. Web home (tentacle-home.js / tentacle-mediabar.js): a merge-Continue-Watching toggle and a row title change were
   not part of the live-update key; a hero change showed only after a full page reload; a toolbar change made while
   away from Home was never applied; after a user switch in the same tab the navbar kept the previous user's toolbar.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.smartlists as ssl

try:
    import routers.smartlists as rsl
except Exception:  # pragma: no cover - depends on optional deps
    rsl = None

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")
HOME_JS = Path("../tentacle-plugin/Inject/tentacle-home.js")
MEDIABAR_JS = Path("../tentacle-plugin/Inject/tentacle-mediabar.js")


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db):
    user = mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1")
    db.add(user)
    db.commit()
    return user


SMARTLISTS = [
    {"name": "Netflix Movies", "playlist_id": "pl-net", "media_types": ["Movie"], "enabled": True,
     "sort_by": "releasedate", "sort_order": "Descending", "is_youtube": False},
    {"name": "QA Tube", "playlist_id": "pl-yt", "media_types": ["Movie"], "enabled": True,
     "sort_by": "releasedate", "sort_order": "Descending", "is_youtube": True},
    {"name": "Recently Added Movies", "playlist_id": "pl-ra", "media_types": ["Movie"], "enabled": True,
     "sort_by": "datecreated", "sort_order": "Descending", "is_youtube": False},
]


@unittest.skipIf(rsl is None, "fastapi not installed")
class TestAddRowCarriesSortAndShape(unittest.TestCase):
    def _add(self, pid):
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        user = _user(db)
        with mock.patch.object(rsl, "HOME_CONFIG_DIR", str(tmp / "home-configs")), \
             mock.patch.object(rsl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: SMARTLISTS), \
             mock.patch.object(rsl, "bump_playlist_version"), \
             mock.patch.object(rsl, "_notify_jellyfin_plugin", lambda db: {}):
            rsl._write_home_json(user, {"hero": {"enabled": False}, "rows": []})
            out = rsl.add_row(rsl.AddRowRequest(playlist_id=pid), db=db, user=user)
            self.assertTrue(out["success"], out)
            cfg = json.loads((tmp / "home-configs" / "jf-1.json").read_text())
        return next(r for r in cfg["rows"] if r["playlist_id"] == pid)

    def test_a_release_date_row_is_sorted_by_release_date(self):
        row = self._add("pl-net")
        self.assertEqual((row.get("sort_by"), row.get("sort_order")), ("releasedate", "Descending"),
                         "the row has no sort, so the plugin shows the stored (append) order")

    def test_a_recently_added_row_keeps_its_date_sort(self):
        self.assertEqual(self._add("pl-ra").get("sort_by"), "datecreated")

    def test_a_youtube_row_starts_wide_and_others_poster(self):
        self.assertEqual(self._add("pl-yt").get("shape"), "wide")
        self.assertEqual(self._add("pl-net").get("shape"), "poster")

    def test_the_row_matches_what_a_regeneration_would_write(self):
        """add-row and write_home_config() must agree, or the row changes under the user at the next sync."""
        row = self._add("pl-yt")
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        _user(db)
        path = tmp / "home.json"
        path.write_text(json.dumps({"hero": {"enabled": False}, "rows": [dict(row)], "toolbar": [{"id": "search", "enabled": True}]}))
        with mock.patch.object(ssl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: SMARTLISTS), \
             mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: path), \
             mock.patch.object(ssl, "bump_playlist_version"):
            cfg = ssl.write_home_config(db, user_id=1)
        regen = cfg["rows"][0]
        for k in ("sort_by", "sort_order", "shape", "max_items", "display_name"):
            self.assertEqual(regen.get(k), row.get(k), k)


class TestNewUserWithPlaylistsGetsStarterRows(unittest.TestCase):
    def test_first_write_seeds_the_starter_rows_before_merging(self):
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        _user(db)
        path = tmp / "home.json"
        starter = {"hero": {"enabled": False, "playlist_id": "", "display_name": ""},
                   "rows": [{"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1}],
                   "toolbar": [{"id": "search", "enabled": True}]}

        def fake_seed(db_, user):
            path.write_text(json.dumps(starter))
            return starter

        with mock.patch.object(ssl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: SMARTLISTS[1:2]), \
             mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: path), \
             mock.patch("routers.smartlists._seed_home_config_from_jellyfin", fake_seed), \
             mock.patch.object(ssl, "bump_playlist_version"):
            cfg = ssl.write_home_config(db, user_id=1)
        self.assertEqual([r.get("section_id") for r in cfg["rows"]], ["resumevideo"],
                         "a new user with only a YouTube playlist got a home config with no rows")

    def test_an_existing_config_is_never_reseeded(self):
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        _user(db)
        path = tmp / "home.json"
        path.write_text(json.dumps({"hero": {"enabled": False}, "rows": [], "toolbar": []}))
        seed = mock.Mock(return_value={})
        with mock.patch.object(ssl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: SMARTLISTS), \
             mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: path), \
             mock.patch("routers.smartlists._seed_home_config_from_jellyfin", seed), \
             mock.patch.object(ssl, "bump_playlist_version"):
            ssl.write_home_config(db, user_id=1)
        seed.assert_not_called()


class FakeJF:
    def __init__(self, current, query):
        self.current, self.query, self.cleared, self.added = list(current), list(query), False, None

    def query_items(self, **kw):
        return [{"Id": i, "Type": "Movie", "ProviderIds": {}} for i in self.query]

    def item_exists(self, pid):
        return True

    def get_playlist_items(self, pid):
        return [{"Id": i, "Type": "Movie", "PlaylistItemId": "e-" + i} for i in self.current]

    def remove_from_playlist(self, pid, entry_ids):
        self.cleared = True
        return True

    def add_to_playlist(self, pid, ids):
        self.added = list(ids)
        return True


RANDOM_CFG = {"Name": "Netflix Movies", "Type": "Playlist", "MediaTypes": ["Movie"],
              "ExpressionSets": [{"Expressions": [{"MemberName": "Tags", "Operator": "Contains", "TargetValue": "Netflix Movies"}]}],
              "Order": {"SortOptions": [{"SortBy": "Random", "SortOrder": "Descending"}]},
              "UserPlaylists": [{"UserId": "jf-1", "JellyfinPlaylistId": "pl-net"}]}


class TestRandomSortShuffles(unittest.TestCase):
    def _run(self, reorder):
        jf = FakeJF(["a", "b", "c", "d"], ["c", "a", "d", "b"])
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        ssl._process_single_playlist_locked(jf, Path("/nonexistent"), json.loads(json.dumps(RANDOM_CFG)), "jf-1", stats,
                                            reorder=reorder)
        return jf

    def test_a_routine_refresh_keeps_the_fixed_shuffle(self):
        jf = self._run(False)
        self.assertFalse(jf.cleared)
        self.assertIsNone(jf.added)

    def test_switching_to_random_stores_the_new_order(self):
        jf = self._run(True)
        self.assertTrue(jf.cleared)
        self.assertEqual(jf.added, ["c", "a", "d", "b"])

    def test_update_playlist_sort_asks_for_the_shuffle_only_for_random(self):
        tmp = Path(tempfile.mkdtemp())
        folder = tmp / "Netflix Movies"
        folder.mkdir()
        (folder / "config.json").write_text(json.dumps(RANDOM_CFG))
        for sort, want in (("random", ["Netflix Movies"]), ("name", None)):
            calls = []
            with mock.patch.object(ssl, "_user_smartlists_path", lambda db, uid: tmp), \
                 mock.patch.object(ssl, "_scan_existing", lambda p: {"Netflix Movies": (folder, json.loads((folder / "config.json").read_text()))}), \
                 mock.patch.object(ssl, "refresh_smartlist_playlists", lambda db, **kw: calls.append(kw) or {}), \
                 mock.patch.object(ssl, "write_home_config", lambda db, user_id=None: {}), \
                 mock.patch.object(ssl, "_notify_jellyfin_plugin", lambda db: {}), \
                 mock.patch.object(ssl, "bump_playlist_version"):
                out = ssl.update_playlist_sort("Netflix Movies", sort, "Descending", db=None, user_id=1)
            self.assertTrue(out["success"], out)
            self.assertEqual(calls[0].get("reorder_names"), want, sort)


class TestPluginPayload(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text()

    def _block(self, start):
        i = self.src.index(start)
        j = self.src.index("GetBaseItemDtos", i)
        return self.src[i:j]

    def test_row_and_hero_items_carry_their_path(self):
        for m in ("public async Task<ActionResult> GetSectionItems", "public async Task<ActionResult> GetHeroItems"):
            block = self._block(m)
            fields = re.search(r"Fields = new\[\]\s*\{(.*?)\}", block, re.S).group(1)
            self.assertIn("ItemFields.Path", fields, m)
            self.assertNotIn("ItemFields.MediaSources", fields, "MediaSources carries a .strm's provider URL")

    def test_hero_config_says_how_the_hero_is_sorted_and_filtered(self):
        i = self.src.index("public async Task<ActionResult> GetHeroConfig")
        block = self.src[i:self.src.index("/// <summary>", i)]
        for f in ("sortBy = hero.SortBy", "sortOrder = hero.SortOrder", "requireLogo = hero.RequireLogo",
                  "requireTrailer = hero.RequireTrailer", "playlistId = hero.PlaylistId", "itemCount = hero.ItemCount"):
            self.assertIn(f, block)


class TestWebHomeLiveUpdates(unittest.TestCase):
    def setUp(self):
        self.home = HOME_JS.read_text()
        self.bar = MEDIABAR_JS.read_text()

    def _fn(self, name):
        start = self.home.index("function %s(" % name)
        nxt = re.search(r"\n  function ", self.home[start + 1:])
        return self.home[start: start + 1 + nxt.start()] if nxt else self.home[start:]

    def test_the_structure_key_covers_the_title_and_the_merge_flag(self):
        body = self._fn("refreshPlaylistRows")
        key = body[body.index("var keyOf"): body.index("if (oldKeys !== newKeys)")]
        self.assertIn("displayText", key)
        self.assertIn("merge", key)
        self.assertIn("MH.activeMerge = mergeCW", body)
        self.assertIn("MH.activeMerge = mergeCW", self._fn("renderHomePage"))

    def test_a_version_change_refreshes_toolbar_and_hero(self):
        chrome = self._fn("refreshChrome")
        self.assertIn("refreshToolbar", chrome)
        self.assertIn("TentacleMediaBar.refreshIfChanged", chrome)
        poll = self._fn("startVersionPolling")
        self.assertGreaterEqual(poll.count("refreshChrome()"), 2,
                                "both the live poll and the seed after returning Home must refresh the chrome")
        seed = poll[: poll.index("setInterval")]
        self.assertRegex(seed.replace("\n", " "), r"MH\.lastVersion !== -1 && v !== MH\.lastVersion")

    def test_a_user_switch_refetches_the_toolbar(self):
        body = self._fn("onHomePage")
        switch = body[body.index("User switched"): body.index("if (document.getElementById('tentacle-home'))")]
        self.assertIn("refreshChrome()", switch)

    def test_the_media_bar_reloads_only_when_its_config_changed(self):
        i = self.bar.index("refreshIfChanged: function")
        block = self.bar[i: self.bar.index("loadContent: function", i)]
        self.assertIn("TentacleHome/HeroConfig", block)
        self.assertRegex(block, r"if \(key === self\._heroConfigKey\) return false")
        self.assertIn("self.loadContent()", block)
        # the key is remembered from the first HeroConfig answer (init) onwards
        self.assertIn("self._applyHeroConfig(cfg)", self.bar[: i])
        self.assertIn("this._heroConfigKey = JSON.stringify(cfg || null)", self.bar)


if __name__ == "__main__":
    unittest.main()
