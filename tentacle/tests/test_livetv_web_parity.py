"""Jellyfin Web's Live TV, like the TV app (TV v1.34.0): the programme menu,
dates only as far as real guide data, recent channels in the player,
multi-view's picks, and the menus on Recordings and Home
(tentacle-plugin/Inject/tentacle-livetv.js, tentacle-recordings.js,
tentacle-home.js).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, Page, node

JS = (INJECT / "tentacle-livetv.js").read_text(encoding="utf-8")
CSS = (INJECT / "tentacle-livetv.css").read_text(encoding="utf-8")
REC = (INJECT / "tentacle-recordings.js").read_text(encoding="utf-8")
HOME = (INJECT / "tentacle-home.js").read_text(encoding="utf-8")

HARNESS = ESC_DOM + r"""
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
process.stdout.write(JSON.stringify(out), function () { process.exit(0); });
"""


def run(body: str):
    return node(HARNESS % (JS, body))


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DatesOnlyAsFarAsRealData(unittest.TestCase):
    """A day is offered while it has a quarter of the busiest day before it."""

    def days(self, counts):
        return run("return T.daysWithData(%s);" % json.dumps(counts))

    def test_a_stray_listing_days_ahead_adds_no_days(self):
        # Jellyfin on 25 Oct: 1,768 / 4,946 / 0 / ... / 1 on 1 Nov → Today and Tomorrow
        self.assertEqual(2, self.days([1768, 4946, 0, 0, 0, 0, 0, 1]))

    def test_thin_day_ends_the_strip(self):
        self.assertEqual(1, self.days([400, 99]))
        self.assertEqual(2, self.days([400, 100, 30]))

    def test_today_is_always_there(self):
        self.assertEqual(1, self.days([0]))
        self.assertEqual(1, self.days([0, 0]))

    def test_a_full_week(self):
        self.assertEqual(7, self.days([900, 1000, 1000, 950, 980, 1000, 990]))


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class ProgrammeMenu(unittest.TestCase):
    """The TV's guide menu: Watch now / Tune to channel, Record this / Record
    every episode (or their cancels) for people who manage recordings while it
    hasn't ended, then Details."""

    def choices(self, s, e, info, manage=True):
        return run("""var now = 1000000; return T.programmeChoices({ s: %d, e: %d }, %s, %s, now);"""
                   % (s, e, json.dumps(info), "true" if manage else "false"))

    def test_on_now(self):
        self.assertEqual(["watch", "record", "recordSeries", "details"], self.choices(900000, 1100000, {}))

    def test_later_and_booked(self):
        self.assertEqual(["tune", "cancel", "cancelSeries", "details"],
                         self.choices(2000000, 3000000, {"TimerId": "t", "SeriesTimerId": "s"}))

    def test_a_film_has_no_series(self):
        self.assertEqual(["tune", "record", "details"], self.choices(2000000, 3000000, {"IsMovie": True}))

    def test_over_or_not_allowed(self):
        self.assertEqual(["tune", "details"], self.choices(100, 200, {}))
        self.assertEqual(["watch", "details"], self.choices(900000, 1100000, {}, manage=False))

    def test_no_jellyfin_programme_page_on_click(self):
        # Lucas (26 Oct): a click opened Jellyfin's page with a blank picture and a Record button
        self.assertNotIn("location.hash = '#/details?id=' + cell.dataset.id", JS)
        self.assertIn("programmeMenu(channelId, cell.dataset.id", JS)

    def test_unstar_asks_first(self):
        body = JS[JS.index("function starClicked("):JS.index("// ── The keyboard in the guide")]
        self.assertIn("'Remove ' + c.name + ' from your Favorites?'", body)
        self.assertLess(body.index("if (!c.fav) { toggleFav(id)"), body.index("confirmMenu("))


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class RecentChannels(unittest.TestCase):
    def merge(self, server, tunes, playing, max_=10):
        return run("""return T.mergeRecents(%s, %s, %s, %d).map(function (c) { return c.Id; });"""
                   % (json.dumps(server), json.dumps(tunes), json.dumps(playing), max_))

    @staticmethod
    def ch(id_, iso):
        return {"Id": id_, "UserData": {"LastPlayedDate": iso}}

    def test_newest_first_without_the_one_playing(self):
        server = [self.ch("a", "2026-10-26T18:00:00Z"), self.ch("b", "2026-10-26T17:00:00Z"),
                  self.ch("c", "2026-10-26T16:00:00Z")]
        self.assertEqual(["b", "c"], self.merge(server, [], "a"))

    def test_this_browsers_tunes_count_at_once(self):
        server = [self.ch("a", "2026-10-26T18:00:00Z"), self.ch("b", "2026-10-26T17:00:00Z")]
        later = 1_900_000_000_000      # after the server's dates
        out = self.merge(server, [{"id": "B", "at": later}, {"id": "z", "at": later - 1}], "q")
        self.assertEqual(["b", "z", "a"], [x.lower() for x in out])

    def test_ids_with_and_without_dashes_are_one_channel(self):
        server = [self.ch("0a1b-2c", "2026-10-26T18:00:00Z")]
        self.assertEqual([], self.merge(server, [{"id": "0a1b2c", "at": 1}], "0A1B2C"))

    def test_at_most_ten(self):
        server = [self.ch("c%d" % i, "2026-10-26T%02d:00:00Z" % i) for i in range(15)]
        self.assertEqual(10, len(self.merge(server, [], "")))

    def test_from_jellyfins_last_played_date(self):
        self.assertIn("SortBy: 'DatePlayed'", JS)
        self.assertIn("Your favorite channels", JS)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class MultiViewPicks(unittest.TestCase):
    def test_picked_channels_are_numbered(self):
        html = run("""
          T.MV.picking = true; T.MV.picks = ['b', 'a'];
          return [T.chanCell({ id: 'a', name: 'A', number: '1', logo: '', fav: false }),
                  T.chanCell({ id: 'c', name: 'C', number: '3', logo: '', fav: false })];
        """)
        self.assertIn('class="tg-pickno">2<', html[0])
        self.assertIn("tg-picked", html[0])
        self.assertNotIn("tg-pickno", html[1])

    def test_uses_the_tvs_session_api(self):
        for path in ("'TentacleDiscover/MultiView'", "'/' + s.id + '/Keepalive'", "'/Web'"):
            self.assertIn(path, JS)
        self.assertIn("navigator.wakeLock.request('screen')", JS)
        # leaving the page gives the connections back at once
        self.assertIn("keepalive: true", JS)

    def test_a_channel_fills_the_screen_inside_the_grid(self):
        # Lucas (26 Oct): full screen from a tile in Jellyfin's player loaded, showed a second,
        # loaded again (the provider cuts each of 4 open channels every 10-15 s; the grid holds
        # frames through it, a player of the channel stalls). The tile is zoomed instead.
        body = JS[JS.index("function zoomTile("):JS.index("function unzoom(")]
        self.assertIn("'scale(' + (W / t.width)", body)
        self.assertIn("hear(i)", body)
        dbl = JS[JS.index("el.addEventListener('dblclick'"):JS.index("onMenuGesture(el, '.mv-tile'")]
        self.assertIn("zoomTile(", dbl)
        self.assertNotIn("fullScreenTile(", dbl)

    def test_a_click_marks_tiles_in_place(self):
        # rebuilding the tiles on the first click swallowed the double click
        body = JS[JS.index("function hear(i)"):JS.index("function onGridClick(")]
        self.assertIn("markTiles()", body)
        self.assertNotIn("renderTiles()", body)

    def test_back_from_the_player_brings_the_grid_back(self):
        body = JS[JS.index("function watchPlayerPage("):JS.index("window.addEventListener('popstate', watchPlayerPage)")]
        self.assertIn("MV.sawPlayer = true", body)
        self.assertIn("openGrid()", body)

    def test_only_the_browsers_pagehide_ends_it(self):
        # Jellyfin Web's own view 'pagehide' (opening the player) ended the session (26 Oct)
        body = JS[JS.index("window.addEventListener('pagehide'"):JS.index("function onGridKey(")]
        self.assertIn("if (e.target !== window && e.target !== document) return;", body)

    def test_tentacles_own_pictures_are_no_stray_players(self):
        # back from Jellyfin's player the grid went black: the stray-player guard took
        # its MSE <video> (blob:) for a live player left running
        body = JS[JS.index("function strayVideos("):JS.index("function checkStrayPlayer(")]
        self.assertIn("v.__livePicture", body)

    def test_no_header_bar_over_the_grid(self):
        self.assertIn("body.tentacle-multiview-active .moonfin-navbar", CSS)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class FirstPageLoad(unittest.TestCase):
    """A first page load opens the guide before Jellyfin web has signed in."""

    def test_no_empty_user_id_in_a_post(self):
        sent = run("""
          var bodies = [];
          ApiClient.getCurrentUserId = function () { return ''; };
          ApiClient.ajax = function (o) { bodies.push(JSON.parse(o.data)); return Promise.resolve({}); };
          T.G.userId = '';
          window.TentacleLiveTV.internals.apiPost('LiveTv/Programs', { UserId: '', Limit: 1 });
          ApiClient.getCurrentUserId = function () { return 'u9'; };
          window.TentacleLiveTV.internals.apiPost('LiveTv/Programs', { UserId: '', Limit: 1 });
          return bodies;
        """)
        self.assertNotIn("UserId", sent[0])          # none: Jellyfin takes the caller
        self.assertEqual("u9", sent[1]["UserId"])    # learnt once signed in

    def test_previews_asked_once_signed_in(self):
        self.assertIn("if (PV.poll && !PV.asked) pollPreview();", JS)


