"""Dashboard Discover: a Movies/TV/Music switch or a search answered late never
paints over the newer choice (#647, #207 follow-up).

#207 guarded the section, provider, genre and list loaders with _discoverView.
loadDiscover() (every Movies/TV/Music switch, every cleared search) and
doDiscoverSearch() (every search, debounced while typing) had no guard: with
TMDB slow, the answer of the earlier type or the earlier query arrived last and
replaced the grid and the section tabs under the newer choice (films under
"TV", "dune" results while "dune part two" is in the box, TMDB tabs over
Music). musicSearch() only dropped an answer overtaken by another music
search, so a slow MusicBrainz search painted its artists over Movies, or over
the Music home after the box was cleared.

Runs the real functions from static/js/pages.js and static/js/music.js under
node with stubs. Skipped when node is not installed.

Run from the tentacle/ directory:  python -m unittest tests.test_discover_late_answer_type_search
"""
import unittest

from dashboard_js import HAVE_NODE, JS, functions, node


def _let(name):
    src = (JS / "pages.js").read_text(encoding="utf-8")
    return next((l for l in src.splitlines() if l.startswith(f"let {name} ")), f"let {name} = 0;")


STUBS = r"""
const els = {};
function el(id) { return els[id] || (els[id] = { id, innerHTML: '', classList: { remove() {}, add() {} }, style: {}, placeholder: '' }); }
const document = { getElementById: el, querySelectorAll() { return []; } };
const state = { currentUser: { is_admin: true } };
let _activityData = {};
let _discoverSearchQuery = '';
let _discoverActiveSection = null;
let _discoverSections = [];
let _discoverType = 'movies';
const musicState = { seq: 0 };
const pending = [];
function api(url) { return new Promise((res, rej) => pending.push({ url, res, rej })); }
function loadActivity() { return Promise.resolve(); }
function toast() {}
function escapeAttr(s) { return String(s); }
function escapeJS(s) { return String(s); }
function escHtml(s) { return String(s); }
function _musicAlbumCard(a) { return String(a.title); }
function _musicDuration() { return ''; }
const painted = [];
function renderDiscoverGrid(items) {
  const titles = items.map(i => i.title).join(',');
  painted.push(titles);
  el('discover-grid').innerHTML = 'TMDB:' + titles;
}
const switched = [];
function switchDiscoverSection(id) { _discoverView++; switched.push(id + ':' + _discoverSections.map(s => s.id).join('+')); }
const DISCOVER_SECTION_LABELS = {};
function musicDiscoverHome() { el('discover-grid').innerHTML = 'MUSIC'; return Promise.resolve(); }
async function settle() { for (let i = 0; i < 20; i++) await null; }
function answer(i, body) { pending[i].res(body); }
function reset() { pending.length = 0; painted.length = 0; switched.length = 0; }
"""

SCENARIOS = r"""
(async () => {
  const out = {};
  // 1. Two searches while typing: the earlier one answers last.
  _discoverType = 'movies';
  doDiscoverSearch('dune');
  doDiscoverSearch('dune part two');
  answer(1, { items: [{ title: 'Dune: Part Two' }] }); await settle();
  answer(0, { items: [{ title: 'Dune' }] }); await settle();
  out.search = painted.slice();

  // 2. Movies -> TV while Movies is loading: Movies answers last.
  reset();
  _discoverType = 'movies'; loadDiscover();
  _discoverType = 'series'; loadDiscover();
  answer(1, { sections: [{ id: 'on_the_air', items: [{ title: 'Show' }] }] }); await settle();
  answer(0, { sections: [{ id: 'now_playing', items: [{ title: 'Film' }] }] }); await settle();
  out.type = { switched: switched.slice(), sections: _discoverSections.map(s => s.id) };

  // 3. Movies -> Music while Movies is loading.
  reset();
  _discoverType = 'movies'; loadDiscover();
  _discoverType = 'music'; loadDiscover();
  answer(0, { sections: [{ id: 'now_playing', items: [{ title: 'Film' }] }] }); await settle();
  out.music = { grid: el('discover-grid').innerHTML, switched: switched.slice() };

  // 4. A search typed while the Discover page itself is loading.
  reset();
  _discoverType = 'movies'; loadDiscover();
  doDiscoverSearch('heat');
  answer(1, { items: [{ title: 'Heat' }] }); await settle();
  answer(0, { sections: [{ id: 'popular', items: [{ title: 'Popular film' }] }] }); await settle();
  out.search_over_discover = { painted: painted.slice(), switched: switched.slice() };

  // 5. A Music search, then Movies with the same query: MusicBrainz answers last.
  reset();
  _discoverType = 'music'; doDiscoverSearch('heat');
  _discoverType = 'movies'; doDiscoverSearch('heat');
  answer(1, { items: [{ title: 'Heat' }] }); await settle();
  answer(0, { artists: [{ name: 'Heatmiser', mbid: 'm1' }] }); await settle();
  out.music_search_then_movies = el('discover-grid').innerHTML;

  // 6. A Music search, then the box is cleared (Music home): MusicBrainz answers last.
  reset();
  _discoverType = 'music'; doDiscoverSearch('heat');
  loadDiscover();
  answer(0, { artists: [{ name: 'Heatmiser', mbid: 'm1' }] }); await settle();
  out.music_search_then_cleared = el('discover-grid').innerHTML;

  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class LateDiscoverAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = "\n".join([
            STUBS,
            _let("_discoverView"),
            functions("pages.js", ["loadDiscover", "doDiscoverSearch"]),
            functions("music.js", ["musicSearch"]),
            SCENARIOS,
        ])
        cls.out = node(src)

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

    def test_a_music_search_answering_after_the_switch_to_movies_is_dropped(self):
        self.assertEqual("TMDB:Heat", self.out["music_search_then_movies"])

    def test_a_music_search_answering_after_the_box_is_cleared_is_dropped(self):
        self.assertEqual("MUSIC", self.out["music_search_then_cleared"])


if __name__ == "__main__":
    unittest.main()
