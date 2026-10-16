"""Dashboard Discover: a Movies/TV/Music switch or a search answered late never
paints over the newer choice (#207 follow-up).

#207 guarded the section, provider, genre and list loaders with _discoverView.
loadDiscover() (run on every Movies/TV switch) and doDiscoverSearch() (every
search, debounced while typing) had no guard: with TMDB slow, the answer of
the earlier type or the earlier query arrived last and replaced the grid and
the section tabs under the newer choice (movies under "TV", "dune" results
while "dune part two" is in the box, TMDB tabs over Music).

Runs the real functions, lifted out of static/js/pages.js, under node with
stubs. Skipped when node is not installed.

Run from the tentacle/ directory:  python -m unittest tests.test_discover_late_answer_type_search
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


def _let(name):
    line = next((l for l in PAGES.splitlines() if l.startswith(f"let {name} ")), None)
    return line or f"let {name} = 0;"


STUBS = r"""
const els = {};
function el(id) { return els[id] || (els[id] = { id, innerHTML: '', classList: { remove() {}, add() {} }, placeholder: '' }); }
const document = { getElementById: el, querySelectorAll() { return []; } };
const state = { currentUser: { is_admin: true } };
let _activityData = {};
let _discoverSearchQuery = '';
let _discoverActiveSection = null;
let _discoverSections = [];
let _discoverType = 'movies';
const pending = [];
function api(url) { return new Promise((res, rej) => pending.push({ url, res, rej })); }
function loadActivity() { return Promise.resolve(); }
function toast() {}
function escapeAttr(s) { return String(s); }
function escapeJS(s) { return String(s); }
function escHtml(s) { return String(s); }
const painted = [];
function renderDiscoverGrid(items) { painted.push(items.map(i => i.title).join(',')); }
const switched = [];
function switchDiscoverSection(id) { _discoverView++; switched.push(id + ':' + _discoverSections.map(s => s.id).join('+')); }
const DISCOVER_SECTION_LABELS = {};
let musicCalls = 0;
function musicDiscoverHome() { musicCalls++; el('discover-grid').innerHTML = 'MUSIC'; return Promise.resolve(); }
function musicSearch() { el('discover-grid').innerHTML = 'MUSIC SEARCH'; return Promise.resolve(); }
async function settle() { for (let i = 0; i < 20; i++) await null; }
function answer(i, body) { pending[i].res(body); }
"""

SCRIPT = STUBS + "\n%s\n%s\n%s\n" + r"""
(async () => {
  const out = {};
  // 1. Two searches while typing: the earlier one answers last.
  doDiscoverSearch('dune');
  doDiscoverSearch('dune part two');
  answer(1, { items: [{ title: 'Dune: Part Two' }] }); await settle();
  answer(0, { items: [{ title: 'Dune' }] }); await settle();
  out.search = painted.slice();

  // 2. Movies -> TV while Movies is loading: Movies answers last.
  pending.length = 0; painted.length = 0;
  _discoverType = 'movies'; loadDiscover();
  _discoverType = 'series'; loadDiscover();
  answer(1, { sections: [{ id: 'on_the_air', items: [{ title: 'Show' }] }] }); await settle();
  answer(0, { sections: [{ id: 'now_playing', items: [{ title: 'Film' }] }] }); await settle();
  out.type = { switched: switched.slice(), sections: _discoverSections.map(s => s.id) };

  // 3. Movies -> Music while Movies is loading.
  pending.length = 0; switched.length = 0;
  _discoverType = 'movies'; loadDiscover();
  _discoverType = 'music'; loadDiscover();
  answer(0, { sections: [{ id: 'now_playing', items: [{ title: 'Film' }] }] }); await settle();
  out.music = { grid: el('discover-grid').innerHTML, switched: switched.slice() };

  // 4. A search typed while the Discover page itself is loading.
  pending.length = 0; switched.length = 0; painted.length = 0;
  _discoverType = 'movies'; loadDiscover();
  doDiscoverSearch('heat');
  answer(1, { items: [{ title: 'Heat' }] }); await settle();
  answer(0, { sections: [{ id: 'popular', items: [{ title: 'Popular film' }] }] }); await settle();
  out.search_over_discover = { painted: painted.slice(), switched: switched.slice() };
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LateDiscoverAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = SCRIPT % (_let("_discoverView"), _fn("loadDiscover"), _fn("doDiscoverSearch"))
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise AssertionError(out.stderr[-2000:])
        cls.out = json.loads(out.stdout)

    def test_the_last_search_typed_is_the_one_shown(self):
        self.assertEqual(["Dune: Part Two"], self.out["search"])

    def test_a_movies_answer_arriving_after_the_switch_to_tv_is_dropped(self):
        self.assertEqual(["on_the_air:on_the_air"], self.out["type"]["switched"])
        self.assertEqual(["on_the_air"], self.out["type"]["sections"])

    def test_a_movies_answer_arriving_after_the_switch_to_music_is_dropped(self):
        self.assertEqual("MUSIC", self.out["music"]["grid"])
        self.assertEqual([], self.out["music"]["switched"])

    def test_a_late_discover_page_does_not_replace_search_results(self):
        self.assertEqual(["Heat"], self.out["search_over_discover"]["painted"])
        self.assertEqual([], self.out["search_over_discover"]["switched"])


if __name__ == "__main__":
    unittest.main()
