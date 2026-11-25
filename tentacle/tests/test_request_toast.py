"""A family request in Jellyfin web: an admin answers it from the toast.

Lucas (25 Nov 2026, watching on Jellyfin web): "one of my users just
requested some content, and I saw it in my jellyfin ... should maybe make it
so i can click that and it sends me to tentacle to approve". The toast was
text only ("Ann asked for this. Approve or decline it in Tentacle →
Requests.") and went after 8 s.

- Tentacle: the admins' "asked for this" message carries its request
  (request_id); only an admin's notifications list says which request, its
  state, who asked and Tentacle → Requests with it in view (#requests/<id>,
  the Pushover link too). Answered anywhere (or taken back), the admins'
  messages about it go away. Approve / decline stay admin-only.
- The plugin: TentacleDiscover/Requests/{id}/Approve|Decline, Jellyfin
  admins only, forwarded with the caller's token (Tentacle checks again).
- Jellyfin web: the request toast has Approve / Decline with a plain result
  line, opens Requests when clicked, stays until answered or closed.

The toast runs the real tentacle-notifications.js under node with a small
working DOM. Run from the tentacle/ directory:
python -m unittest tests.test_request_toast
"""
import re
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
from routers.auth import get_user_from_request
from services import approvals
from inject_js import HAVE_NODE, INJECT, node
from test_family import Case

PLUGIN_API = INJECT.parent / "Api" / "DiscoverController.cs"


class Server(Case):
    def setUp(self):
        super().setUp()
        p = mock.patch("services.approvals._details", return_value={"title": "Dune", "year": "2021",
                                                                    "poster_path": "/d.jpg"})
        p.start()
        self.addCleanup(p.stop)
        mdb.set_setting(self.db, "external_url", "https://tentacle.example")
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [438631]}, tmdb_ids=[438631])
        self.row = self.db.query(mdb.MediaRequest).one()

    def client(self, user):
        from routers import family, notifications
        app = FastAPI()
        app.include_router(family.router)
        app.include_router(notifications.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        # The real require_admin, on top of the person the token resolved to.
        p = mock.patch("routers.auth.get_user_from_request", return_value=user)
        p.start()
        self.addCleanup(p.stop)
        return TestClient(app)

    def asks(self, user):
        return [n for n in self.client(user).get("/api/notifications").json()["notifications"]
                if n["kind"] == "request"]

    def test_the_admins_toast_knows_its_request(self):
        [n] = self.asks(self.admin)
        self.assertEqual({"request_id": self.row.id, "request_status": "pending", "requested_by": "Ann",
                          "requests_url": f"https://tentacle.example/#requests/{self.row.id}"},
                         {k: n[k] for k in ("request_id", "request_status", "requested_by", "requests_url")})
        self.assertEqual("Dune", n["title"])

    def test_pushover_opens_that_request(self):
        self.assertEqual(f"https://tentacle.example/#requests/{self.row.id}", self.push.call_args[0][3])

    def test_no_one_else_is_told_which_request(self):
        # Even a message carrying a request id, in a non-admin's list.
        mdb.create_notification(self.db, self.ann.id, 438631, "movie", "Dune", "x", kind="request",
                                force=True, request_id=self.row.id)
        [n] = self.asks(self.ann)
        self.assertFalse({"request_id", "request_status", "requested_by", "requests_url"} & set(n))

    def test_a_non_admin_cannot_answer(self):
        c = self.client(self.ann)
        self.assertEqual(403, c.post(f"/api/requests/{self.row.id}/approve").status_code)
        self.assertEqual(403, c.post(f"/api/requests/{self.row.id}/decline", json={}).status_code)
        self.db.refresh(self.row)
        self.assertEqual("pending", self.row.status)

    def test_approved_the_toast_goes(self):
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1, "already_exists": 0, "failed": 0}
        with mock.patch("services.media_requests.request_movies", return_value=outcome), \
                mock.patch("services.media_requests.coming_out", return_value=None):
            r = self.client(self.admin).post(f"/api/requests/{self.row.id}/approve")
        self.assertEqual((200, True), (r.status_code, r.json()["ok"]))
        self.assertEqual([], self.asks(self.admin))

    def test_declined_with_no_body_the_toast_goes(self):
        r = self.client(self.admin).post(f"/api/requests/{self.row.id}/decline", json={})
        self.assertEqual(200, r.status_code)
        self.assertEqual([], self.asks(self.admin))
        self.assertEqual(409, self.client(self.admin).post(f"/api/requests/{self.row.id}/approve").status_code)

    def test_a_failed_approve_keeps_the_toast_to_try_again(self):
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 0, "already_exists": 0, "failed": 1,
                                            "detail": "Radarr isn't answering"}
        with mock.patch("services.media_requests.request_movies", return_value=outcome):
            r = self.client(self.admin).post(f"/api/requests/{self.row.id}/approve")
        self.assertEqual((False, "Radarr isn't answering"), (r.json()["ok"], r.json()["detail"]))
        [n] = self.asks(self.admin)
        self.assertEqual("failed", n["request_status"])

    def test_taken_back_the_toast_goes(self):
        self.assertTrue(approvals.cancel(self.db, self.row.id, self.ann))
        self.assertEqual([], self.asks(self.admin))


