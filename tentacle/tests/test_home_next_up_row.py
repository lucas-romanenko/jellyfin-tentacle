"""Next Up as a row of its own (Lucas, 2026-10-30: "it should be that continue
watching still has the next up checkbox option, if that is off, Next Up row is
an option in the add row list"), and a new season of a show you had caught up
on comes to the front of the next episodes instead of sinking behind every
show watched since.

Run from the tentacle/ directory:  python -m unittest tests.test_home_next_up_row
"""
import re
import unittest
from pathlib import Path

import routers.smartlists as rsl
import services.default_home as dh
import services.home_rows as hr
from test_default_home import Base

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"
CW = {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "shape": "wide"}
NU = {"type": "builtin", "section_id": "nextup", "display_name": "Next Up", "shape": "wide"}
PL = {"type": "playlist", "playlist_id": "p1", "display_name": "Netflix Movies", "shape": "poster"}


def home(rows, merge):
    return {"hero": {"enabled": False, "playlist_id": "", "display_name": ""},
            "rows": [{**r, "order": i} for i, r in enumerate(rows, 1)], "merge_continue_watching": merge}


class AddRowList(Base):
    def offered(self, uid=2, scope=None):
        return [s["section_id"] for s in rsl.list_builtin_sections(db=self.db, user=self.user(uid), scope=scope)["sections"]]

    def test_next_up_is_offered_only_while_next_episodes_are_off(self):
        self.write(2, home([CW, PL], True))
        self.assertNotIn("nextup", self.offered())
        rsl.set_merge_continue_watching(rsl.MergeContinueRequest(enabled=False), db=self.db, user=self.user(2))
        self.assertIn("nextup", self.offered())
        self.assertEqual(["Next Up", "On Now", "Recordings"],
                         [s["display_name"] for s in rsl.list_builtin_sections(db=self.db, user=self.user(2))["sections"]])

    def test_without_continue_watching_on_the_home_next_up_is_offered(self):
        self.write(2, home([PL], True))
        self.assertIn("nextup", self.offered())

    def test_add_next_up_wide_at_the_top(self):
        self.write(2, home([CW, PL], False))
        r = rsl.add_row(rsl.AddRowRequest(section_id="nextup"), db=self.db, user=self.user(2))
        self.assertTrue(r["success"])
        rows = sorted(self.cfg(2)["rows"], key=lambda r: r["order"])
        self.assertEqual(("nextup", "Next Up", "wide", 1), tuple(rows[0][k] for k in ("section_id", "display_name", "shape", "order")))
        self.assertEqual(["Next Up", "Continue Watching", "Netflix Movies"], self.names(2))
        self.assertNotIn("nextup", self.offered(), "once on the home, not offered again")

    def test_adding_next_up_while_continue_watching_shows_them_is_refused(self):
        self.write(2, home([CW, PL], True))
        r = rsl.add_row(rsl.AddRowRequest(section_id="nextup"), db=self.db, user=self.user(2))
        self.assertFalse(r["success"])
        self.assertIn("Next episodes", r["message"])
        self.assertEqual(["Continue Watching", "Netflix Movies"], self.names(2))


class NoCardTwice(Base):
    def test_switching_next_episodes_on_takes_the_next_up_row_out(self):
        self.write(2, home([PL, NU, CW], False))
        r = rsl.set_merge_continue_watching(rsl.MergeContinueRequest(enabled=True), db=self.db, user=self.user(2))
        self.assertTrue(r["removed_next_up"])
        self.assertEqual(["Netflix Movies", "Continue Watching"], self.names(2))
        self.assertEqual([1, 2], sorted(r["order"] for r in self.cfg(2)["rows"]))
        self.assertTrue(self.cfg(2)["merge_continue_watching"])

    def test_switching_off_or_on_without_a_next_up_row_changes_no_row(self):
        self.write(2, home([CW, PL], True))
        r = rsl.set_merge_continue_watching(rsl.MergeContinueRequest(enabled=False), db=self.db, user=self.user(2))
        self.assertFalse(r["removed_next_up"])
        self.assertEqual(["Continue Watching", "Netflix Movies"], self.names(2))

    def test_next_up_alone_stays_when_the_switch_goes_on(self):
        """No Continue Watching on the home: nothing else shows the next episodes."""
        self.write(2, home([NU, PL], False))
        r = rsl.set_merge_continue_watching(rsl.MergeContinueRequest(enabled=True), db=self.db, user=self.user(2))
        self.assertFalse(r["removed_next_up"])
        self.assertEqual(["Next Up", "Netflix Movies"], self.names(2))

    def test_adding_continue_watching_beside_next_up_keeps_both_apart(self):
        self.write(2, home([NU, PL], True))
        self.assertTrue(rsl.add_row(rsl.AddRowRequest(section_id="resumevideo"), db=self.db, user=self.user(2))["success"])
        self.assertEqual(["Continue Watching", "Next Up", "Netflix Movies"], self.names(2))
        self.assertFalse(self.cfg(2)["merge_continue_watching"], "Continue Watching keeps to what was started")


