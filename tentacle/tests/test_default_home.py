"""The default home (Decisions 2026-10-21): everyone without their own layout
sees the admin's default (Tentacle's suggestion until an admin saves one),
never Jellyfin's stock sections; changing your own home makes it yours;
"Reset to default" gives it back; old untouched starter homes move over once.

Run from the tentacle/ directory:  python -m unittest tests.test_default_home
"""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.smartlists as rsl
import services.default_home as dh
import services.smartlists as sl
from tmp_dirs import temp_dir

LEGACY_STARTER = [("smalllibrarytiles", "My Media"), ("resumevideo", "Continue Watching"),
                  ("nextup", "Next Up"), ("latestmedia", "Recently Added Media")]


class Base(unittest.TestCase):
    """A household (admin Lucas, Ethan, Fahad, kid Mia) with a library of two
    IPTV sources and recent movies and shows. Jellyfin is faked at the
    playlist level: a person "has" a ready-made playlist once its switch is on
    and a sync ran, with the id "<person>-<key>"."""

    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([
            mdb.TentacleUser(id=1, jellyfin_user_id="jf-lucas", display_name="lucas", is_admin=True),
            mdb.TentacleUser(id=2, jellyfin_user_id="jf-ethan", display_name="ethan"),
            mdb.TentacleUser(id=3, jellyfin_user_id="jf-fahad", display_name="fahad"),
            mdb.TentacleUser(id=4, jellyfin_user_id="jf-mia", display_name="mia", kids_level="kids"),
            mdb.Provider(id=1, name="P", server_url="http://p", username="u", password="p"),
        ])
        now = datetime.utcnow()
        tmdb = iter(range(1, 1000))
        for tag, n in (("Netflix", 5), ("Hulu", 2)):
            for i in range(n):
                self.db.add(mdb.Movie(tmdb_id=next(tmdb), title=f"{tag} {i}", source="provider_1", provider_id=1,
                                      source_tag=tag, date_added=now - timedelta(days=90)))
        self.db.add(mdb.Movie(tmdb_id=next(tmdb), title="New one", source="radarr", date_added=now))
        self.db.add(mdb.Series(tmdb_id=next(tmdb), title="New show", source="sonarr", date_added=now))
        self.db.commit()

        self.home = self.tmp / "home-configs"
        self.home.mkdir()
        self.built = {}       # user id -> [smartlist dicts]
        self.syncs = []
        self.notified = []
        self.extra = {}       # user id -> their own playlists (lists, custom)

        def path(db, user_id=None):
            u = db.query(mdb.TentacleUser).filter(mdb.TentacleUser.id == user_id).first()
            return self.home / f"{u.jellyfin_user_id}.json"

        def smartlists(db, user_id=None):
            return list(self.built.get(user_id, [])) + list(self.extra.get(user_id, []))

        def sync(db, user_id=None):
            self.syncs.append(user_id)
            self.built[user_id] = [
                {"name": a["name"], "playlist_id": f"{user_id}-{a['key']}", "sort_by": "datecreated",
                 "sort_order": "Descending", "is_youtube": False}
                for a in rsl._compute_auto_playlists(db, user_id=user_id) if a.get("enabled")]
            return {}

        for p in (
            mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.home)),
            mock.patch.object(sl, "_user_home_config_path", path),
            mock.patch.object(sl, "_get_smartlists_with_playlist_ids", smartlists),
            mock.patch.object(rsl, "_get_smartlists_with_playlist_ids", smartlists),
            mock.patch.object(sl, "sync_smartlists", sync),
            mock.patch.object(sl, "refresh_smartlist_playlists", lambda db, user_id=None: {}),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db, user=None: self.notified.append(user) or {"notified": True}),
            mock.patch.object(rsl, "_notify_jellyfin_plugin", lambda db, user=None: self.notified.append(user) or {"notified": True}),
            mock.patch.object(sl, "bump_playlist_version"),
            mock.patch.object(rsl, "bump_playlist_version"),
            # The background run, run at once
            mock.patch.object(dh, "apply_to_followers_soon", lambda reason="": self._apply_all()),
            mock.patch.object(dh, "apply_soon", lambda uid: dh.apply_default(self.db, self.user(uid))),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _apply_all(self):
        for u in dh.followers(self.db):
            dh.apply_default(self.db, u)

    def user(self, uid):
        return self.db.query(mdb.TentacleUser).filter(mdb.TentacleUser.id == uid).first()

    def file(self, uid):
        return self.home / f"{self.user(uid).jellyfin_user_id}.json"

    def cfg(self, uid):
        return json.loads(self.file(uid).read_text())

    def names(self, uid):
        return [r["display_name"] for r in sorted(self.cfg(uid)["rows"], key=lambda r: r["order"])]

    def write(self, uid, cfg):
        self.file(uid).write_text(json.dumps(cfg))