class Plugin(unittest.TestCase):
    def test_answers_are_jellyfin_admins_only_and_forward_tentacles_status(self):
        src = PLUGIN_API.read_text(encoding="utf-8")
        for what in ("Approve", "Decline"):
            m = re.search(r'\[HttpPost\("Requests/\{requestId\}/' + what + r'"\)\]\s*\n\s*\[Authorize\(Policy = '
                          r'"RequiresElevation"\)\]', src)
            self.assertTrue(m, what + " is not admin-only")
        body = src[src.index("private async Task<ActionResult> AnswerRequest"):]
        body = body[:body.index("\n    }\n")]
        self.assertIn("AppendUserId(", body)        # the caller's token: Tentacle checks it's an admin
        self.assertIn("StatusCode = (int)response.StatusCode", body)


# A small DOM that works: classes, children, querySelector('.x'), clicks.
DOM = r"""
function _el(tag) {
  var e = { tagName: tag.toUpperCase(), children: [], parentNode: null, style: {}, attrs: {}, _cls: [],
    _on: {}, textContent: '', title: '', src: '', alt: '' };
  Object.defineProperty(e, 'className', { get: function () { return this._cls.join(' '); },
    set: function (v) { this._cls = String(v).split(/\s+/).filter(Boolean); } });
  e.classList = { add: function (c) { if (e._cls.indexOf(c) < 0) e._cls.push(c); },
    remove: function (c) { e._cls = e._cls.filter(function (x) { return x !== c; }); },
    contains: function (c) { return e._cls.indexOf(c) >= 0; } };
  e.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  e.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
  e.removeChild = function (c) { this.children = this.children.filter(function (x) { return x !== c; }); c.parentNode = null; };
  e.addEventListener = function (n, f) { (this._on[n] = this._on[n] || []).push(f); };
  e.all = function () { var out = []; this.children.forEach(function (c) { out.push(c); out = out.concat(c.all()); }); return out; };
  e.querySelector = function (sel) { var c = sel.replace(/^\./, ''); return this.all().filter(function (x) { return x.classList.contains(c); })[0] || null; };
  e.text = function () { return [this.textContent].concat(this.children.map(function (c) { return c.text(); })).filter(Boolean).join(' | '); };
  return e;
}
function click(target) {
  var ev = { target: target };
  for (var n = target; n; n = n.parentNode) (n._on.click || []).forEach(function (f) { f(ev); });
}
var timers = [], opened = [], posts = [], listeners = {};
var document = { body: _el('body'), readyState: 'complete', hidden: false,
  createElement: function (t) { return _el(t); },
  querySelector: function (s) { return document.body.querySelector(s); },
  addEventListener: function (n, f) { (listeners[n] = listeners[n] || []).push(f); } };
// The tab comes back to the front: the script reads the notifications again.
async function pollNow() { (listeners.visibilitychange || []).forEach(function (f) { f(); });
  for (var i = 0; i < 4; i++) await tick(); }
var window = { addEventListener: function () {}, dispatchEvent: function () {},
  open: function (u, t) { opened.push([u, t]); } };
function CustomEvent() {}
function setTimeout(fn, ms) { timers.push([fn, ms]); return timers.length; }
function setInterval() { return 1; }
function requestAnimationFrame(fn) { fn(); }
var feed = { notifications: [], notifications_enabled: true };
var answerWith = null;
window.ApiClient = {
  getCurrentUserId: function () { return 'u1'; },
  getUrl: function (p) { return '/' + p; },
  getJSON: function () { return Promise.resolve(JSON.parse(JSON.stringify(feed))); },
  ajax: function (o) { posts.push(o.url); return Promise.resolve(); },
  fetch: function (o) { posts.push(o.url); return answerWith(); },
};
function ok(body) { return function () { return Promise.resolve(body); }; }
function http(status, body) { return function () { return Promise.reject({ status: status,
  json: function () { return Promise.resolve(body); } }); }; }
function toasts() { return document.body.all().filter(function (x) { return x.classList.contains('tentacle-toast'); }); }
function tick() { return new Promise(function (r) { setImmediate(r); }); }
var ASK = { id: 41, kind: 'request', title: 'Dune', message: 'Ann asked for this. Approve or decline it in Tentacle → Requests.',
  poster_path: '/d.jpg', request_id: 7, request_status: 'pending', requested_by: 'Ann',
  requests_url: 'https://tentacle.example/#requests/7' };
"""

TOAST_JS = (INJECT / "tentacle-notifications.js").read_text(encoding="utf-8")


