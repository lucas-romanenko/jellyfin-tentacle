"""A slow answer for an earlier opening never lands in a later one (#144, #170).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The title-detail modal and the Add / Manage / Download More modal are shared:
one element tree, reopened for every title. Before 5f2b5cd they showed
whichever answer arrived LAST, so a slow detail for an earlier title replaced
the one clicked (#170), a slow Radarr profile list landed in a later Sonarr
add, and the Add path kept another series' downloaded episodes, shown as
locked "DL" (#144). The fix gives every opening a number that each await
checks. Nothing pinned it: these run the real openers under node with an
api() whose answers the test releases in any order.
"""
import unittest

from dashboard_js import DOM_STUB, HAVE_NODE, functions, node

SRC = functions("pages.js", [
    "_detailOpening", "showMediaDetail",
    "_resetEpisodePicker", "_resetManageMode", "_profileOptionsHtml", "showAddToArrModal",
])

HARNESS = DOM_STUB + r"""
// Answers are held until the test releases them, in the order it chooses.
const held = [];
async function api(path) {
  return new Promise((resolve, reject) => held.push({ path, resolve, reject }));
}
function release(prefix, value) {
  const i = held.findIndex(h => h.path.startsWith(prefix));
  if (i < 0) throw new Error('nothing pending for ' + prefix + ' in ' + JSON.stringify(held.map(h => h.path)));
  const h = held.splice(i, 1)[0];
  if (value instanceof Error) h.reject(value); else h.resolve(value);
}
const tick = () => new Promise(r => setImmediate(r));
const _getById = document.getElementById.bind(document);
document.getElementById = id => { const e = _getById(id); e.parentElement = e.parentElement || { style: {} }; return e; };
function showModal() {}
function escapeAttr(s) { return String(s); }
function escapeJS(s) { return String(s); }
function _imgUrl() { return ''; }
function _trailerBtn() { return ''; }
function _loadSeriesEpisodes() {}
const state = { currentUser: { is_admin: true } };
let _detailSeq = 0, _detailEpState = {};
let _addArrTmdbId = null, _addArrTvdbId = 0, _addArrMediaType = 'movie';
let _epPickerSeasons = [], _epPickerLoaded = {}, _epPickerToken = 0;
let _vodEpisodes = {}, _dlEpisodes = {}, _unairedPerSeason = {};
let _manageTmdbId = null, _downloadMoreTmdbId = null;
"""


def run(body: str):
    script = (HARNESS + SRC + "\n(async () => {\n" + body + "\n})().then("
              "r => process.stdout.write(JSON.stringify(r)),"
              " e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });")
    return node(script)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DetailModalKeepsTheTitleClicked(unittest.TestCase):
    """#170: the detail modal shows the title clicked last."""

    def test_a_late_answer_for_the_earlier_title_is_dropped(self):
        out = run("""
          const first = showMediaDetail(1, 'movie');
          const second = showMediaDetail(2, 'movie');
          release('/api/library/item/movie/2', { title: 'Second' }); await tick();
          release('/api/library/item/movie/1', { title: 'First' });
          await first; await second;
          return document.getElementById('detail-title').textContent;
        """)
        self.assertEqual("Second", out)

    def test_a_late_failure_for_the_earlier_title_is_dropped(self):
        out = run("""
          const first = showMediaDetail(1, 'movie');
          const second = showMediaDetail(2, 'movie');
          release('/api/library/item/movie/2', { title: 'Second', overview: 'the second film' }); await tick();
          release('/api/library/item/movie/1', new Error('timed out'));
          await first; await second;
          return document.getElementById('detail-body').innerHTML;
        """)
        self.assertIn("the second film", out)
        self.assertNotIn("Failed to load details", out)

    def test_answers_in_order_still_show_the_last_title(self):
        out = run("""
          const first = showMediaDetail(1, 'movie');
          release('/api/library/item/movie/1', { title: 'First' }); await first;
          const second = showMediaDetail(2, 'movie');
          release('/api/library/item/movie/2', { title: 'Second' }); await second;
          return document.getElementById('detail-title').textContent;
        """)
        self.assertEqual("Second", out)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class AddModalBelongsToItsOwnOpening(unittest.TestCase):
    """#144: the Add modal's profile list and episode state are its own."""

    def test_a_slow_radarr_profile_list_does_not_fill_a_later_sonarr_add(self):
        out = run("""
          const movie = showAddToArrModal(10, 'A film', 2001, null, 'movie');
          const show = showAddToArrModal(20, 'A show', 2002, null, 'series');
          release('/api/lists/sonarr-profiles', [{ id: 7, name: 'SonarrHD' }]); await tick();
          release('/api/lists/radarr-profiles', [{ id: 3, name: 'RadarrUHD' }]);
          await movie; await show;
          return { select: document.getElementById('add-radarr-quality').innerHTML,
                   title: document.getElementById('add-arr-modal-title').textContent };
        """)
        self.assertEqual("Add to Sonarr", out["title"])
        self.assertIn("SonarrHD", out["select"])
        self.assertNotIn("RadarrUHD", out["select"])

    def test_opening_add_drops_another_series_episode_state(self):
        out = run("""
          // What Download More / Manage left behind for another series.
          _dlEpisodes = { 1: [1, 2, 3] }; _vodEpisodes = { 1: [4] }; _unairedPerSeason = { 2: 5 };
          _epPickerSeasons = [{ season_number: 1 }]; _epPickerLoaded = { 1: [{ episode_number: 1 }] };
          const p = showAddToArrModal(20, 'Another show', 2002, null, 'series');
          release('/api/lists/sonarr-profiles', []); await p;
          return [_dlEpisodes, _vodEpisodes, _unairedPerSeason, _epPickerSeasons, _epPickerLoaded];
        """)
        self.assertEqual([{}, {}, {}, [], {}], out)


if __name__ == "__main__":
    unittest.main()
