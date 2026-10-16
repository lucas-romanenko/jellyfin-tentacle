"""Pin three dashboard fixes that had no test: #197, #203 and #205.

- #197 "Which movie is this really?": opening Fix it for one title and then
  another (or searching twice) let the slower suggestions answer fill the
  list, and a pick from it re-matched the title on screen to the other one's
  film. _fmLoad keeps only the newest answer for the title on screen.
- #203 Activity: the in-place update patched cards by position, so a title
  arriving or leaving rewrote every card below it (each poster flashed).
  _morphChildren moves cards by their data-morph-key and leaves them alone.
- #205 Discover: "From My Lists" never showed its picker (All + one tab per
  list) because nothing called loadListsSection().

Runs the real functions, lifted out of static/js/pages.js, under node with
stubs. Skipped when node is not installed.

Run from the tentacle/ directory:  python -m unittest tests.test_dashboard_late_answers_pinned
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")


def _fn(name):
    start = PAGES.index(f"function {name}(")
    if PAGES[max(0, start - 6):start] == "async ":
        start -= 6
    return PAGES[start:PAGES.index("\n}\n", start) + 2]


def _decl(prefix):
    return next(l for l in PAGES.splitlines() if l.startswith(prefix))


def _run(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout)


FIX_MATCH = r"""
const els = {};
function el(id) { return els[id] || (els[id] = { id, innerHTML: '', textContent: '' }); }
const document = { getElementById: el };
const pending = [];
function api(url) { return new Promise((res, rej) => pending.push({ url, res, rej })); }
function escapeAttr(s) { return String(s); }
function _imgUrl() { return ''; }
const _fm = { tmdbId: 0, cands: [] };
async function settle() { for (let i = 0; i < 20; i++) await null; }
%s
%s
(async () => {
  const out = {};
  _fm.tmdbId = 21137; _fmLoad('');            // Fix it on one title...
  _fm.tmdbId = 949; _fmLoad('');              // ...then on another, before the first answered
  pending[1].res({ candidates: [{ title: 'Heat', year: 1995 }] }); await settle();
  pending[0].res({ candidates: [{ title: 'The Decline', year: 2020 }] }); await settle();
  out.titles = _fm.cands.map(c => c.title);
  out.list_has_other = el('fm-list').innerHTML.includes('The Decline');
  out.urls = pending.map(p => p.url);
  // two searches on one title, the first answers last
  pending.length = 0;
  _fmLoad('heat'); _fmLoad('heat 1995');
  pending[1].res({ candidates: [{ title: 'Heat (1995)' }] }); await settle();
  pending[0].rej(new Error('late failure')); await settle();
  out.search_titles = _fm.cands.map(c => c.title);
  out.search_list = el('fm-list').innerHTML.includes('late failure');
  process.stdout.write(JSON.stringify(out));
})();
"""

MORPH = r"""
class Node {
  constructor(name, attrs = {}, kids = []) {
    this.nodeType = 1; this.nodeName = name.toUpperCase(); this._a = new Map(Object.entries(attrs));
    this.childNodes = []; this.parentNode = null; this.writes = 0; kids.forEach(k => this.appendChild(k));
  }
  get attributes() { return [...this._a].map(([name, value]) => ({ name, value })); }
  getAttribute(n) { return this._a.has(n) ? this._a.get(n) : null; }
  hasAttribute(n) { return this._a.has(n); }
  setAttribute(n, v) { this.writes++; this._a.set(n, String(v)); }
  removeAttribute(n) { this.writes++; this._a.delete(n); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    c.parentNode = this; return c;
  }
  removeChild(c) { this.childNodes.splice(this.childNodes.indexOf(c), 1); c.parentNode = null; return c; }
  replaceChild(n, o) { this.insertBefore(n, o); return this.removeChild(o); }
}
function card(key, poster) {
  return new Node('div', { class: 'activity-card', 'data-morph-key': key }, [new Node('img', { src: poster })]);
}
%s
%s
%s
const live = new Node('div', {}, [card('s:a', '/a.jpg'), card('s:b', '/b.jpg'), card('s:c', '/c.jpg')]);
const [a, b, c] = live.childNodes;
const out = {};
// A title arrives at the top
_morphChildren(live, new Node('div', {}, [card('s:new', '/new.jpg'), card('s:a', '/a.jpg'), card('s:b', '/b.jpg'), card('s:c', '/c.jpg')]));
out.after_arrival = live.childNodes.map(n => n.getAttribute('data-morph-key'));
out.same_nodes = live.childNodes[1] === a && live.childNodes[2] === b && live.childNodes[3] === c;
out.poster_writes = [a, b, c].map(n => n.childNodes[0].writes);
// One leaves from the middle
_morphChildren(live, new Node('div', {}, [card('s:new', '/new.jpg'), card('s:a', '/a.jpg'), card('s:c', '/c.jpg')]));
out.after_leave = live.childNodes.map(n => n.getAttribute('data-morph-key'));
out.kept_c = live.childNodes[2] === c && c.childNodes[0].writes === 0;
process.stdout.write(JSON.stringify(out));
"""

LISTS = r"""
const calls = [];
const pills = { style: { display: 'none' } };
const document = { getElementById: id => (id === 'discover-streaming-pills' ? pills : null), querySelectorAll: () => [] };
let _discoverActiveSection = null, _discoverSections = [], _discoverView = 0;
function loadStreamingSection() { calls.push('streaming'); }
function loadGenreSection() { calls.push('genres'); }
function loadListsSection() { calls.push('lists'); }
function renderDiscoverGrid() { calls.push('grid'); }
%s
switchDiscoverSection('missing');
process.stdout.write(JSON.stringify({ calls, display: pills.style.display }));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FixMatchLateSuggestions(unittest.TestCase):
    """#197"""

    @classmethod
    def setUpClass(cls):
        cls.out = _run(FIX_MATCH % (_decl("let _fmSeq"), _fn("_fmLoad")))

    def test_the_title_on_screen_keeps_its_own_suggestions(self):
        self.assertEqual(["Heat"], self.out["titles"])
        self.assertFalse(self.out["list_has_other"], "the other title's late answer filled the list")
        self.assertIn("/movie/949/", self.out["urls"][1])

    def test_an_earlier_search_answering_last_is_dropped(self):
        self.assertEqual(["Heat (1995)"], self.out["search_titles"])
        self.assertFalse(self.out["search_list"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ActivityCardsMovedByKey(unittest.TestCase):
    """#203"""

    @classmethod
    def setUpClass(cls):
        cls.out = _run(MORPH % (_fn("_morphKey"), _fn("_morphNode"), _fn("_morphChildren")))

    def test_a_title_arriving_on_top_leaves_the_others_untouched(self):
        self.assertEqual(["s:new", "s:a", "s:b", "s:c"], self.out["after_arrival"])
        self.assertTrue(self.out["same_nodes"], "the cards below were rebuilt instead of moved")
        self.assertEqual([0, 0, 0], self.out["poster_writes"], "posters of unchanged cards were rewritten")

    def test_a_title_leaving_removes_only_its_card(self):
        self.assertEqual(["s:new", "s:a", "s:c"], self.out["after_leave"])
        self.assertTrue(self.out["kept_c"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FromMyListsPicker(unittest.TestCase):
    """#205"""

    def test_from_my_lists_builds_its_picker(self):
        out = _run(LISTS % _fn("switchDiscoverSection"))
        self.assertEqual(["lists"], out["calls"])
        self.assertEqual("flex", out["display"])


if __name__ == "__main__":
    unittest.main()