class Normalize(unittest.TestCase):
    def test_next_up_with_next_episodes_off_stays_where_it_is(self):
        cfg = home([PL, NU, CW], False)
        self.assertFalse(hr.normalize_rows(cfg))
        self.assertEqual(["p1", "nextup", "resumevideo"], [r.get("playlist_id") or r["section_id"] for r in cfg["rows"]])
        self.assertEqual("wide", hr.shape_of(cfg["rows"][1]))

    def test_an_unsaid_switch_beside_next_up_is_off(self):
        cfg = home([CW, NU], None)
        del cfg["merge_continue_watching"]
        hr.normalize_rows(cfg)
        self.assertEqual(["resumevideo", "nextup"], [r["section_id"] for r in cfg["rows"]])
        self.assertFalse(cfg["merge_continue_watching"])

    def test_merges_next_up(self):
        self.assertTrue(hr.merges_next_up(home([CW], True)))
        self.assertFalse(hr.merges_next_up(home([CW], False)))
        self.assertFalse(hr.merges_next_up(home([PL], True)), "no Continue Watching row")


class DefaultHome(Base):
    def test_a_default_with_next_up_reaches_the_followers(self):
        dh.save_default(self.db, home([CW, NU, PL], False))
        self.assertEqual(["resumevideo", "nextup"],
                         [r["section_id"] for r in dh.saved_default(self.db)["rows"] if r["type"] == "builtin"])
        rsl.reset_home_to_default(db=self.db, user=self.user(2))
        cfg = self.cfg(2)
        self.assertEqual(["Continue Watching", "Next Up"], [r["display_name"] for r in cfg["rows"] if r["type"] == "builtin"])
        self.assertFalse(cfg["merge_continue_watching"])

    def test_the_suggested_default_is_unchanged(self):
        d = dh.suggested_default(self.db)
        self.assertTrue(d["merge_continue_watching"])
        self.assertNotIn("nextup", [r.get("section_id") for r in d["rows"]])


class PluginAndWeb(unittest.TestCase):
    """No .NET or JS test host here: these read the sources. The ordering itself
    (TentacleRows.NextUpDate) was checked with real dates in the job's harness."""

    @classmethod
    def setUpClass(cls):
        cls.controller = (PLUGIN / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")
        cls.rows = (PLUGIN / "HomeScreen" / "TentacleRows.cs").read_text(encoding="utf-8")
        cls.js = (PLUGIN / "Inject" / "tentacle-home.js").read_text(encoding="utf-8")

    def _action(self, route):
        start = self.controller.index(f'[HttpGet("{route}")]')
        return self.controller[start:self.controller.index("[Http", start + 10)]

    def test_next_up_row_reads_all_then_orders_then_limits(self):
        body = self._action("NextUp")
        self.assertIn("TentacleRows.NextUp", body)
        self.assertIn("RowOf(user, NextEpisodes(user, dtoOptions), limit, dtoOptions)", body)
        start = self.controller.index("private List<(BaseItem Item, DateTime Played)> NextEpisodes(")
        reader = self.controller[start:self.controller.index("\n    }\n", start)]
        self.assertIn("Limit = NextUpFetchLimit", reader, "not the row's limit: an old show with a new season would be cut first")
        self.assertRegex(self.controller, r"private const int NextUpFetchLimit = (\d{3,});")
        self.assertIn("TentacleRows.NextUpDate(", reader)
        self.assertIn("next.DateCreated", reader)

    def test_a_new_episode_counts_from_when_it_arrived_unless_rescanned(self):
        start = self.rows.index("public static DateTime NextUpDate(")
        body = self.rows[start:self.rows.index("\n    }\n", start)]
        self.assertIn("added <= lastPlayed", body)
        self.assertIn("added - before < NewEpisodeGap", body)
        self.assertIn("TimeSpan.FromHours(6)", self.rows)

    def test_sections_never_send_next_up_beside_merged_continue_watching(self):
        body = self._action("Sections")
        self.assertIn("tentacleRow.SectionId == TentacleRows.NextUp && config.MergeContinueWatching", body)

    def test_the_web_draws_next_up_from_the_plugin(self):
        self.assertIn("nextup: 'TentacleHome/NextUp'", self.js)
        self.assertRegex(self.js, re.escape("section.sectionId === 'resumevideo' || section.sectionId === 'nextup'"))
        self.assertNotIn("Shows/NextUp", self.js)


if __name__ == "__main__":
    unittest.main()
