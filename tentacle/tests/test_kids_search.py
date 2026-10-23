"""A kids profile can search in Jellyfin web.

Kids keep Search in their navbar (services/family.py turns off only Discover
and Activity). Jellyfin web's search page is Tentacle's (tentacle-search.js
hides Jellyfin's own results and asks /TentacleDiscover/Search), but
/api/discover/search is behind the router-wide kids guard (routers/discover.py
_not_on_kids_profiles: 403), and the plugin answers that 403 with "Tentacle
didn't accept this account. Open the Tentacle dashboard once to finish setup."
(DiscoverController.DescribeFailure). A kid typing "bluey" sees that sentence
and nothing else: Jellyfin's results, which their age limit already filters,
stay hidden.

Wanted:
* Tentacle answers a kid's search itself: no TMDB catalogue (Discover isn't
  theirs), and `kids: true` so the client knows why it is empty;
* Jellyfin web then shows Jellyfin's own search for that person (their
  parental limit applies there), also when they come back to the page;
* everyone else's search is unchanged.

The web part runs the real tentacle-search.js under node with a small DOM.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_kids_search.py
"""
import json
import logging
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from inject_js import HAVE_NODE, INJECT, node
from tmp_dirs import temp_dir

KID = "a" * 32
ADULT = "b" * 32