class SuggestedDefault(Base):
    def test_a_library_gets_continue_watching_on_now_recent_and_the_biggest_sources(self):
        """Lucas's call (Sections.md, 2026-10-21): Next Up merged into
        Continue Watching, On Now instead of Jellyfin's Live TV row, Tentacle's
        recently added rows instead of Recently Added Media, no My Media."""
        d = dh.suggested_default(self.db)
        self.assertEqual([r["display_name"] for r in d["rows"]],
                         ["Continue Watching", "On Now", "Recently Added Movies", "Recently Added TV",
                          "Netflix Movies", "Hulu Movies"])
        self.assertTrue(d["merge_continue_watching"])
        self.assertTrue(d["hero"]["enabled"])
        self.assertEqual(d["hero"]["playlist_id"], "builtin:recently_added_movies")
        self.assertNotIn("smalllibrarytiles", [r.get("section_id") for r in d["rows"]],
                         "the stock Jellyfin 'My Media' tiles are not part of a Tentacle home")

    def test_an_empty_library_still_has_rows(self):
        self.db.query(mdb.Movie).delete()
        self.db.query(mdb.Series).delete()
        self.db.commit()
        d = dh.suggested_default(self.db)
        self.assertEqual([r.get("section_id") for r in d["rows"]], ["resumevideo", "onnow", "latestmedia"])
        self.assertFalse(d["hero"]["enabled"])

    def test_the_admins_saved_default_wins(self):
        dh.save_default(self.db, {"rows": [{"type": "builtin", "section_id": "nextup", "display_name": "Next Up"}]})
        self.assertEqual([r["section_id"] for r in dh.get_default(self.db)["rows"]], ["nextup"])
        dh.clear_default(self.db)
        self.assertEqual(len(dh.get_default(self.db)["rows"]), 6)

    def test_only_playlists_everyone_has_can_be_in_the_default(self):
        cfg = dh.save_default(self.db, {"rows": [
            {"type": "playlist", "playlist_id": "list:7", "display_name": "Lucas's list"},
            {"type": "playlist", "playlist_id": "source:Netflix:movies", "display_name": "Netflix Movies"}],
            "hero": {"enabled": True, "playlist_id": "0123abcd", "display_name": "Mine"}})
        self.assertEqual([r["playlist_id"] for r in cfg["rows"]], ["source:Netflix:movies"])
        self.assertFalse(cfg["hero"]["enabled"])


