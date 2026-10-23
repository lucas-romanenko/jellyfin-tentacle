"""Every way onto a title's details page gets Tentacle's details page.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The styled details page (Inject/tentacle-details.js) used to open only when a
card was clicked inside Jellyfin Web. Opening #/details?id=... any other way
(Tentacle's Play / Open in Jellyfin links, a bookmark, a reload, Back/Forward,
the redirect after signing in, Tentacle's own search/home/favourites rows that
set the address) showed Jellyfin's stock page. Now the route itself opens it,
for the types it draws; Live TV channels, music and the rest keep Jellyfin's
page. Runs the real script under node.
"""
import json
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, node

SETUP = ESC_DOM + r"""
var _classes = {};
document.documentElement = { classList: {
  add: function (c) { _classes[c] = true; }, remove: function (c) { delete _classes[c]; },
  contains: function (c) { return !!_classes[c]; } } };
var _listeners = {};
var _pushes = [], _replaced = [], _backs = 0;
var location = { hash: '__HASH__', pathname: '/web/', search: '',
  replace: function (u) { _replaced.push(u); location.hash = u.slice(u.indexOf('#')); } };
var history = { state: null, pushState: function (s) { _pushes.push(s); },
  replaceState: function () {}, back: function () { _backs++; } };
var TYPES = __TYPES__;
var _fetched = [];
var window = { location: location, history: history,
  addEventListener: function (n, f) { (_listeners[n] = _listeners[n] || []).push(f); },
  removeEventListener: function (n, f) { _listeners[n] = (_listeners[n] || []).filter(function (g) { return g !== f; }); },
  ApiClient: { serverAddress: function () { return 'http://jf'; }, accessToken: function () { return 't'; },
    getCurrentUserId: function () { return 'u'; }, serverId: function () { return 's'; },
    getItem: function (u, id) { _fetched.push(id);
      return TYPES[id] ? Promise.resolve({ Id: id, Type: TYPES[id], Name: id }) : Promise.reject(new Error('404')); } } };
document.addEventListener = function (n, f) { (_listeners[n] = _listeners[n] || []).push(f); };
var ApiClient = window.ApiClient; var navigator = { userAgent: 'x' };
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
var sessionStorage = localStorage;
var MutationObserver = function () { this.observe = function () {}; this.disconnect = function () {}; };
function fire(name, ev) { (_listeners[name] || []).forEach(function (f) { f(ev || {}); }); }
function settle() { return new Promise(function (r) { setTimeout(r, 30); }); }
"""

TAIL = r"""
var _shown = [];
Details.showDetails = function (id, type, opts) {
  _shown.push({ id: id, type: type, route: !!(opts && opts.route) });
  this.isVisible = true; this._route = !!(opts && opts.route); this.currentItem = { Id: id, Type: type };
};
var _hidden = [];
var realHide = Details.hide;
Details.hide = function (skip) { _hidden.push(!!skip); return realHide.call(this, skip); };
function state() {
  return { shown: _shown, hidden: _hidden, visible: Details.isVisible, fetched: _fetched,
           prehide: document.documentElement.classList.contains('tentacle-details-route'),
           pushes: _pushes.length, backs: _backs, replaced: _replaced, hash: location.hash };
}
"""


