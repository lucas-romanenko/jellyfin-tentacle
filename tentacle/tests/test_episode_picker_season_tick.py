"""Episode picker: ticking a closed season's box ticks every episode and stays ticked (#916).

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_episode_picker_season_tick.py"

Dashboard (pages.js: Add → Pick Episodes…, Download Remaining Episodes):
toggleSeasonAll opens the season, and rendering its episodes
(_updateSeasonStatus) set the season box to "whole season already owned",
false, over the user's tick. Jellyfin web (tentacle-discover.js
_mdToggleSeasonAll) ticked the episodes after a fixed 100 ms, while a TMDB
season answer usually takes longer: nothing got ticked and the box went off.

A season whose aired episodes are all VOD starts with its box ticked and
disabled (_renderSeasonsWithVod counts aired episodes only); opening it keeps
it that way instead of counting the not-yet-aired ones.

Runs the real functions under node with a page stub that keeps each
checkbox's checked/disabled state, as a browser does.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = (ROOT / "tentacle-plugin" / "Inject" / "tentacle-discover.js").read_text(encoding="utf-8")
PAGES = (ROOT / "tentacle" / "static" / "js" / "pages.js").read_text(encoding="utf-8")

# Elements by id; markup written into a season list or the season container is
# turned into checkbox objects that keep their state, as in a browser.
DOM = r"""
const els = {};
const seasons = {};   // sn -> { cb, count }
function cls() { const s = new Set(); return { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c) }; }
function inputs(html) {
  return [...html.matchAll(/<input type="checkbox"([^>]*)>/g)].map(m => ({
    checked: /\schecked(\s|$)/.test(m[1]), disabled: /\sdisabled(\s|$)/.test(m[1]) }));
}
function el(id) {
  if (!els[id]) {
    let html = '';
    const e = { id, style: {}, classList: cls(), boxes: [] };
    Object.defineProperty(e, 'innerHTML', { get: () => html, set(v) {
      html = String(v);
      if (/^(ep-list-|mdEpList)/.test(id)) e.boxes = inputs(html);
      if (id === 'episode-picker-seasons' || id === 'mdEpSeasons') {
        for (const m of html.matchAll(/data-season="(\d+)">\s*<div[^>]*>[\s\S]*?(<input type="checkbox"[^>]*>)[\s\S]*?<span class="((?:md-)?ep-count[^"]*)">([^<]*)</g)) {
          seasons[m[1]] = { cb: inputs(m[2])[0], count: { className: m[3], textContent: m[4] } };
        }
      }
    } });
    e.querySelectorAll = sel => sel.includes(':not(:disabled)') ? e.boxes.filter(b => !b.disabled) : e.boxes;
    els[id] = e;
  }
  return els[id];
}
function seasonEl(sn) {
  const s = seasons[sn];
  return s && { querySelector: sel => /count/.test(sel) ? s.count : s.cb };
}
const document = {
  getElementById: el,
  querySelector(sel) {
    const m = /^\.(?:md-ep-season|ep-picker-season)\[data-season="(\d+)"\](.*)$/.exec(sel);
    if (!m) return null;
    return m[2].trim() ? (seasons[m[1]] || {}).cb : seasonEl(m[1]);
  },
  querySelectorAll() { return []; },
};
const EPISODES = Array.from({ length: 10 }, (_, k) => ({ episode_number: k + 1, name: 'Ep ' + (k + 1), air_date: '2020-01-01' }));
const later = (ms, v) => new Promise(r => setTimeout(() => r(v), ms));
// The user ticks a season's box: the browser flips it, then runs its onclick.
async function tickSeason(sn, handler) {
  seasons[sn].cb.checked = true;
  await handler(sn, true);
  await later(600);
  const boxes = el(%(list)s + sn).boxes;
  return { season: seasons[sn].cb, ticked: boxes.filter(b => b.checked).length, total: boxes.length };
}
"""

DASHBOARD = DOM + r"""
let _epPickerToken = 1, _epPickerLoaded = {}, _vodEpisodes = {}, _dlEpisodes = {}, _unairedPerSeason = {};
let _epPickerSeasons = [], _downloadMoreTmdbId = null, _addArrTmdbId = 100, _addArrTvdbId = null;
function escapeAttr(s) { return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function api(path) { return later(300, { episodes: EPISODES }); }   // a TMDB season answer, slower than 100 ms
%(fns)s
(async () => {
  const out = {};
  _epPickerSeasons = [{ season_number: 1, name: 'Season 1', episode_count: 10 }];
  _renderSeasons();
  out.pick = await tickSeason(1, toggleSeasonAll);

  // Download Remaining Episodes: S2 has 2 VOD episodes, S3 has its 10 aired ones on VOD and 2 still to air.
  for (const k in seasons) delete seasons[k];
  _epPickerLoaded = {};
  _vodEpisodes = { '2': [1, 2], '3': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] };
  _unairedPerSeason = { 3: 2 };
  _epPickerSeasons = [{ season_number: 2, episode_count: 10 }, { season_number: 3, episode_count: 12 }];
  _renderSeasonsWithVod();
  out.remaining = await tickSeason(2, toggleSeasonAll);
  out.vodBefore = { ...seasons[3].cb };
  const twelve = EPISODES.concat([{ episode_number: 11 }, { episode_number: 12 }]);
  api = () => later(300, { episodes: twelve });
  await toggleSeasonAccordion(3);
  out.vodAfter = { ...seasons[3].cb };
  out.vodCount = seasons[3].count.textContent;
  process.stdout.write(JSON.stringify(out));
})();
"""

PLUGIN_PAGE = DOM + r"""
const window = {};
const MD = { _epLoaded: {}, _vodEpisodes: {}, _dlEpisodes: {}, _currentItem: { tmdb_id: 100 } };
function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function apiGet(path) { return later(300, { episodes: EPISODES }); }   // a TMDB season answer, slower than 100 ms
%(fns)s
(async () => {
  const out = {};
  MD._epSeasons = [{ season_number: 1, name: 'Season 1', episode_count: 10 }];
  _mdRenderSeasons();
  out.pick = await tickSeason(1, window._mdToggleSeasonAll);
  for (const k in seasons) delete seasons[k];
  MD._epLoaded = {};
  MD._vodEpisodes = { '2': [1, 2] };
  MD._epSeasons = [{ season_number: 2, episode_count: 10 }];
  _mdRenderSeasons();
  out.remaining = await tickSeason(2, window._mdToggleSeasonAll);
  process.stdout.write(JSON.stringify(out));
})();
"""


def _pages_fn(name):
    m = re.search(r"^(async )?function %s\(" % re.escape(name), PAGES, re.M)
    return PAGES[m.start():PAGES.index("\n}\n", m.start()) + 2]


def _plugin_block(start, end_marker):
    i = PLUGIN.index(start)
    return PLUGIN[i:PLUGIN.index(end_marker, i) + len(end_marker)]


def _run(script):
    run = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardSeasonTick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fns = "\n".join(_pages_fn(n) for n in ("_renderSeasons", "_renderSeasonsWithVod", "toggleSeasonAccordion",
                                               "_renderEpisodes", "toggleSeasonAll", "updateSeasonCheckbox",
                                               "_updateSeasonStatus"))
        cls.out = _run(DASHBOARD % {"fns": fns, "list": json.dumps("ep-list-")})

    def test_pick_episodes_season_tick_stays_and_ticks_every_episode(self):
        pick = self.out["pick"]
        self.assertEqual((10, 10), (pick["ticked"], pick["total"]))
        self.assertTrue(pick["season"]["checked"], "the season box turned itself off")

    def test_download_remaining_season_tick_stays(self):
        rem = self.out["remaining"]
        self.assertEqual((10, 10), (rem["ticked"], rem["total"]))
        self.assertTrue(rem["season"]["checked"], "the season box turned itself off")
        self.assertFalse(rem["season"]["disabled"])

    def test_season_with_every_aired_episode_on_vod_stays_owned_when_opened(self):
        self.assertEqual({"checked": True, "disabled": True}, self.out["vodBefore"])
        self.assertEqual({"checked": True, "disabled": True}, self.out["vodAfter"],
                         "opening the season counted episodes that haven't aired")
        self.assertEqual("10/10", self.out["vodCount"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JellyfinWebSeasonTick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fns = _plugin_block("  function _mdRenderSeasons()", "\n  window._mdEpSelectAll")
        fns = fns[:-len("\n  window._mdEpSelectAll")]
        cls.out = _run(PLUGIN_PAGE % {"fns": fns, "list": json.dumps("mdEpList")})

    def test_season_tick_ticks_every_episode_when_the_season_loads_slowly(self):
        pick = self.out["pick"]
        self.assertEqual((10, 10), (pick["ticked"], pick["total"]), "nothing got ticked")
        self.assertTrue(pick["season"]["checked"], "the season box turned itself off")

    def test_season_tick_with_some_episodes_on_vod(self):
        rem = self.out["remaining"]
        self.assertEqual((10, 10), (rem["ticked"], rem["total"]))
        self.assertTrue(rem["season"]["checked"])


if __name__ == "__main__":
    unittest.main()