class NewPeople(Base):
    def test_the_first_read_gives_the_default_not_jellyfins_sections(self):
        out = rsl.read_home(db=self.db, user=self.user(2))
        self.assertTrue(out["exists"])
        self.assertTrue(out["follows_default"])
        self.assertEqual(self.names(2), ["Continue Watching", "On Now", "Recently Added Movies",
                                         "Recently Added TV", "Netflix Movies", "Hulu Movies"])
        cfg = self.cfg(2)
        self.assertEqual(cfg["hero"]["playlist_id"], "2-builtin:recently_added_movies",
                         "the spotlight is their own copy of the playlist")
        self.assertTrue(all(r["playlist_id"].startswith("2-") for r in cfg["rows"] if r["type"] == "playlist"))
        self.assertTrue(cfg["toolbar"], "the TV app hides every button without a toolbar (#119)")

    def test_the_ready_made_playlists_it_needs_are_switched_on(self):
        dh.apply_default(self.db, self.user(3))
        on = {t.key for t in self.db.query(mdb.AutoPlaylistToggle).filter_by(user_id=3, enabled=True)}
        self.assertEqual(on, {"builtin:recently_added_movies", "builtin:recently_added_tv",
                              "source:Netflix:movies", "source:Hulu:movies"})
        self.assertIn(3, self.syncs)

    def test_a_switch_they_turned_off_elsewhere_is_never_turned_off(self):
        self.db.add(mdb.AutoPlaylistToggle(user_id=3, key="builtin:downloaded_movies", enabled=True))
        self.db.commit()
        dh.apply_default(self.db, self.user(3))
        t = self.db.query(mdb.AutoPlaylistToggle).filter_by(user_id=3, key="builtin:downloaded_movies").one()
        self.assertTrue(t.enabled)

    def test_a_write_for_someone_with_no_home_gives_the_default(self):
        cfg = sl.write_home_config(self.db, user_id=2)
        self.assertTrue(cfg.get("follows_default"))
        self.assertEqual(len(cfg["rows"]), 6)

    def test_a_kid_gets_the_same_rows(self):
        """Rows are filtered per item by the plugin (IsVisibleStandalone: the
        kid's age limit), so the layout itself is the same."""
        dh.apply_default(self.db, self.user(2))
        dh.apply_default(self.db, self.user(4))
        self.assertEqual(self.names(4), self.names(2))

    def test_without_building_only_rows_they_already_have_show(self):
        cfg = dh.apply_default(self.db, self.user(2), build=False)
        self.assertEqual([r.get("section_id") for r in cfg["rows"]], ["resumevideo", "onnow"])
        self.assertNotIn(2, self.syncs)


class OwnHomes(Base):
    def setUp(self):
        super().setUp()
        rsl.read_home(db=self.db, user=self.user(2))

    def test_changing_your_own_rows_makes_the_home_yours(self):
        order = [rsl._row_key(r) for r in reversed(self.cfg(2)["rows"])]
        rsl.reorder(rsl.ReorderRequest(order=order), db=self.db, user=self.user(2))
        self.assertNotIn("follows_default", self.cfg(2))
        mine = self.names(2)
        dh.save_default(self.db, {"rows": [{"type": "builtin", "section_id": "nextup", "display_name": "Next Up"}]})
        self._apply_all()
        self.assertEqual(self.names(2), mine, "a person's own layout is never overwritten")

    def test_every_layout_change_counts(self):
        for change in (
            lambda u: rsl.remove_row(rsl.RemoveRowRequest(row_key="builtin:nextup"), db=self.db, user=u),
            lambda u: rsl.set_hero(rsl.HeroPickRequest(playlist_id=""), db=self.db, user=u),
            lambda u: rsl.set_row_shape(rsl.RowShapeRequest(row_key="playlist:2-source:Netflix:movies", shape="wide"),
                                        db=self.db, user=u),
            lambda u: rsl.set_merge_continue_watching(rsl.MergeContinueRequest(enabled=True), db=self.db, user=u),
        ):
            rsl.reset_home_to_default(db=self.db, user=self.user(2))
            self.assertTrue(self.cfg(2)["follows_default"])
            change(self.user(2))
            self.assertNotIn("follows_default", self.cfg(2))

    def test_the_toolbar_is_not_the_layout(self):
        rsl.set_toolbar(rsl.ToolbarRequest(buttons=[rsl.ToolbarButton(id="search", enabled=True)]),
                        db=self.db, user=self.user(2))
        self.assertTrue(self.cfg(2)["follows_default"])

    def test_reset_to_default(self):
        rsl.remove_row(rsl.RemoveRowRequest(row_key="builtin:onnow"), db=self.db, user=self.user(2))
        rsl.set_toolbar(rsl.ToolbarRequest(buttons=[rsl.ToolbarButton(id="genres", enabled=True)]),
                        db=self.db, user=self.user(2))
        rsl.reset_home_to_default(db=self.db, user=self.user(2))
        self.assertTrue(self.cfg(2)["follows_default"])
        self.assertIn("On Now", self.names(2))
        self.assertTrue(next(b for b in self.cfg(2)["toolbar"] if b["id"] == "genres")["enabled"],
                        "their toolbar stays")

    def test_a_regeneration_keeps_following(self):
        sl.write_home_config(self.db, user_id=2)
        self.assertTrue(self.cfg(2)["follows_default"])


