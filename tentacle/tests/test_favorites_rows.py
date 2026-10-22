"""The Favorites tab as home-style rows (tentacle-plugin/Inject/tentacle-favorites.js).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Runs the real script under node with a document stub and a fake Jellyfin
ApiClient, and checks what goes in which row and in what order (Movies, Shows,
Live TV, YouTube, Recordings, then the other kinds), that YouTube is told by
Tentacle's own tags (a bare "youtube" tag is also a TMDB keyword on ordinary
films), that names come out exactly, that long lists are read to the end, that
a favourite changed anywhere makes the page read its lists again, and that the
heart reaches the server with the right arguments.
"""
import json
import time
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, NAMES, Page, node

SCRIPT = (INJECT / "tentacle-favorites.js").read_text(encoding="utf-8")

# A fake ApiClient: answers from FIXTURE, records every request and favourite call.
HARNESS = ESC_DOM + r"""
var FIXTURE = %(fixture)s;
var requests = [], favCalls = [];
function page(list, url) {
  var q = new URL('http://x/' + url).searchParams;
  var start = parseInt(q.get('StartIndex') || '0', 10);
  var limit = Math.min(parseInt(q.get('Limit') || '100000', 10), FIXTURE.serverMaxLimit || 100000);
  return { Items: list.slice(start, start + limit), TotalRecordCount: list.length, StartIndex: start };
}
var ApiClient = {
  getCurrentUserId: function () { return 'u1'; },
  getUrl: function (p, q) { return p + (q ? (p.indexOf('?') < 0 ? '?' : '&') + new URLSearchParams(q) : ''); },
  getJSON: function (url) {
    requests.push(url);
    if (url.indexOf('LiveTv/Channels') === 0) return Promise.resolve({ Items: FIXTURE.channels || [] });
    if (url.indexOf('LiveTv/Recordings/Folders') === 0) return Promise.resolve({ Items: (FIXTURE.recordingFolders || []).map(function (id) { return { Id: id }; }) });
    if (url.indexOf('ParentId=') >= 0) {
      var parent = new URL('http://x/' + url).searchParams.get('ParentId');
      return Promise.resolve(page((FIXTURE.recordings || {})[parent] || [], url));
    }
    return Promise.resolve(page(FIXTURE.items || [], url));
  },
  updateFavoriteStatus: function (u, id, f) { favCalls.push([u, id, f]); return Promise.resolve({}); },
};
// not on the Favorites route: the tests drive loading themselves
var window = { ApiClient: ApiClient, addEventListener: function () {}, location: { hash: '#/home' } };
var location = window.location;
%(script)s
var I = window.TentacleFavorites.internals;
var NOW = Date.parse('2026-10-22T18:00:00Z');
function plan() {
  return I.planRows(I.FAV.data, NOW).map(function (p) {
    return { row: p.def.key, title: p.def.title, ids: p.entries.map(function (e) { return e.item.Id; }), html: I.rowHtml(p, NOW) };
  });
}
function load() {
  I.FAV.active = true;
  I.FAV.data = { items: null, channels: null, recordingIds: null };
  I.FAV.load.busy = false;
  I.FAV.load.gen = 0;
  I.FAV.container = null;   // no DOM: planRows/rowHtml are what is checked
  return new Promise(function (res) { I.scheduleLoad(0); setTimeout(res, 50); });
}
%(body)s
"""


def run(fixture: dict, body: str):
    return node(HARNESS % {"fixture": json.dumps(fixture), "script": SCRIPT, "body": body})


def movie(i, name=None, tags=(), **kw):
    return {"Id": f"m{i}", "Name": name or f"Movie {i}", "Type": "Movie", "Tags": list(tags), "ImageTags": {"Primary": "t"}, **kw}


def channel(i, name, number, tags=("tentacle-livetv-all",), prog=None):
    c = {"Id": f"c{i}", "Name": name, "ChannelNumber": str(number), "Type": "TvChannel", "Tags": list(tags),
         "ImageTags": {"Primary": "x"}}
    if prog:
        c["CurrentProgram"] = {"Id": f"p{i}", "Name": prog,
                               "StartDate": "2026-10-22T17:30:00Z", "EndDate": "2026-10-22T18:30:00Z"}
    return c