class PreviewAndPlayer(unittest.TestCase):
    def test_preview_waits_and_asks_tentacle(self):
        self.assertIn("PREVIEW_REST_MS = 1500", JS)
        self.assertIn("'TentacleDiscover/LivePreview'", JS)
        self.assertIn("q.previewing = true", JS)

    def test_a_clicked_preview_keeps_its_stream_for_the_player(self):
        body = JS[JS.index("function watch(channelId)"):JS.index("// ── Dates only as far")]
        self.assertIn("stopPreview(true)", body)

    def test_player_buttons_only_for_a_channel(self):
        body = JS[JS.index("function osdButtons()"):JS.index("// ── The guide over the player")]
        self.assertIn("PL.item.Type === 'TvChannel'", body)

    def test_the_picture_moves_into_the_box(self):
        self.assertIn("body.tentacle-guide-over-player .videoPlayerContainer", CSS)


class RecordingsAndHome(unittest.TestCase):
    def test_a_recording_plays_on_click(self):
        self.assertIn("play(item, false)", REC)
        self.assertIn("window.TentacleLiveTV.playChannel(item.ChannelId)", REC)   # recording now: live

    def test_delete_only_when_jellyfin_allows_and_asked(self):
        body = REC[REC.index("function recordingMenu("):]
        self.assertIn("item.CanDelete ?", body)
        self.assertIn("M.confirm(", body)
        self.assertIn("Fields: 'Overview,DateCreated,PremiereDate,CanDelete'", REC)

    def test_continue_watching_menu(self):
        body = HOME[HOME.index("function cardMenu("):HOME.index("var cardMenuBound")]
        for label in ("Remove from Continue Watching", "Play from start", "Mark watched", "Details"):
            self.assertIn(label, body)
        self.assertIn("PlaybackPositionTicks: 0", body)
        self.assertIn(":not(.mh-card-onnow)", HOME)


if __name__ == "__main__":
    unittest.main()