class AdminEditsTheDefault(Base):
    def setUp(self):
        super().setUp()
        for uid in (2, 3):
            rsl.read_home(db=self.db, user=self.user(uid))
        rsl.remove_row(rsl.RemoveRowRequest(row_key="builtin:resumevideo"), db=self.db, user=self.user(3))
        self.fahad = self.names(3)
        self.notified.clear()

    def test_a_change_reaches_everyone_on_it_and_nobody_else(self):
        rsl.remove_row(rsl.RemoveRowRequest(row_key="playlist:source:Hulu:movies"),
                       db=self.db, user=self.user(1), scope="default")
        self.assertNotIn("Hulu Movies", self.names(2))
        self.assertEqual(self.names(3), self.fahad)
        self.assertIn(self.user(2), self.notified, "the follower's clients are told (live)")

    def test_rows_order_shape_and_spotlight(self):
        admin = self.user(1)
        rsl.add_row(rsl.AddRowRequest(playlist_id="builtin:downloaded_tv"), db=self.db, user=admin, scope="default")
        rsl.reorder(rsl.ReorderRequest(order=["playlist:builtin:downloaded_tv", "builtin:onnow"]),
                    db=self.db, user=admin, scope="default")
        rsl.set_row_shape(rsl.RowShapeRequest(row_key="playlist:builtin:downloaded_tv", shape="wide"),
                          db=self.db, user=admin, scope="default")
        rsl.set_hero(rsl.HeroPickRequest(playlist_id="source:Netflix:movies"), db=self.db, user=admin, scope="default")
        self.assertEqual(self.names(2)[:2], ["Downloaded TV", "On Now"])
        row = self.cfg(2)["rows"][0]
        self.assertEqual((row["playlist_id"], row["shape"]), ("2-builtin:downloaded_tv", "wide"))
        self.assertEqual(self.cfg(2)["hero"]["playlist_id"], "2-source:Netflix:movies")
        self.assertNotIn("follows_default", self.cfg(3))

    def test_the_editor_offers_ready_made_playlists_not_the_admins_own(self):
        self.extra[1] = [{"name": "Lucas's list", "playlist_id": "pl-own", "is_youtube": False}]
        got = rsl.available_playlists(db=self.db, user=self.user(1), scope="default")["playlists"]
        names = {p["name"] for p in got}
        self.assertIn("Downloaded Movies", names)
        self.assertIn("My Downloads", names)
        self.assertNotIn("Lucas's list", names)
        self.assertNotIn("Netflix Movies", names, "already a row of the default")

    def test_only_admins(self):
        with self.assertRaises(HTTPException) as e:
            rsl.remove_row(rsl.RemoveRowRequest(row_key="builtin:nextup"), db=self.db, user=self.user(2), scope="default")
        self.assertEqual(e.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            rsl.read_home(db=self.db, user=self.user(2), scope="default")

    def test_the_default_editor_says_who_uses_it(self):
        out = rsl.read_home(db=self.db, user=self.user(1), scope="default")
        self.assertIn("ethan", out["followers"])
        self.assertNotIn("fahad", out["followers"])
        self.assertFalse(out["custom"])

    def test_copy_my_home_leaves_out_what_only_i_have(self):
        self.extra[1] = [{"name": "Crime Docs", "playlist_id": "pl-list", "is_youtube": False},
                         {"name": "lucas's Downloads", "playlist_id": "pl-dl", "is_youtube": False}]
        self.db.add(mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=1))
        self.db.add(mdb.AutoPlaylistToggle(user_id=1, key="source:Netflix:movies", enabled=True))
        self.db.commit()
        sl.sync_smartlists(self.db, user_id=1)
        self.write(1, {"hero": {"enabled": True, "playlist_id": "1-source:Netflix:movies", "display_name": "Netflix Movies"},
                       "rows": [{"type": "playlist", "playlist_id": "pl-list", "display_name": "Crime Docs", "order": 1},
                                {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 2},
                                {"type": "playlist", "playlist_id": "1-source:Netflix:movies", "display_name": "Netflix Movies", "order": 3, "shape": "wide"},
                                {"type": "playlist", "playlist_id": "pl-dl", "display_name": "lucas's Downloads", "order": 4}]})
        rsl.default_home_from_mine(db=self.db, user=self.user(1))
        d = dh.saved_default(self.db)
        self.assertEqual([r["display_name"] for r in d["rows"]], ["Continue Watching", "Netflix Movies", "My Downloads"])
        self.assertEqual(d["rows"][1]["shape"], "wide")
        self.assertEqual(d["hero"]["playlist_id"], "source:Netflix:movies")
        self.assertEqual(self.names(2), ["Continue Watching", "Netflix Movies"],
                         "ethan has no downloads, so no Downloads row")


