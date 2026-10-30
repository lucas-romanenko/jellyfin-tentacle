"""Every home row comes from Tentacle (Lucas, 2026-10-26: "no Jellyfin
built-in rows at all"). Continue Watching (with next episodes), Next Up, On Now
and Recordings are Tentacle's own rows, configured like any row and drawn the
same on the web and the TV app; Jellyfin's home sections leave every layout
once, and nothing else about a saved layout changes.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import models.database as mdb
import services.default_home as dh
import services.home_rows as hr
from test_default_home import Base

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"


def lucas_layout():
    """Lucas's own home on 26 Oct: Continue Watching 6th, My Media 16th, the
    old Live TV row last, playlists around them."""
    rows = []
    for i in range(1, 26):
        if i == 6:
            rows.append({"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": i})
        elif i == 16:
            rows.append({"type": "builtin", "section_id": "smalllibrarytiles", "display_name": "My Media", "order": i})
        elif i == 25:
            rows.append({"type": "builtin", "section_id": "livetv", "display_name": "Live TV", "order": i})
        else:
            rows.append({"type": "playlist", "playlist_id": f"p{i}", "display_name": f"Playlist {i}",
                         "shape": "wide" if i == 3 else "poster", "max_items": 20, "order": i})
    return {"hero": {"enabled": True, "playlist_id": "p1", "display_name": "Playlist 1"}, "rows": rows}


class NormalizeRows(unittest.TestCase):
    def test_lucas_keeps_every_playlist_row_and_continue_watching_where_they_were(self):
        cfg = lucas_layout()
        playlists_before = [dict(r) for r in cfg["rows"] if r["type"] == "playlist"]
        self.assertTrue(hr.normalize_rows(cfg))
        rows = cfg["rows"]
        self.assertEqual(23, len(rows))
        self.assertNotIn("smalllibrarytiles", [r.get("section_id") for r in rows])
        self.assertNotIn("livetv", [r.get("section_id") for r in rows])
        self.assertEqual("resumevideo", rows[5]["section_id"], "Continue Watching stays 6th")
        self.assertEqual([r["playlist_id"] for r in playlists_before],
                         [r["playlist_id"] for r in rows if r["type"] == "playlist"])
        self.assertEqual(["poster", "poster", "wide"], [r["shape"] for r in rows[:3]])
        self.assertEqual(list(range(1, 24)), [r["order"] for r in rows])
        self.assertEqual("p1", cfg["hero"]["playlist_id"])
        self.assertTrue(cfg["merge_continue_watching"], "next episodes, as decided on 21 Oct")
        self.assertEqual("wide", hr.shape_of(rows[5]), "Continue Watching is drawn wide, as the web showed it")

    def test_next_up_beside_continue_watching_with_next_episodes_leaves(self):
        """Since 30 Oct "nextup" is Tentacle's own Next Up row: it stays unless
        Continue Watching shows the next episodes already (no card twice)."""
        cfg = {"rows": [{"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1,
                         "shape": "poster"},
                        {"type": "builtin", "section_id": "nextup", "display_name": "Next Up", "order": 2}],
               "merge_continue_watching": True}
        self.assertTrue(hr.normalize_rows(cfg))
        self.assertEqual(["resumevideo"], [r["section_id"] for r in cfg["rows"]])
        self.assertEqual("poster", cfg["rows"][0]["shape"], "a chosen shape stays")

    def test_next_episodes_switched_off_stays_off(self):
        cfg = {"rows": [{"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1}],
               "merge_continue_watching": False}
        self.assertFalse(hr.normalize_rows(cfg))
        self.assertFalse(cfg["merge_continue_watching"])

    def test_every_other_jellyfin_section_leaves(self):
        gone = ["latestmedia", "activerecordings", "resumeaudio", "resumebook", "smalllibrarytiles_small"]
        cfg = {"rows": [{"type": "builtin", "section_id": s, "display_name": s, "order": i}
                        for i, s in enumerate(gone + ["onnow"], 1)]}
        hr.normalize_rows(cfg)
        self.assertEqual([("onnow", 1)], [(r["section_id"], r["order"]) for r in cfg["rows"]])

    def test_on_now_never_carries_a_shape_and_a_tidy_home_is_left_alone(self):
        cfg = {"rows": [{"type": "builtin", "section_id": "onnow", "display_name": "On Now", "order": 1, "shape": "wide"}]}
        self.assertTrue(hr.normalize_rows(cfg))
        self.assertNotIn("shape", cfg["rows"][0])
        self.assertEqual("", hr.shape_of(cfg["rows"][0]))
        self.assertFalse(hr.normalize_rows(cfg))


class MigrateHomes(Base):
    def test_once_per_install_layouts_lose_jellyfins_rows_and_only_their_clients_are_told(self):
        mdb.set_setting(self.db, dh.MIGRATED_KEY, "1")
        self.write(1, lucas_layout())
        tidy = {"hero": {"enabled": False}, "merge_continue_watching": True, "rows": [
            {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 1}]}
        self.write(2, tidy)
        dh.save_default(self.db, {"rows": [{"type": "builtin", "section_id": "latestmedia", "display_name": "x"},
                                           {"type": "builtin", "section_id": "onnow", "display_name": "On Now"}]})
        self.assertEqual(1, hr.migrate_homes(self.db))
        self.assertEqual(23, len(self.cfg(1)["rows"]))
        self.assertEqual(tidy, self.cfg(2))
        self.assertEqual([1], [u.id for u in self.notified])
        self.assertEqual(["onnow"], [r["section_id"] for r in dh.saved_default(self.db)["rows"]])
        # Once: a row put back by hand later (an old client) is not taken again
        self.write(1, lucas_layout())
        self.assertEqual(0, hr.migrate_homes(self.db))
        self.assertEqual(25, len(self.cfg(1)["rows"]))


class RecentlyAddedReachesBack(Base):
    def test_a_short_window_reaches_back_to_the_newest_titles(self):
        """A quiet server: one movie in the window. The row reaches back so it
        holds at least RECENTLY_ADDED_MIN titles."""
        now = datetime.utcnow()
        for i in range(30):
            self.db.add(mdb.Movie(tmdb_id=5000 + i, title=f"Old {i}", source="radarr",
                                  date_added=now - timedelta(days=100 + i)))
        self.db.commit()
        cutoff = mdb.recently_added_cutoff(self.db, mdb.Movie)
        n = self.db.query(mdb.Movie).filter(mdb.Movie.date_added >= cutoff).count()
        self.assertEqual(mdb.RECENTLY_ADDED_MIN, n)

    def test_a_full_window_is_the_window(self):
        now = datetime.utcnow()
        for i in range(25):
            self.db.add(mdb.Movie(tmdb_id=6000 + i, title=f"New {i}", source="radarr", date_added=now - timedelta(hours=i)))
        self.db.commit()
        cutoff = mdb.recently_added_cutoff(self.db, mdb.Movie)
        self.assertAlmostEqual((now - timedelta(days=30) - cutoff).total_seconds(), 0, delta=5)

    def test_fewer_titles_than_that_in_all_are_all_recent(self):
        self.assertEqual(datetime.min, mdb.recently_added_cutoff(self.db, mdb.Series))


class PluginAndWebDrawTentaclesRows(unittest.TestCase):
    """No JS or .NET test host here: these read the sources."""

    @classmethod
    def setUpClass(cls):
        cls.controller = (PLUGIN / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")
        cls.rows = (PLUGIN / "HomeScreen" / "TentacleRows.cs").read_text(encoding="utf-8")
        cls.js = (PLUGIN / "Inject" / "tentacle-home.js").read_text(encoding="utf-8")
        cls.css = (PLUGIN / "Inject" / "tentacle-home.css").read_text(encoding="utf-8")

    def _action(self, route):
        start = self.controller.index(f'[HttpGet("{route}")]')
        return self.controller[start:self.controller.index("[Http", start + 10)]

    def test_sections_carry_only_tentacles_rows_with_their_shape(self):
        sections = self._action("Sections")
        self.assertIn("TentacleRows.Find(row.SectionId)", sections)
        self.assertIn("shape = TentacleRows.ShapeOf(row)", sections)
        self.assertIn("includeNextUp", sections)
        self.assertIn('new(ContinueWatching, "Continue Watching", "wide")', self.rows)
        self.assertIn('new(NextUp, "Next Up", "wide")', self.rows)
        self.assertIn('new(OnNow, "On Now", null)', self.rows)
        self.assertIn('new(Recordings, "Recordings", "poster")', self.rows)

    def test_continue_watching_and_recordings_are_the_callers_own(self):
        for route in ("ContinueWatching", "NextUp", "Recordings"):
            body = self._action(route)
            self.assertIn("CallerIdentity.ResolveAsync", body, route)
            self.assertIn("return Forbid();", body, route)

    def test_continue_watching_is_one_list_in_one_order(self):
        body = self._action("ContinueWatching")
        self.assertIn("IsResumable = true", body)
        self.assertIn("NextEpisodes(user, dtoOptions)", body)
        self.assertIn("MergeContinueWatching", body)
        self.assertIn("RowOf(user, entries, limit, dtoOptions)", body)
        self.assertIn("TentacleRows.ContinueWatchingOrder", self.controller)
        self.assertIn("OrderByDescending(k => k.Played)", self.rows)

    def test_the_web_draws_them_from_the_plugin_not_from_jellyfins_sections(self):
        for gone in ("Items/Resume", "Shows/NextUp", "Items/Latest", "/Views", "IsInProgress=true",
                     "function loadLibraryTiles", "renderMerged"):
            self.assertNotIn(gone, self.js)
        self.assertIn("resumevideo: 'TentacleHome/ContinueWatching'", self.js)
        self.assertIn("recordings: 'TentacleHome/Recordings'", self.js)
        self.assertRegex(self.js, r"createContinueCard\(item, wide\)")

    def test_continue_watching_and_recordings_refresh_in_place_on_a_change(self):
        start = self.js.index("function refreshPlaylistRows(")
        body = self.js[start:self.js.index("\n  // ── Utilities", start)]
        self.assertIn("fillTentacleRow(row, section, false)", body)

    def test_on_now_has_its_own_square_card(self):
        self.assertRegex(self.css, r"\.mh-card-poster\.mh-onnow-logo \{[^}]*aspect-ratio: 1 / 1")
        start = self.js.index("function createOnNowCard(")
        card = self.js[start:self.js.index("\n  function ", start + 10)]
        self.assertNotIn("mh-card-wide", card)
        self.assertNotIn("api_key", card)


if __name__ == "__main__":
    unittest.main()