def run(hash_, steps, types=None):
    types = types or {"m1": "Movie", "s1": "Series", "c1": "TvChannel", "m2": "Movie"}
    script = (SETUP.replace("__HASH__", hash_).replace("__TYPES__", json.dumps(types))
              + (INJECT / "tentacle-details.js").read_text(encoding="utf-8") + TAIL
              + "(async function () {\n" + steps + "\nprocess.stdout.write(JSON.stringify(state()));\nprocess.exit(0);\n})();")
    return node(script)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DetailsRoute(unittest.TestCase):
    def test_a_deep_link_opens_the_styled_page(self):
        s = run("#/details?id=m1&serverId=x", "await settle();")
        self.assertEqual(s["shown"], [{"id": "m1", "type": "Movie", "route": True}])
        self.assertEqual(s["pushes"], 0, "the route is already in history; nothing is pushed on top")

    def test_the_stock_page_is_hidden_from_the_first_moment(self):
        # Hidden before the item is even fetched (no flash of Jellyfin's layout).
        s = run("#/details?id=m1", "")
        self.assertTrue(s["prehide"])

    def test_the_bang_form_and_id_not_first(self):
        s = run("#!/details?serverId=x&id=m1", "await settle();")
        self.assertEqual([x["id"] for x in s["shown"]], ["m1"])

    def test_a_channel_keeps_jellyfins_page(self):
        s = run("#/details?id=c1", "await settle();")
        self.assertEqual(s["shown"], [])
        self.assertFalse(s["prehide"], "a page the overlay doesn't draw must be shown again")

    def test_an_item_that_fails_to_load_keeps_jellyfins_page(self):
        s = run("#/details?id=zz", "await settle();")
        self.assertEqual(s["shown"], [])
        self.assertFalse(s["prehide"])

    def test_signing_in_then_landing_on_the_page(self):
        s = run("#/login?url=%2Fdetails%3Fid%3Dm1", """
await settle();
location.hash = '#/details?id=m1'; fire('hashchange'); fire('viewshow');
await settle();""")
        self.assertEqual([x["id"] for x in s["shown"]], ["m1"], "shown once, not once per event")

    def test_leaving_the_route_closes_it_without_going_back_again(self):
        s = run("#/details?id=m1", """
await settle();
location.hash = '#/home'; fire('popstate', { state: null }); fire('hashchange');
await settle();""")
        self.assertFalse(s["visible"])
        self.assertEqual(s["backs"], 0, "the browser already left the page")
        self.assertFalse(s["prehide"])

    def test_back_and_forward_between_two_titles(self):
        s = run("#/details?id=m1", """
await settle();
location.hash = '#/details?id=s1'; fire('hashchange');
await settle();
location.hash = '#/details?id=m1'; fire('popstate', { state: null }); fire('hashchange');
await settle();""")
        self.assertEqual([x["id"] for x in s["shown"]], ["m1", "s1", "m1"])
        self.assertTrue(s["visible"])

    def test_closing_a_page_opened_from_outside_goes_home(self):
        # Nothing of Jellyfin's before it in this tab: Back would leave Jellyfin.
        s = run("#/details?id=m1", "await settle(); Details.hide();")
        self.assertEqual(s["backs"], 0)
        self.assertEqual(s["replaced"], ["/web/#/home"])

    def test_closing_a_page_reached_inside_jellyfin_goes_back(self):
        s = run("#/home", """
await settle();
location.hash = '#/details?id=m1'; fire('hashchange');
await settle(); Details.hide();""")
        self.assertEqual(s["backs"], 1)
        self.assertEqual(s["replaced"], [])

    def test_playing_then_coming_back_shows_it_again(self):
        s = run("#/details?id=m1", """
await settle();
Details.hide(true); location.hash = '#/video'; fire('hashchange');
await settle();
location.hash = '#/details?id=m1'; fire('popstate', { state: null }); fire('hashchange');
await settle();""")
        self.assertEqual([x["id"] for x in s["shown"]], ["m1", "m1"])
        self.assertTrue(s["visible"])

    def test_a_play_link_opens_the_player_not_this_page(self):
        # tentacle-play.js plays it; the page waits (Jellyfin's stays hidden).
        s = run("#/details?id=m1&serverId=x&tentaclePlay=1", """
await settle();
var waiting = true; window.TentaclePlay = { pending: function () { return waiting; } };
location.hash = '#/details?id=m1&serverId=x'; fire('hashchange'); fire('viewshow');
await settle();
waiting = false; fire('tentacle-play-done', { detail: { started: true } });
await settle();
fire('viewshow');   // Jellyfin's page again, while the player opens
await settle();""")
        self.assertEqual(s["shown"], [])
        self.assertTrue(s["prehide"])

    def test_a_play_link_with_nothing_to_play_shows_the_page(self):
        s = run("#/details?id=s1&tentaclePlay=1", """
await settle();
location.hash = '#/details?id=s1'; fire('hashchange');
fire('tentacle-play-done', { detail: { started: false } });
await settle();""")
        self.assertEqual([x["id"] for x in s["shown"]], ["s1"])

    def test_forward_to_a_page_opened_from_a_card(self):
        # A card click opens it over another page with a history entry of its
        # own; Back closes it, Forward brings that title back.
        s = run("#/search?query=x", """
await settle();
fire('popstate', { state: { moonfinDetails: true, id: 'm2', type: 'Movie' } });
await settle();""")
        self.assertEqual(s["shown"], [{"id": "m2", "type": "Movie", "route": False}])
        self.assertEqual(s["pushes"], 0)

    def test_the_card_click_entry_records_the_title_for_forward(self):
        script = (SETUP.replace("__HASH__", "#/search?query=x").replace("__TYPES__", "{}")
                  + (INJECT / "tentacle-details.js").read_text(encoding="utf-8") + r"""
Details.fetchItem = function () { return new Promise(function () {}); };
var c = document.createElement('div'); c.parentElement = document.body;
c.querySelector = function () { return document.createElement('div'); };
Details.container = c;
Details.showDetails('m2', 'Movie');
process.stdout.write(JSON.stringify(_pushes));""")
        self.assertEqual(node(script), [{"moonfinDetails": True, "id": "m2", "type": "Movie"}])


if __name__ == "__main__":
    unittest.main()