EVERYTHING = {
    "items": [
        movie(1, "Jay Kelly"),
        movie(2, "Bo Burnham: Inside", tags=["youtube", "Comedy"]),       # TMDB keyword, a film
        movie(3, "Leafs highlights", tags=["youtube", "yt:toronto-maple-leafs"]),  # Tentacle's YouTube
        {"Id": "s1", "Name": "Love on the Spectrum", "Type": "Series", "ImageTags": {}},
        {"Id": "e1", "Name": "Pilot", "Type": "Episode", "SeriesName": "Slow Horses",
         "ParentIndexNumber": 1, "IndexNumber": 1, "ImageTags": {}},
        {"Id": "r1", "Name": "Hockey Night", "Type": "Episode", "ImageTags": {}},     # a recording
        {"Id": "b1", "Name": "Alien Collection", "Type": "BoxSet", "ImageTags": {}},
        {"Id": "a1", "Name": "Abbey Road", "Type": "MusicAlbum", "AlbumArtist": "The Beatles", "ImageTags": {}},
        {"Id": "f1", "Name": "Some folder", "Type": "Folder", "ImageTags": {}},        # no row for it
    ],
    "channels": [
        channel(3, "TSN 2", 288, prog="NFL GameDay"),
        channel(1, "PBS Kids", 10, prog="Odd Squad"),
        channel(2, "TraderTV Live", 1012, tags=["tentacle-youtube-2"]),
    ],
    "recordingFolders": ["rec"],
    "recordings": {"rec": [{"Id": "r1", "Type": "Episode"}]},
}


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class FavoritesRows(unittest.TestCase):
    def test_rows_come_in_order_and_hold_the_right_things(self):
        rows = run(EVERYTHING, "load().then(function () { console.log(JSON.stringify(plan())); });")
        self.assertEqual(["movies", "shows", "livetv", "youtube", "recordings", "episodes", "collections", "albums"],
                         [r["row"] for r in rows])
        by = {r["row"]: r["ids"] for r in rows}
        self.assertEqual(["m1", "m2"], sorted(by["movies"]))
        self.assertEqual(["c1", "c3"], by["livetv"], "Live TV in lineup order, YouTube's live channel elsewhere")
        self.assertEqual(["c2", "m3"], by["youtube"], "YouTube: its live channel first, then videos")
        self.assertEqual(["r1"], by["recordings"])
        self.assertEqual(["e1"], by["episodes"], "a recording is not also an episode")

    def test_a_youtube_keyword_alone_does_not_make_a_film_youtube(self):
        rows = run(EVERYTHING, "load().then(function () { console.log(JSON.stringify(plan())); });")
        by = {r["row"]: r["ids"] for r in rows}
        self.assertIn("m2", by["movies"])
        self.assertNotIn("m2", by["youtube"])

    def test_empty_rows_are_not_shown(self):
        rows = run({"items": [movie(1)], "channels": []},
                   "load().then(function () { console.log(JSON.stringify(plan())); });")
        self.assertEqual(["movies"], [r["row"] for r in rows])

    def test_no_live_tv_access_still_shows_the_rest(self):
        fixture = dict(EVERYTHING)
        body = r"""
        var real = ApiClient.getJSON;
        ApiClient.getJSON = function (url) {
          if (url.indexOf('LiveTv/') === 0) return Promise.reject(new Error('403'));
          return real(url);
        };
        load().then(function () { console.log(JSON.stringify(plan())); });"""
        rows = run(fixture, body)
        names = [r["row"] for r in rows]
        self.assertNotIn("livetv", names)
        self.assertNotIn("recordings", names)
        self.assertEqual("movies", names[0])

    def test_channel_card_shows_number_name_whats_on_and_progress(self):
        rows = run(EVERYTHING, "load().then(function () { console.log(JSON.stringify(plan())); });")
        html = next(r["html"] for r in rows if r["row"] == "livetv")
        page = Page(html)
        self.assertIn("PBS Kids", page.text)
        self.assertIn("Odd Squad", page.text)
        self.assertIn("30m left", page.text)
        self.assertIn("10", [t.strip() for t in page.texts])
        widths = [a["style"] for t, a in page.elements if "mh-card-progress-bar" in a.get("class", "")]
        self.assertEqual(["width:50%", "width:50%"], widths)
        self.assertEqual(["c1", "c3"], page.attrs("data-channel-id"))

    def test_a_channel_with_nothing_on_says_so_without_a_progress_bar(self):
        rows = run({"channels": [channel(1, "Quiet", 5)], "items": []},
                   "load().then(function () { console.log(JSON.stringify(plan())); });")
        page = Page(rows[0]["html"])
        self.assertIn("Nothing in the guide now", page.text)
        self.assertFalse([a for t, a in page.elements if "mh-card-progress" in a.get("class", "")])

    def test_names_show_exactly_in_card_and_tooltip(self):
        items = [movie(i, n) for i, n in enumerate(NAMES)]
        chans = [channel(i, n, i + 1, prog=n) for i, n in enumerate(NAMES)]
        rows = run({"items": items, "channels": chans},
                   "load().then(function () { console.log(JSON.stringify(plan())); });")
        for r in rows:
            page = Page(r["html"])
            titles = [a["title"] for t, a in page.elements if "tfav-card-name" in a.get("class", "")]
            self.assertEqual(sorted(NAMES), sorted(titles), r["row"])
            for n in NAMES:
                self.assertIn(n, page.text)

    def test_every_favourite_past_the_first_page_is_shown(self):
        items = [movie(i) for i in range(1234)]
        out = run({"items": items, "serverMaxLimit": 100},
                  "load().then(function () { setTimeout(function () { var p = plan();"
                  " console.log(JSON.stringify({ n: p[0].ids.length, uniq: new Set(p[0].ids).size,"
                  " reqs: requests.filter(function (u) { return u.indexOf('Users/') === 0; }).length })); }, 200); });")
        self.assertEqual(1234, out["n"])
        self.assertEqual(1234, out["uniq"])
        self.assertLess(out["reqs"], 20)

    def test_a_small_list_is_three_requests(self):
        out = run({"items": [movie(1)], "channels": []},
                  "load().then(function () { console.log(JSON.stringify(requests)); });")
        self.assertEqual(3, len(out), out)   # favourites, channels, recording folders

    def test_a_favourite_changed_anywhere_reads_the_lists_again(self):
        body = r"""
        load().then(function () {
          var before = requests.length;
          I.onMessage({ MessageType: 'UserDataChanged', Data: { UserId: 'u-1', UserDataList: [{ ItemId: 'm9', IsFavorite: true }] } });
          setTimeout(function () {
            var mine = requests.length - before;
            before = requests.length;
            I.onMessage({ MessageType: 'UserDataChanged', Data: { UserId: 'someone-else', UserDataList: [{ ItemId: 'm9', IsFavorite: true }] } });
            I.onMessage({ MessageType: 'UserDataChanged', Data: { UserId: 'u1', UserDataList: [{ ItemId: 'm9', PlaybackPositionTicks: 5 }] } });
            setTimeout(function () {
              console.log(JSON.stringify({ mine: mine, others: requests.length - before }));
            }, 400);
          }, 400);
        });"""
        out = run({"items": [movie(1)]}, body)
        self.assertGreaterEqual(out["mine"], 1, "a favourite of this person changed: read again")
        self.assertEqual(0, out["others"], "someone else's favourite or a play position: nothing to do")

    def test_a_library_change_reads_the_lists_again(self):
        body = r"""
        load().then(function () {
          var before = requests.length;
          I.onMessage({ MessageType: 'LibraryChanged', Data: null });
          setTimeout(function () { console.log(JSON.stringify(requests.length - before)); }, 1800);
        });"""
        self.assertGreaterEqual(run({"items": [movie(1)]}, body), 1)

    def test_the_heart_unfavourites_with_user_item_false_and_drops_it_at_once(self):
        body = r"""
        load().then(function () {
          I.unfavorite('c1');
          var p = plan();
          console.log(JSON.stringify({ calls: favCalls, livetv: (p.filter(function (r) { return r.row === 'livetv'; })[0] || {}).ids }));
        });"""
        out = run(EVERYTHING, body)
        self.assertEqual([["u1", "c1", False]], out["calls"])
        self.assertEqual(["c3"], out["livetv"])


class FavoritesRowsSource(unittest.TestCase):
    def test_kids_and_per_person_limits_are_jellyfins(self):
        """Every list is the person's own query (their token, their user id):
        Jellyfin applies parental limits and BlockedTags. No admin key, no
        server-side list that could leak another person's channels."""
        self.assertNotIn("api_key", SCRIPT)
        self.assertIn("'Users/' + uid + '/Items?Filters=IsFavorite", SCRIPT)
        self.assertIn("LiveTv/Channels?UserId=' + uid + '&IsFavorite=true", SCRIPT)

    def test_rows_use_homes_markup(self):
        for cls in ("mh-row", "mh-row-items", "mh-card", "mh-row-title"):
            self.assertIn(cls, SCRIPT)


if __name__ == "__main__":
    unittest.main()
