"""Live updates in the browser: the dashboard's live.js and Jellyfin Web's
Tentacle scripts react to "this changed" at once.

- Dashboard (static/js/live.js): a note on the live channel re-reads what
  the open page shows for that topic: the person's Activity badge after an
  approval (Lucas, 2026-10-22: it showed "1" only after a click), the
  admins' Requests badge after someone asks, the open Discover detail.
- Jellyfin Web (tentacle-notifications.js): the plugin's message on
  Jellyfin's socket (RefreshProgress, ItemId "tentacle") reads the
  notifications at once and tells the Activity overlay and navbar badge.

Runs the real scripts under node with stubs. Skipped without node.

Run from the tentacle/ directory:  python -m unittest tests.test_live_updates_js
"""
import json
import unittest

from dashboard_js import HAVE_NODE, JS, node
from inject_js import ESC_DOM, INJECT


DASH_STUBS = r"""
var calls = [];
function rec(name) { return function () { calls.push(name); }; }
var timers = [];
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
function runTimers() { while (timers.length) timers.shift()(); }
var document = {
  hidden: false, activeElement: null,
  addEventListener: function () {},
  querySelector: function () { return null; },
  getElementById: function (id) {
    if (id === 'discover-tab-activity') return { style: { display: state.activityTab ? '' : 'none' } };
    if (id === 'modal-media-detail') return { style: { display: state.detailOpen ? 'flex' : 'none' } };
    return null;
  },
};
var state = { currentUser: { is_admin: false }, currentPage: 'library', sourcesTab: 'providers' };
var famLoadBadge = rec('famLoadBadge'), loadRequests = rec('loadRequests'), loadActivity = rec('loadActivity');
var famLoadMyRequests = rec('famLoadMyRequests'), famRefreshPeople = rec('famRefreshPeople');
var loadArrProblems = rec('loadArrProblems'), loadProviderProblems = rec('loadProviderProblems');
var loadHealthAlerts = rec('loadHealthAlerts'), loadHealthPage = rec('loadHealthPage');
var loadHealthDownloads = rec('loadHealthDownloads'), famRefreshBanner = rec('famRefreshBanner');
var loadJellyfinPage = rec('loadJellyfinPage'), loadMyChannels = rec('loadMyChannels');
var nfpLoadCounts = rec('nfpLoadCounts'), _debouncedLibRefresh = rec('_debouncedLibRefresh');
var libRemoveCard = function (id) { calls.push('remove:' + id); };
var _discoverDetailArgs = null;
function showDiscoverDetail() { calls.push('detail:' + JSON.stringify(Array.prototype.slice.call(arguments))); }
function EventSource(url) { this.url = url; this.l = {}; EventSource.last = this; }
EventSource.prototype.addEventListener = function (n, f) { this.l[n] = f; };
EventSource.prototype.close = function () { this.closed = true; };
function emit(name, data) { EventSource.last.l[name]({ data: JSON.stringify(data) }); runTimers(); }
function take() { var c = calls; calls = []; return c; }
"""


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Dashboard(unittest.TestCase):
    def run_js(self, body):
        src = (JS / "live.js").read_text(encoding="utf-8")
        return node(DASH_STUBS + src + "\n(async function () {\n" + body + "\n})();")

    def test_an_approval_reaches_the_persons_open_discover(self):
        out = self.run_js(r"""
          state.currentPage = 'discover';
          liveStart();
          var r = { url: EventSource.last.url };
          emit('hello', { seq: 1 });
          r.hello = take();                                 // the first hello reads nothing again
          emit('change', { topic: 'requests', seq: 2 });
          emit('change', { topic: 'activity', seq: 3 });
          r.after = take();
          state.activityTab = false;
          emit('change', { topic: 'activity', seq: 4, data: { progress: true } });
          r.progressHidden = take();                        // bars nobody sees: nothing
          state.activityTab = true;
          emit('change', { topic: 'activity', seq: 5, data: { progress: true } });
          r.progressShown = take();
          console.log(JSON.stringify(r));
        """)
        self.assertEqual("/api/updates", out["url"])
        self.assertEqual([], out["hello"])
        self.assertEqual(["loadActivity", "famLoadMyRequests", "loadActivity"], out["after"])
        self.assertEqual([], out["progressHidden"])
        self.assertEqual(["loadActivity"], out["progressShown"])

    def test_admins_badge_and_requests_page(self):
        out = self.run_js(r"""
          state.currentUser.is_admin = true;
          liveStart(); emit('hello', {});
          take();
          emit('change', { topic: 'requests' });
          var r = { elsewhere: take() };
          state.currentPage = 'requests';
          emit('change', { topic: 'requests' });
          r.open = take();
          state.currentPage = 'people';
          emit('change', { topic: 'people' });
          r.people = take();
          emit('change', { topic: 'health' });
          r.health = take();
          console.log(JSON.stringify(r));
        """)
        self.assertEqual(["famLoadBadge"], out["elsewhere"])
        self.assertEqual(["loadRequests"], out["open"])
        self.assertEqual(["famRefreshPeople"], out["people"])
        self.assertEqual(["loadArrProblems", "loadProviderProblems", "loadHealthAlerts"], out["health"])

    def test_a_reconnect_catches_up_and_a_burst_is_one_read(self):
        out = self.run_js(r"""
          state.currentUser.is_admin = true;
          state.currentPage = 'library';
          liveStart(); emit('hello', {}); take();
          emit('hello', {});                                  // the line came back
          var r = { resync: take() };
          liveQueue('requests'); liveQueue('requests'); liveQueue('requests');
          runTimers();
          r.burst = take();
          emit('change', { topic: 'library', data: { event: 'movie_removed', tmdb_id: 7 } });
          r.removed = take();
          console.log(JSON.stringify(r));
        """)
        # Every topic's handler, each loader once.
        self.assertEqual(["famLoadBadge", "loadArrProblems", "loadProviderProblems", "loadHealthAlerts",
                          "nfpLoadCounts", "_debouncedLibRefresh"], out["resync"])
        self.assertEqual(["famLoadBadge"], out["burst"])
        self.assertEqual(["remove:7"], out["removed"])

    def test_the_open_discover_detail_follows_its_title(self):
        out = self.run_js(r"""
          state.currentPage = 'discover';
          liveStart(); emit('hello', {}); take();
          _discoverDetailArgs = [438631, 'movie', 'Dune', '2021', '/d.jpg', false, 0];
          state.detailOpen = true;
          emit('change', { topic: 'requests' });
          var r = { open: take() };
          state.detailOpen = false;
          emit('change', { topic: 'requests' });
          r.closed = take();
          console.log(JSON.stringify(r));
        """)
        self.assertIn('detail:[438631,"movie","Dune","2021","/d.jpg",false,0,true]', out["open"])
        self.assertEqual(["loadActivity", "famLoadMyRequests"], out["closed"])

    def test_its_own_save_doesnt_redraw_the_home_being_edited(self):
        out = self.run_js(r"""
          state.currentPage = 'jellyfin';
          liveStart(); emit('hello', {}); take();
          live.lastWrite = Date.now();
          emit('change', { topic: 'home' });
          var r = { own: take() };
          live.lastWrite = 0;
          emit('change', { topic: 'home' });
          r.other = take();
          console.log(JSON.stringify(r));
        """)
        self.assertEqual([], out["own"])
        self.assertEqual(["loadJellyfinPage"], out["other"])