class ExistingHomes(Base):
    def test_old_untouched_starters_move_over_once_and_own_layouts_stay(self):
        starter = {"hero": {"enabled": False, "playlist_id": "", "display_name": ""},
                   "rows": [{"type": "builtin", "section_id": s, "display_name": n, "order": i}
                            for i, (s, n) in enumerate(LEGACY_STARTER, 1)],
                   "toolbar": [{"id": "search", "enabled": True}],
                   "jellyfin_sections_snapshot": {f"homesection{i}": "" for i in range(10)}}
        own = {"hero": {"enabled": True, "playlist_id": "x", "display_name": "Recently Added Movies"},
               "rows": [{"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1}]}
        self.write(1, own)
        self.write(2, starter)
        self.write(3, dict(starter, rows=starter["rows"][1:]))   # removed My Media: their own
        self.assertEqual(dh.migrate_existing_homes(self.db), 1)
        self._apply_all()
        self.assertEqual(self.cfg(1), own)
        self.assertEqual(self.names(3), [n for _, n in LEGACY_STARTER[1:]])
        self.assertTrue(self.cfg(2)["follows_default"])
        self.assertEqual(self.names(2)[:2], ["Continue Watching", "On Now"])
        self.assertEqual(self.cfg(2)["toolbar"], starter["toolbar"])
        self.assertEqual(self.cfg(2)["jellyfin_sections_snapshot"], starter["jellyfin_sections_snapshot"])
        self.assertIn(self.user(4), dh.followers(self.db), "no home yet: they get the default")
        self.assertEqual(dh.migrate_existing_homes(self.db), 0, "only once")

    def test_a_starter_mirrored_from_their_jellyfin_sections_counts_as_untouched(self):
        snap = {f"homesection{i}": "" for i in range(10)}
        snap.update(homesection0="nextup", homesection1="resume")
        cfg = {"hero": {"enabled": False}, "jellyfin_sections_snapshot": snap,
               "rows": [{"type": "builtin", "section_id": "nextup", "order": 1},
                        {"type": "builtin", "section_id": "resumevideo", "order": 2}]}
        self.assertTrue(dh.is_untouched_seed(cfg))
        cfg["rows"][0]["order"], cfg["rows"][1]["order"] = 2, 1   # reordered by hand
        self.assertFalse(dh.is_untouched_seed(cfg))

    def test_an_unreadable_home_is_left_alone(self):
        self.file(2).write_text('{"rows": [')
        dh.apply_default(self.db, self.user(2))
        self.assertEqual(self.file(2).read_text(), '{"rows": [')


if __name__ == "__main__":
    unittest.main()


class Picker(Base):
    def test_retired_jellyfin_sections_are_not_offered_and_on_now_is(self):
        rsl.read_home(db=self.db, user=self.user(2))
        offered = [x["section_id"] for x in rsl.list_builtin_sections(db=self.db, user=self.user(2))["sections"]]
        for gone in ("smalllibrarytiles", "smalllibrarytiles_small", "resumebook", "livetv"):
            self.assertNotIn(gone, offered)
        self.assertIn("nextup", offered)
        r = rsl.add_row(rsl.AddRowRequest(section_id="smalllibrarytiles"), db=self.db, user=self.user(2))
        self.assertFalse(r["success"])

    def test_a_retired_row_in_someones_own_layout_stays(self):
        self.write(3, {"hero": {"enabled": False}, "rows": [
            {"type": "builtin", "section_id": "smalllibrarytiles", "display_name": "My Media", "order": 1},
            {"type": "builtin", "section_id": "livetv", "display_name": "Live TV", "order": 2}]})
        sl.write_home_config(self.db, user_id=3)
        self.assertEqual(self.names(3), ["My Media", "Live TV"])