def run(body: str):
    # The script keeps its state in a closure: expose it for the test.
    src = TOAST_JS.replace("  TN.topicsOf = topicsOf;", "  TN.topicsOf = topicsOf; window.__TN = TN;")
    return node(DOM + src + "\n(async function () {\n" + body + "\n})();")


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Toast(unittest.TestCase):
    def test_an_admin_gets_approve_and_decline_and_it_stays(self):
        out = run(r"""
          feed.notifications = [ASK]; await pollNow(); await pollNow();
          var t = toasts();
          var box = document.querySelector('.tentacle-asks');
          console.log(JSON.stringify({ n: t.length, text: t[0].text(), inBox: t[0].parentNode === box,
            autoClose: timers.filter(function (x) { return x[1] >= 1000; }).length }));
        """)
        self.assertEqual(1, out["n"])
        self.assertEqual("Request | Dune | Ann asked for this | Approve | Decline | ×", out["text"])
        self.assertTrue(out["inBox"], "not in the top-right stack (would cover the player's controls)")
        self.assertEqual(0, out["autoClose"], "a request toast must not vanish by itself")

    def answer(self, what, reply):
        return run(r"""
          feed.notifications = [ASK]; await pollNow();
          var t = toasts()[0];
          answerWith = %REPLY%;
          click(t.querySelector('.tentacle-toast-%WHAT%')); await tick(); await tick(); await tick();
          var r = t.querySelector('.tentacle-toast-result');
          console.log(JSON.stringify({ line: r.textContent, good: r.classList.contains('tentacle-toast-good'),
            post: posts.filter(function (u) { return u.indexOf('/Requests/') >= 0; }),
            yes: t.querySelector('.tentacle-toast-approve').textContent,
            buttons: t.querySelector('.tentacle-toast-actions').style.display || '',
            opened: opened.length }));
        """.replace("%REPLY%", reply).replace("%WHAT%", what))

    def test_approve(self):
        out = self.answer("approve", "ok({ ok: true, status: 'approved', detail: '' })")
        self.assertEqual(["/TentacleDiscover/Requests/7/Approve?userId=u1"], out["post"])
        self.assertEqual(("Approved, downloading", True, "none"), (out["line"], out["good"], out["buttons"]))
        self.assertEqual(0, out["opened"], "a button click also opened Requests")

    def test_decline(self):
        out = self.answer("decline", "ok({ ok: true, status: 'declined' })")
        self.assertEqual(["/TentacleDiscover/Requests/7/Decline?userId=u1"], out["post"])
        self.assertEqual(("Declined", True), (out["line"], out["good"]))

    def test_radarr_said_no_try_again(self):
        out = self.answer("approve", "ok({ ok: false, status: 'failed', detail: \"Radarr isn't answering\" })")
        self.assertEqual("It didn’t go through: Radarr isn't answering.", out["line"])
        self.assertEqual(("Try again", ""), (out["yes"], out["buttons"]))

    def test_someone_else_answered(self):
        out = self.answer("approve", "http(409, { detail: 'Someone already answered this request.' })")
        self.assertEqual("Someone already answered this request.", out["line"])

    def test_refused(self):
        out = self.answer("approve", "http(403, null)")
        self.assertEqual("Only an admin can answer requests.", out["line"])

    def test_clicking_the_toast_opens_that_request(self):
        out = run(r"""
          feed.notifications = [ASK]; await pollNow();
          click(toasts()[0].querySelector('.tentacle-toast-title'));
          console.log(JSON.stringify(opened));
        """)
        self.assertEqual([["https://tentacle.example/#requests/7", "_blank"]], out)

    def test_answered_elsewhere_it_goes(self):
        out = run(r"""
          feed.notifications = [ASK]; await pollNow();
          var before = toasts().length;
          feed.notifications = []; await pollNow();
          timers.forEach(function (t) { if (t[1] === 400) t[0](); });
          console.log(JSON.stringify({ before: before, after: toasts().length,
            dismissed: posts.filter(function (u) { return u.indexOf('Dismiss') >= 0; }).length }));
        """)
        self.assertEqual({"before": 1, "after": 0, "dismissed": 0}, out)

    def test_closed_it_goes_and_stays_gone(self):
        out = run(r"""
          feed.notifications = [ASK]; await pollNow();
          click(toasts()[0].querySelector('.tentacle-toast-close'));
          timers.forEach(function (t) { if (t[1] === 400) t[0](); });
          console.log(JSON.stringify({ after: toasts().length, opened: opened.length,
            dismissed: posts.filter(function (u) { return u.indexOf('/Notifications/41/Dismiss') >= 0; }).length }));
        """)
        self.assertEqual({"after": 0, "opened": 0, "dismissed": 1}, out)

    def test_no_buttons_without_a_request_to_answer(self):
        # A family member's own "Lucas said yes" message, an old message, a
        # request someone already answered: the plain toast as before.
        out = run(r"""
          var plain = [{ id: 1, kind: 'request', title: 'Dune', message: 'Lucas said yes.' },
                       { id: 2, kind: 'request', title: 'Dune', message: 'x', request_id: 7, request_status: 'approved' }];
          console.log(JSON.stringify(plain.map(window.__TN.canAnswer).concat([window.__TN.canAnswer(ASK)])));
        """)
        self.assertEqual([False, False, True], out)


if __name__ == "__main__":
    unittest.main()
