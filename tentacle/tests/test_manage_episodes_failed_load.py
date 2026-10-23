"""Manage Episodes: Save can't send an empty selection for an episode list that never loaded.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_manage_episodes_failed_load.py"

Manage Episodes (Jellyfin web: tentacle-discover.js _mdLoadManageEpisodes;
dashboard: pages.js showManageEpisodesModal / confirmManageEpisodes) shows
Save enabled while the episode list loads, and leaves it enabled when the
load fails ("Failed to load": the plugin's 15 s timeout on a big Sonarr, a
dropped connection) or comes back empty (GET /api/discover/sonarr-episodes
answers in_sonarr: true with episodes: [] when Sonarr's episode read
fails). Save then posts selected_episodes: [] and POST
/api/discover/manage-episodes, which reads Sonarr itself, unmonitors every
episode of the show that isn't selected: all of them.

Choosing no episodes on purpose (untick everything, Save) still has to work.

Runs the real functions under node with a page stub whose Save click does
nothing while the button is disabled, as in a browser.
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

EPISODES = [{"id": 10 + n, "seasonNumber": 1, "episodeNumber": n, "title": f"Ep {n}", "monitored": n < 3,
             "hasFile": False} for n in (1, 2, 3)]
LOADS = {
    "fails": "Promise.reject(new Error('Request timed out'))",
    "slow": "new Promise(() => {})",
    "empty": "Promise.resolve({ in_sonarr: true, sonarr_id: 5, episodes: [] })",
    "ok": "Promise.resolve({ in_sonarr: true, sonarr_id: 5, episodes: %s })" % json.dumps(EPISODES),
}

# Elements on demand. querySelectorAll() answers the episode checkboxes as the
# browser would, from the markup written into the episode lists (`untickAll`:
# the user unticked every box).
DOM = r"""
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: '', textContent: '', className: '', style: {}, dataset: {}, disabled: false,
    parentElement: { style: {} }, _click: [], addEventListener(t, f) { if (t === 'click') this._click.push(f); },
    setAttribute(k, v) { this[k] = v; } };
  return els[id];
}
let untickAll = false;
function boxes(prefix) {
  const out = [];
  for (const [id, e] of Object.entries(els)) {
    if (!id.startsWith(prefix)) continue;
    for (const m of e.innerHTML.matchAll(/<input type="checkbox" ([^>]*)>/g)) {
      const a = m[1], d = /data-season="(\d+)" data-episode="(\d+)"/.exec(a);
      if (d && !/\bdisabled\b/.test(a)) out.push({ checked: /\bchecked\b/.test(a) && !untickAll,
                                               dataset: { season: d[1], episode: d[2] } });
    }
  }
  return out;
}
const document = {
  getElementById: el,
  querySelectorAll(sel) {
    const all = sel.includes('.md-ep-row') ? boxes('mdEpList') : sel.includes('.ep-picker-ep') ? boxes('ep-list-') : [];
    return sel.includes(':checked') ? all.filter(b => b.checked) : all;
  },
};
const flush = async () => { for (let k = 0; k < 10; k++) await new Promise(r => setImmediate(r)); };
const posts = [];
"""


def _plugin_fn(name):
    m = re.search(r"^  function %s\(" % re.escape(name), PLUGIN, re.M)
    return PLUGIN[m.start():PLUGIN.index("\n  }\n", m.start()) + 4]


def _pages_fn(name):
    m = re.search(r"^(async )?function %s\(" % re.escape(name), PAGES, re.M)
    return PAGES[m.start():PAGES.index("\n}\n", m.start()) + 2]


PLUGIN_PAGE = DOM + r"""
const window = { ApiClient: { getCurrentUserId: () => 'u1' } };
const MD = {};
function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function apiGet(path) { return %(load)s; }
function apiPost(path, body) { posts.push([path, body]); return Promise.resolve({ success: true, monitored: 0 }); }
%(fns)s
(async () => {
  _mdLoadManageEpisodes({ tmdb_id: 100 });
  await flush();
  untickAll = %(untick)s;
  const save = el('mdManageSaveBtn');
  if (!save.disabled) save._click.forEach(f => f());   // a disabled button ignores the click
  await flush();
  process.stdout.write(JSON.stringify({ posts, list: el('mdEpSeasons').innerHTML }));
})();
"""

DASHBOARD_PAGE = DOM + r"""
let _manageTmdbId = null, _epPickerToken = 0, _epPickerLoaded = {};
function _resetEpisodePicker() { _epPickerLoaded = {}; return ++_epPickerToken; }
function _imgUrl() { return ''; }
function escapeAttr(s) { return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function showModal() {}
function closeModal() {}
function toast() {}
function api(path, opts) {
  if (opts && opts.method === 'POST') { posts.push([path, opts.body]); return Promise.resolve({ success: true, monitored: 0 }); }
  return %(load)s;
}
%(fns)s
(async () => {
  showManageEpisodesModal(100, 'Show', '2020', '');
  await flush();
  untickAll = %(untick)s;
  const save = el('add-radarr-confirm-btn');
  if (!save.disabled) await confirmManageEpisodes();   // onclick; a disabled button ignores the click
  await flush();
  process.stdout.write(JSON.stringify({ posts, list: el('episode-picker-seasons').innerHTML }));
})();
"""


def _run(script):
    run = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


class _Cases:
    def test_save_after_the_episode_list_failed_to_load_sends_nothing(self):
        out = self.save("fails")
        self.assertEqual([], out["posts"], "an empty selection was posted for a list that never loaded")

    def test_save_while_the_episode_list_is_still_loading_sends_nothing(self):
        self.assertEqual([], self.save("slow")["posts"])

    def test_save_after_an_empty_episode_answer_sends_nothing(self):
        self.assertEqual([], self.save("empty")["posts"])

    def test_save_after_a_good_load_sends_the_ticked_episodes(self):
        posts = self.save("ok")["posts"]
        self.assertEqual(1, len(posts))
        self.assertEqual([{"season": 1, "episode": 1}, {"season": 1, "episode": 2}], posts[0][1]["selected_episodes"])

    def test_unticking_every_episode_on_purpose_still_saves(self):
        posts = self.save("ok", untick=True)["posts"]
        self.assertEqual(1, len(posts))
        self.assertEqual([], posts[0][1]["selected_episodes"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JellyfinWebManageEpisodes(_Cases, unittest.TestCase):
    def save(self, load, untick=False):
        fns = "\n".join(_plugin_fn(n) for n in ("_mdGetSelectedEpisodes", "_mdLoadManageEpisodes"))
        return _run(PLUGIN_PAGE % {"load": LOADS[load], "fns": fns, "untick": json.dumps(untick)})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardManageEpisodes(_Cases, unittest.TestCase):
    def save(self, load, untick=False):
        fns = "\n".join(_pages_fn(n) for n in ("_getSelectedEpisodes", "showManageEpisodesModal",
                                               "confirmManageEpisodes"))
        return _run(DASHBOARD_PAGE % {"load": LOADS[load], "fns": fns, "untick": json.dumps(untick)})


if __name__ == "__main__":
    unittest.main()
