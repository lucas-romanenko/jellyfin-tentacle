"""Episode picker: unticking a closed season opens it; Manage Episodes hides Download More's toggle; season boxes big enough to hit.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_episode_picker_manage_untick.py"

Manage Episodes starts a fully monitored season with its box ticked. One
click on that box while the season was closed unticked (unmonitored on Save)
every episode with nothing on screen but the box changing. The season now
opens either way, in the dashboard and in Jellyfin web.

Download Remaining Episodes shows an "Auto-download new episodes" toggle in the
shared modal; Manage Episodes never hid it, so it stayed on screen there
although Save ignores it.

The season box was the browser's 13 px default (12 px drawn); a near miss
lands on the header, which opens or closes the season instead.

Runs the real functions under node with the page stub of
test_episode_picker_season_tick.py.
"""
import json
import re
import shutil
import unittest
from pathlib import Path

from test_episode_picker_season_tick import DOM, PAGES, PLUGIN, _pages_fn, _plugin_block, _run

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "tentacle" / "static" / "index.html").read_text(encoding="utf-8")
PLUGIN_CSS = (ROOT / "tentacle-plugin" / "Inject" / "tentacle-discover.css").read_text(encoding="utf-8")

MANAGE = DOM + r"""
const _byId = document.getElementById;
document.getElementById = id => { const e = _byId(id); e.parentElement = e.parentElement || { style: {} }; e.setAttribute = e.setAttribute || (() => {}); return e; };
let _epPickerToken = 1, _epPickerLoaded = {}, _vodEpisodes = {}, _dlEpisodes = {}, _unairedPerSeason = {};
let _epPickerSeasons = [], _downloadMoreTmdbId = null, _addArrTmdbId = null, _addArrTvdbId = null, _manageTmdbId = null;
function escapeAttr(s) { return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function showModal() {}
const SONARR = { in_sonarr: true, episodes: [1, 2].flatMap(sn => Array.from({ length: 6 }, (_, k) =>
  ({ seasonNumber: sn, episodeNumber: k + 1, title: 'Ep', monitored: sn === 1, hasFile: false }))) };
function api(path) { return later(50, SONARR); }
%(fns)s
(async () => {
  const out = {};
  document.getElementById('dl-more-monitor-wrap').style.display = 'flex';   // left by Download Remaining Episodes
  await showManageEpisodesModal(100, 'Show', '2020', null);
  out.wrap = document.getElementById('dl-more-monitor-wrap').style.display;
  out.before = { ...seasons[1].cb, open: el('ep-list-1').classList.contains('open') };
  seasons[1].cb.checked = false;            // the user unticks the closed, fully monitored season
  await toggleSeasonAll(1, false);
  const boxes = el('ep-list-1').boxes;
  out.after = { season: seasons[1].cb.checked, ticked: boxes.filter(b => b.checked).length, open: el('ep-list-1').classList.contains('open') };
  seasons[2].cb.checked = true;             // and ticks the closed, unmonitored one
  await toggleSeasonAll(2, true);
  out.tick2 = { season: seasons[2].cb.checked, ticked: el('ep-list-2').boxes.filter(b => b.checked).length, open: el('ep-list-2').classList.contains('open') };
  process.stdout.write(JSON.stringify(out));
})();
"""

PLUGIN_PAGE = DOM + r"""
const window = {};
const MD = { _epLoaded: {}, _vodEpisodes: {}, _dlEpisodes: {}, _currentItem: { tmdb_id: 100 } };
function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
const EPS = Array.from({ length: 10 }, (_, k) => ({ episode_number: k + 1, name: 'Ep', air_date: '2020-01-01' }));
function apiGet(path) { return later(50, { episodes: EPS }); }
%(fns)s
(async () => {
  MD._epSeasons = [{ season_number: 1, name: 'Season 1', episode_count: 10 }];
  _mdRenderSeasons();
  seasons[1].cb.checked = true;
  await window._mdToggleSeasonAll(1, true);   // ticks and opens it
  await window._mdToggleSeason(1);            // the user closes it
  seasons[1].cb.checked = false;
  await window._mdToggleSeasonAll(1, false);  // and unticks it while closed
  const boxes = el('mdEpList1').boxes;
  process.stdout.write(JSON.stringify({ season: seasons[1].cb.checked, ticked: boxes.filter(b => b.checked).length,
                                        open: el('mdEpList1').classList.contains('open') }));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardManageEpisodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fns = "\n".join(_pages_fn(n) for n in ("_resetEpisodePicker", "_imgUrl", "showManageEpisodesModal", "_epSourceBadges",
                                               "toggleSeasonAccordion", "_renderEpisodes", "toggleSeasonAll",
                                               "updateSeasonCheckbox", "_updateSeasonStatus"))
        cls.out = _run(MANAGE % {"fns": fns, "list": json.dumps("ep-list-")})

    def test_download_mores_auto_download_toggle_is_hidden(self):
        self.assertEqual("none", self.out["wrap"])

    def test_untick_of_a_closed_season_opens_it(self):
        self.assertEqual({"checked": True, "disabled": False, "open": False}, self.out["before"])
        self.assertEqual({"season": False, "ticked": 0, "open": True}, self.out["after"],
                         "the season was unticked without being shown")

    def test_tick_of_a_closed_season_still_opens_and_ticks_every_episode(self):
        self.assertEqual({"season": True, "ticked": 6, "open": True}, self.out["tick2"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JellyfinWebUntick(unittest.TestCase):
    def test_untick_of_a_closed_season_opens_it(self):
        fns = _plugin_block("  function _mdRenderSeasons()", "\n  window._mdEpSelectAll")
        fns = fns[:-len("\n  window._mdEpSelectAll")]
        out = _run(PLUGIN_PAGE % {"fns": fns, "list": json.dumps("mdEpList")})
        self.assertEqual({"season": False, "ticked": 0, "open": True}, out)


class SeasonBoxSize(unittest.TestCase):
    def _size(self, css, selector):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, selector)
        return [int(v) for v in re.findall(r"(?:width|height):\s*(\d+)px", m.group(1))]

    def test_dashboard_season_box(self):
        sizes = self._size(INDEX, ".ep-picker-season-check")
        self.assertTrue(sizes and all(v >= 18 for v in sizes), sizes)

    def test_jellyfin_web_season_box(self):
        sizes = self._size(PLUGIN_CSS, '.md-ep-season-hdr input[type="checkbox"]')
        self.assertTrue(sizes and all(v >= 18 for v in sizes), sizes)


if __name__ == "__main__":
    unittest.main()
