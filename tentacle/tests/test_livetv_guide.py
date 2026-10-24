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
// the guide keeps a timer running (it watches the route): exit once written
process.stdout.write(JSON.stringify(out), function () { process.exit(0); });
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


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class OverlappingProgrammesDrawSideBySide(unittest.TestCase):
    def test_a_guide_that_overlaps_itself_draws_no_cell_on_another(self):
        # Channel 19's merged guide: 16:30-19:30, 17:00-20:00, 18:00-20:00, 19:30-21:00
        got = run("""
          var H = 3600000, day = { start: 0, end: 24 * H };
          return T.progsHtml([
            { id: 'a', name: 'Live: MLB Baseball', s: 16.5 * H, e: 19.5 * H, ov: '' },
            { id: 'b', name: 'Live: MLB Central', s: 17 * H, e: 20 * H, ov: '' },
            { id: 'c', name: 'Live: MLB Central', s: 18 * H, e: 20 * H, ov: '' },
            { id: 'd', name: 'Live: Intentional Talk', s: 19.5 * H, e: 21 * H, ov: '' }], day, 0);
        """)
        cells = [a for t, a in Page(got).elements if "data-s" in a]
        spans = []
        for a in cells:
            left = int(re.search(r"left:(\d+)px", a["style"]).group(1))
            width = int(re.search(r"width:(\d+)px", a["style"]).group(1))
            spans.append((left, left + width))
        for (l1, r1), (l2, r2) in zip(spans, spans[1:]):
            self.assertLessEqual(r1, l2, spans)
        # the later one shows the time left to it (19:30-20:00), the one with none left is not drawn
        self.assertEqual(["a", "b", "d"], [a["data-id"] for a in cells])


# A guide of 400 channels on a 1440 px screen: rows 66 px, 6.5 px a minute.
PLACE = """
  var H = 3600000, G = T.G, mid = new Date(); mid.setHours(0, 0, 0, 0); mid = mid.getTime();
  G.userId = 'u1'; G.view = 'all'; G.m = { phone: false, ppm: 6.5, rowH: 66, chanW: 260, headH: 38 };
  G.channels = []; for (var i = 0; i < 400; i++) G.channels.push({ id: 'c' + i, name: 'C' + i, number: '' + i, logo: '', fav: false });
  G.rows = G.channels.slice(); G.days = [{ start: mid, end: mid + 24 * H }]; G.dayIndex = 0;
  var sc = { scrollTop: 0, scrollLeft: 0, clientHeight: 900, querySelector: function () { return null; } };
  G.container = { querySelector: function (q) { return q === '.tg-scroller' ? sc : null; } };
"""


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class KeepsYourPlace(unittest.TestCase):
    def test_back_where_it_was_rows_and_time(self):
        # a time far from now (it failed every evening at 23:08-23:38, when
        # the fixed 23:00 was "now")
        got = run(PLACE + """
          var h = (new Date().getHours() + 12) % 24;
          sc.scrollTop = 300 * 66 + 20; sc.scrollLeft = Math.round((mid + h * H - G.days[0].start) / 60000 * 6.5);
          T.keepPlace();
          var p = G.place; sc.scrollTop = 0; sc.scrollLeft = 0;
          T.restorePlace(p);
          return [sc.scrollTop, sc.scrollLeft - Math.round(h * 60 * 6.5), p.view, p.day === mid, p.atNow];
        """)
        self.assertEqual([300 * 66 + 20, 0, "all", True, False], got)

    def test_rows_that_came_meanwhile_do_not_shift_it(self):
        got = run(PLACE + """
          sc.scrollTop = 300 * 66;
          T.keepPlace();
          var p = G.place;
          G.rows = [{ id: 'new1' }, { id: 'new2' }].concat(G.rows);
          T.restorePlace(p);
          return Math.round(sc.scrollTop / 66);
        """)
        self.assertEqual(302, got)

    def test_at_the_current_time_it_comes_back_to_the_current_time(self):
        got = run(PLACE + """
          var now = Date.now(), at = function (t) { return Math.max(0, Math.round((t - mid) / 60000 * 6.5 - 30 * 6.5)); };
          sc.scrollLeft = at(now);
          T.keepPlace();
          var p = G.place; p.atNow = true; sc.scrollLeft = 0;
          T.restorePlace(p);
          return [Math.abs(sc.scrollLeft - at(Date.now())) <= 1];
        """)
        self.assertEqual([True], got)

    def test_the_channel_watched_is_in_view(self):
        got = run(PLACE + """
          sc.scrollTop = 10 * 66;
          G.lastId = 'c250';   // channel up from the one clicked ended here
          T.keepPlace();
          T.restorePlace(G.place);
          var first = sc.scrollTop / 66, shown = (900 - 38) / 66;
          var inView = 250 >= first && 251 <= first + shown;
          G.lastId = 'c12'; sc.scrollTop = 10 * 66; T.keepPlace(); T.restorePlace(G.place);
          return [inView, sc.scrollTop];
        """)
        # off screen: scrolled to it; already in view: the rows stay
        self.assertEqual([True, 660], got)

    def test_it_is_kept_when_the_guide_is_left(self):
        self.assertIn("keepPlace();\n    G.active = false;", JS)
        self.assertIn("applyView(place);", JS)


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