class KidsSearchOnTheServer(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.db.add(mdb.LiveChannel(provider_id=p.id, name="Bluey TV", stream_id="1",
                                    stream_url="http://192.0.2.10/live/1.ts", enabled=True, sort_order=1))
        self.kid = mdb.TentacleUser(jellyfin_user_id=KID, display_name="Kid", is_admin=False, kids_level="kids")
        self.adult = mdb.TentacleUser(jellyfin_user_id=ADULT, display_name="Ann", is_admin=False)
        self.db.add_all([self.kid, self.adult])
        self.db.commit()

    def _get(self, user, path):
        from routers import discover
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(discover.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        tmdb = mock.Mock()
        tmdb.search_multi_results.return_value = [
            {"tmdb_id": 1, "title": "Night Terror", "media_type": "movie", "year": "2020"}]
        # The kids guard calls get_user_from_request directly (not as a dependency).
        with mock.patch("routers.discover.get_user_from_request", return_value=user), \
                mock.patch("routers.discover._get_tmdb", return_value=tmdb):
            return TestClient(app).get(path), tmdb

    def test_a_kids_search_is_answered_without_the_tmdb_catalogue(self):
        r, tmdb = self._get(self.kid, "/api/discover/search?q=bluey&type=all")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([], r.json()["items"])
        self.assertIs(True, r.json().get("kids"))
        tmdb.search_multi_results.assert_not_called()

    def test_the_rest_of_discover_stays_closed_to_kids(self):
        r, _ = self._get(self.kid, "/api/discover/providers")
        self.assertEqual(403, r.status_code)

    def test_an_adults_search_is_unchanged(self):
        r, _ = self._get(self.adult, "/api/discover/search?q=bluey&type=channels")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(["Bluey TV"], [i["title"] for i in r.json()["items"]])
        self.assertNotIn("kids", r.json())


# A small DOM: enough for tentacle-search.js to find Jellyfin's search input,
# attach, search and draw. innerHTML is not parsed; the results container's
# grid is made when the container is written.
DOM = r"""
function El(tag, id) {
  var e = { tagName: tag.toUpperCase(), id: id || '', children: [], parentNode: null, style: {}, value: '',
    _listeners: {}, _html: '', offsetParent: {},
    classList: { _s: new Set(), add: function (c) { this._s.add(c); }, remove: function (c) { this._s.delete(c); },
      contains: function (c) { return this._s.has(c); }, toggle: function (c, on) { on ? this._s.add(c) : this._s.delete(c); } },
    appendChild: function (c) { c.parentNode = this; this.children.push(c); return c; },
    remove: function () { if (this.parentNode) { var p = this.parentNode; p.children = p.children.filter(function (x) { return x !== e; }); this.parentNode = null; } },
    addEventListener: function (t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); },
    removeEventListener: function (t, f) { this._listeners[t] = (this._listeners[t] || []).filter(function (x) { return x !== f; }); },
    dispatch: function (t, ev) { (this._listeners[t] || []).slice().forEach(function (f) { f(ev || { target: e }); }); },
    setAttribute: function (k, v) { this['_a_' + k] = v; }, getAttribute: function (k) { return this['_a_' + k] || null; },
    querySelector: function (sel) { return sel === '.searchfields-txtSearch' ? (this._input || null) : null; },
    querySelectorAll: function () { return []; },
    closest: function () { return null; } };
  Object.defineProperty(e, 'innerHTML', { get: function () { return this._html; }, set: function (v) {
    this._html = String(v);
    if (this.id === 'tentacleSearchResults' && this._html.indexOf('id="tentacleSearchGrid"') !== -1) {
      this.children = []; this.appendChild(El('div', 'tentacleSearchGrid'));
    } } });
  Object.defineProperty(e, 'textContent', { get: function () { return this._text || ''; }, set: function (v) {
    this._text = String(v); this._html = String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } });
  return e;
}
function find(root, id) {
  if (root.id === id) return root;
  for (var i = 0; i < root.children.length; i++) { var f = find(root.children[i], id); if (f) return f; }
  return null;
}
var docListeners = {};
var document = { readyState: 'complete', body: El('body'), head: El('head'),
  createElement: function (t) { return El(t); },
  getElementById: function (id) { return find(document.body, id) || find(document.head, id); },
  querySelector: function () { return null; },
  querySelectorAll: function (sel) { return sel === '.view' ? [view] : []; },
  addEventListener: function (t, f) { (docListeners[t] = docListeners[t] || []).push(f); },
  removeEventListener: function () {} };
var view = El('div'); view.className = 'view';
var input = El('input'); view._input = input; view.appendChild(input);
document.body.appendChild(view);
var window = globalThis;
window.document = document;
window.location = { hash: '#/search.html', pathname: '/web/' };
window.getComputedStyle = function () { return { display: 'block' }; };
window.addEventListener = function () {};
window.MutationObserver = function () { return { observe: function () {}, disconnect: function () {} }; };
var searches = [];
window.ApiClient = {
  getCurrentUserId: function () { return USER; },
  getUrl: function (p) { return p; },
  serverId: function () { return 's1'; },
  getJSON: function (url) {
    if (url.indexOf('TentacleDiscover/Search') === 0) { searches.push(url); return Promise.resolve(JSON.parse(JSON.stringify(ANSWER))); }
    return Promise.resolve({ show: false });
  },
  fetch: function () { return Promise.resolve({}); }
};
function settle() { return new Promise(function (r) { setTimeout(r, 0); }); }
function nativeShown() {
  return !document.body.classList.contains('tentacle-search-active') && !document.getElementById('tentacleSearchHideNative');
}
"""


def search_page(user: str, answer: dict) -> dict:
    """Open the search page with "bluey" typed, Tentacle answering `answer`;
    then come back to the page and type again."""
    src = (INJECT / "tentacle-search.js").read_text(encoding="utf-8")
    script = ("var USER = %s; var ANSWER = %s;\n" % (json.dumps(user), json.dumps(answer)) + DOM + """
input.value = 'bluey';
""" + src + """
(async function () {
  await settle(); await settle();
  var grid = document.getElementById('tentacleSearchGrid');
  var first = { nativeShown: nativeShown(), ours: !!document.getElementById('tentacleSearchResults'),
                text: grid ? grid.innerHTML : null };
  // Back to the search page (Jellyfin's viewshow) and a new query.
  (docListeners.viewshow || []).forEach(function (f) { f({ detail: { type: 'search' }, target: view }); });
  var afterVisit = nativeShown();
  input.value = 'bluey 2';
  input.dispatch('input', { target: input });
  await new Promise(function (r) { setTimeout(r, 450); });
  await settle(); await settle();
  console.log(JSON.stringify({ first: first, again: { afterVisit: afterVisit, nativeShown: nativeShown() }, searches: searches.length }));
})();
""")
    return node(script)


KIDS_ANSWER = {"items": [], "kids": True}           # what the server answers a kid with this change
UNAUTHORIZED = {"items": [], "error": "unauthorized",  # what the plugin turns today's 403 into
                "message": "Tentacle didn't accept this account. Open the Tentacle dashboard once to finish setup."}
ADULT_ANSWER = {"items": [{"media_type": "channel", "title": "Bluey TV", "channel_id": 1}]}


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class KidsSearchInJellyfinWeb(unittest.TestCase):
    def test_a_kid_gets_jellyfins_own_search(self):
        out = search_page("kid1", KIDS_ANSWER)
        self.assertTrue(out["first"]["nativeShown"], "Jellyfin's own results stay hidden")
        self.assertFalse(out["first"]["ours"])

    def test_and_keeps_it_when_coming_back_to_the_page(self):
        out = search_page("kid1", KIDS_ANSWER)
        self.assertTrue(out["again"]["afterVisit"], "Jellyfin's results hidden again on the next visit")
        self.assertTrue(out["again"]["nativeShown"])
        self.assertEqual(1, out["searches"], "Tentacle asked again for the same person")

    def test_an_adult_still_gets_tentacles_search(self):
        out = search_page("adult1", ADULT_ANSWER)
        self.assertFalse(out["first"]["nativeShown"])
        self.assertTrue(out["first"]["ours"])
        self.assertIn("Bluey TV", out["first"]["text"])
        self.assertEqual(2, out["searches"])

    def test_a_refused_account_still_reads_why(self):
        # Unchanged: what the plugin answers for a 401/403 is shown in Tentacle's search. Today
        # this is also what a kid gets (the kids guard's 403), with Jellyfin's results hidden.
        out = search_page("kid1", UNAUTHORIZED)
        self.assertIn("finish setup", out["first"]["text"])
        self.assertFalse(out["first"]["nativeShown"])


if __name__ == "__main__":
    unittest.main()