WEB_STUBS = ESC_DOM + r"""
var listeners = {}, polled = [], relayed = [], timers = [];
var window = {
  dispatchEvent: function (e) { (listeners[e.type] || []).forEach(function (f) { f(e); }); },
  addEventListener: function (n, f) { (listeners[n] = listeners[n] || []).push(f); },
  location: { hash: '' },
};
function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; }
function setTimeout(fn) { timers.push(fn); return 1; }
function setInterval() { return 1; }
function requestAnimationFrame() {}
function BroadcastChannel() { this.postMessage = function (m) { relayed.push(m); }; }
var socketHandlers = [];
var socket = { addEventListener: function (n, f) { socketHandlers.push(f); } };
window.ApiClient = {
  _webSocket: socket,
  getCurrentUserId: function () { return 'u1'; },
  getUrl: function (p) { return p; },
  getJSON: function (u) { polled.push(u); return Promise.resolve({ notifications: [], notifications_enabled: true }); },
  ajax: function () { return Promise.resolve(); },
};
var changes = [];
window.addEventListener('tentacle-changed', function (e) { changes.push(e.detail.topics); });
function send(msg) { socketHandlers.forEach(function (f) { f({ target: socket, data: JSON.stringify(msg) }); }); }
"""


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class JellyfinWeb(unittest.TestCase):
    def test_the_plugins_message_reads_notifications_and_tells_the_overlays(self):
        src = (INJECT / "tentacle-notifications.js").read_text(encoding="utf-8")
        out = node(WEB_STUBS + src + r"""
          var r = { first: polled.length };
          send({ MessageType: 'RefreshProgress', Data: { ItemId: 'tentacle', Progress: '0', Tentacle: 'requests,activity' } });
          r.activity = { polled: polled.length, changes: changes.slice(), relayed: relayed.length };
          send({ MessageType: 'RefreshProgress', Data: { ItemId: 'tentacle', Progress: '0', Tentacle: 'notifications' } });
          r.notifications = { polled: polled.length, changes: changes.length };
          send({ MessageType: 'RefreshProgress', Data: { ItemId: '0a1b2c', Progress: '42.5' } });   // Jellyfin's own
          send({ MessageType: 'LibraryChanged', Data: {} });
          r.others = { polled: polled.length, changes: changes.length };
          console.log(JSON.stringify(r));
        """)
        self.assertEqual(1, out["first"])
        self.assertEqual({"polled": 1, "changes": [["requests", "activity"]], "relayed": 1}, out["activity"])
        self.assertEqual({"polled": 2, "changes": 2}, out["notifications"])
        self.assertEqual({"polled": 2, "changes": 2}, out["others"])

    def test_a_toast_about_someone_elses_download_says_whose(self):
        src = (INJECT / "tentacle-notifications.js").read_text(encoding="utf-8")
        start = src.index("  function toastLabel(")
        end = src.index("\n  }\n", start) + 4
        out = node(src[start:end] + r"""
          console.log(JSON.stringify([toastLabel({ kind: 'download', for: 'Sam' }), toastLabel({ kind: 'download' }),
                                      toastLabel({ kind: 'request' })]));
        """)
        self.assertEqual(["Ready to Watch · for Sam", "Ready to Watch", "Request"], out)


if __name__ == "__main__":
    unittest.main()
