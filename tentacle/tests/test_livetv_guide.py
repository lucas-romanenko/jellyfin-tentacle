"""Tentacle's Live TV guide (tentacle-plugin/Inject/tentacle-livetv.js).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Live TV opens straight to the guide with a Favorites / All toggle; the date
strip runs from today to the last day that has programme data (not
Jellyfin's fixed two weeks); channel and programme names show exactly.
Runs the real script under node with a stub document.
"""
import json
import re
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, NAMES, Page, node

JS = (INJECT / "tentacle-livetv.js").read_text(encoding="utf-8")
CSS = (INJECT / "tentacle-livetv.css").read_text(encoding="utf-8")

HARNESS = ESC_DOM + r"""
process.env.TZ = 'America/Toronto';
var ApiClient = {
  getCurrentUserId: function () { return 'u1'; },
  serverId: function () { return 's1'; },
  getUrl: function (p, q) { return p + (q ? '?' + new URLSearchParams(q) : ''); },
  getJSON: function () { return Promise.resolve({ Items: [] }); },
  ajax: function () { return Promise.resolve({ Items: [] }); },
};
var window = { ApiClient: ApiClient, addEventListener: function () {}, innerWidth: 1440,
  location: { hash: '#/home' } };
var location = window.location;
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
%s
var T = window.TentacleLiveTV.internals;
var out = (function () { %s })();
process.stdout.write(JSON.stringify(out));
"""


def run(body: str):
    return node(HARNESS % (JS, body))


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DatesOnlyAsFarAsTheGuide(unittest.TestCase):
    def days(self, max_end_expr: str):
        return run("""
          var now = new Date(); var mid = new Date(now); mid.setHours(0, 0, 0, 0);
          var D = function (n, h) { var d = new Date(mid); d.setDate(d.getDate() + n); d.setHours(h || 0, 0, 0, 0); return d.getTime(); };
          var days = T.computeDays(%s);
          return days.map(function (d) { return [(d.start - mid.getTime()) / 3600000, (d.end - d.start) / 3600000]; });
        """ % max_end_expr)

    def test_guide_ending_tomorrow_morning_gives_today_and_tomorrow(self):
        # Data until 5:00 tomorrow: two days, the second ends with the data.
        days = self.days("D(1, 5)")
        self.assertEqual(2, len(days))
        self.assertEqual(24, days[0][1])
        self.assertEqual(5, days[1][1])

    def test_no_guide_data_is_today_only(self):
        self.assertEqual(1, len(self.days("0")))

    def test_guide_data_that_already_ended_is_today_only(self):
        self.assertEqual(1, len(self.days("Date.now() - 3600000")))

    def test_data_to_midnight_does_not_add_an_empty_day(self):
        days = self.days("D(2, 0)")
        self.assertEqual(2, len(days))
        self.assertEqual(24, days[1][1])

    def test_a_week_of_data_is_eight_days_not_fourteen(self):
        days = self.days("D(7, 12)")
        self.assertEqual(8, len(days))
        self.assertEqual(12, days[-1][1])

    def test_last_day_ends_on_the_next_whole_hour(self):
        days = self.days("D(1, 4) + 20 * 60000")
        self.assertEqual(5, days[-1][1])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class OpensStraightToTheGuide(unittest.TestCase):
    def test_live_tv_landing_and_guide_tab_are_the_guide(self):
        got = run("""
          return ['#/livetv', '#/livetv.html', '#/livetv?tab=0', '#/livetv?tab=1', '#/livetv?tab=3',
                  '#/livetv?tab=4', '#/home', '#/details?id=1'].map(function (h) {
            location.hash = h; return T.isGuidePage(); });
        """)
        self.assertEqual([True, True, True, True, False, False, False, False], got)

    def test_no_favourite_tiles_landing_any_more(self):
        self.assertNotIn("tltv-card", JS)
        self.assertNotIn("IsFavorite=true", JS)

    def test_toggle_is_favorites_and_all_and_is_remembered_per_person(self):
        self.assertIn("'favorites', 'all'", JS)
        self.assertIn("DisplayPreferences/' + PREFS_ID", JS)
        self.assertIn("CustomPrefs.guideView = G.view", JS)
        self.assertIn("'tentacle-guide-view-' + uid", JS)

    def test_no_favourites_says_how_to_star_and_offers_all(self):
        self.assertIn("No favorite channels yet. In All, tap the ☆ next to a channel to add it here.", JS)
        self.assertIn('data-view="all">Show all channels', JS)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class NamesShowExactly(unittest.TestCase):
    def test_channel_and_programme_names(self):
        got = run("""
          var names = %s, now = Date.now(), day = { start: now - 3600000, end: now + 3600000 };
          var chans = names.map(function (n, i) { return T.chanCell({ id: 'c' + i, name: n, number: '1' + i, logo: '', fav: i %% 2 === 0 }); });
          var list = names.map(function (n, i) { return { id: 'p' + i, name: n, s: day.start + i * 60000, e: day.start + (i + 1) * 60000, ov: '' }; });
          return { chans: chans.join(''), progs: T.progsHtml(list, day, now) };
        """ % json.dumps(NAMES))
        chans = Page(got["chans"])
        self.assertEqual(NAMES, [a["title"] for t, a in chans.elements if "tg-chan" == a.get("class")])
        progs = Page(got["progs"])
        self.assertEqual(NAMES, [a["title"] for t, a in progs.elements if "data-s" in a])
        for n in NAMES:
            self.assertIn(n, chans.text)
            self.assertIn(n, progs.text)

    def test_now_playing_is_marked_and_past_dimmed(self):
        got = run("""
          var now = Date.now(), day = { start: now - 7200000, end: now + 7200000 };
          return T.progsHtml([
            { id: 'a', name: 'Past', s: day.start, e: now - 60000, ov: '' },
            { id: 'b', name: 'Now', s: now - 60000, e: now + 60000, ov: '' },
            { id: 'c', name: 'Later', s: now + 60000, e: day.end, ov: '' }], day, now);
        """)
        classes = [a["class"] for t, a in Page(got).elements if "data-s" in a]
        self.assertEqual(["tg-prog past", "tg-prog live", "tg-prog"], classes)
        self.assertIn('class="tg-bar"', got)

    def test_channel_without_guide_data_says_so(self):
        got = run("var day = { start: 0, end: 3600000 }; return T.progsHtml([], day, 0);")
        self.assertIn("No guide information", got)

    def test_times_are_readable(self):
        got = run("""
          var a = new Date(2026, 9, 21, 14, 0), b = new Date(2026, 9, 21, 16, 30), c = new Date(2026, 9, 21, 11, 30);
          return [T.fmtRange(a.getTime(), b.getTime()), T.fmtRange(c.getTime(), b.getTime())];
        """)
        self.assertTrue(re.fullmatch(r"2:00 – 4:30\s?PM", got[0]), got[0])
        self.assertTrue(re.fullmatch(r"11:30\s?AM – 4:30\s?PM", got[1]), got[1])


class FastWithThousandsOfChannels(unittest.TestCase):
    def test_rows_are_virtualized_and_programmes_batched(self):
        self.assertIn("OVERSCAN", JS)
        self.assertIn("var BATCH = 100;", JS)
        self.assertIn("apiPost('LiveTv/Programs'", JS)
        self.assertIn("G.mounted[id].remove()", JS)

    def test_sticky_titles_need_overflow_clip_not_hidden(self):
        rule = CSS[CSS.index(".tg-prog {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("overflow: clip", rule)


if __name__ == "__main__":
    unittest.main()
