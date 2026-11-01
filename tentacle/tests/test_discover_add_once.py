"""Discover (Jellyfin web): one tap on Add to Radarr / Sonarr sends one add (#896).

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_discover_add_once.py"

loadDownloadOptions() (tentacle-discover.js) looked the button up by id when
the quality profiles arrived and added one more click listener each time.
When the dialog was drawn twice (a second showDetailModal before the first
profiles answer came back), both profile loads bound to the button on
screen, so one tap posted the add twice, 20 ms apart: Radarr added the film
on the first and failed the second on its unique index, and that error is
what the dialog showed. Each load now binds only its own dialog's button,
and doDownload() sends nothing while an add is already on its way.

Runs the real functions under node with a page stub whose click does
nothing while the button is disabled, as in a browser.
"""
import json
import re
import unittest

from inject_js import HAVE_NODE, INJECT, node

SRC = (INJECT / "tentacle-discover.js").read_text(encoding="utf-8")


def fn(name):
    m = re.search(r"^  function %s\(" % re.escape(name), SRC, re.M)
    end = SRC.index("\n  }\n", m.start()) + 4
    return SRC[m.start():end]


# A page holding one dialog at a time: draw() replaces it, as renderModal()
# does (old one removed, new one appended), so getElementById answers the
# newest dialog's elements.
PAGE = r"""
let page = {}, posts = [], answers = [];
function mkEl(id) {
  return { id, innerHTML: '', textContent: '', style: {}, disabled: false, isConnected: true, value: '',
    _click: [], onclick: null,
    addEventListener(t, f) { if (t === 'click') this._click.push(f); },
    click() { if (this.disabled) return; if (this.onclick) this.onclick({}); this._click.forEach(f => f({})); } };
}
function draw() {
  for (const e of Object.values(page)) e.isConnected = false;
  page = { mdDownloadBtn: mkEl('mdDownloadBtn'), mdProfileSelect: mkEl('mdProfileSelect'),
           mdDownloadStatus: mkEl('mdDownloadStatus') };
}
var document = { getElementById: id => page[id] || null };
var MD = {};
var window = { ApiClient: { getCurrentUserId: () => 'u1' } };
let profileWaits = [];
function apiGet(url) { return new Promise(res => profileWaits.push(() => res([{ id: 4, name: 'HD-1080p' }]))); }
function apiPost(url, body) { posts.push(url); return new Promise(res => answers.push(res)); }
function esc(s) { return String(s); }
function _mdGetSelectedEpisodes() { return []; }
const tick = () => new Promise(r => setTimeout(r, 0));
"""

ITEM = {"title": "Film", "media_type": "movie", "tmdb_id": 438631}


def run(steps):
    script = PAGE + "\n".join(fn(n) for n in ("profileOptionsHtml", "loadDownloadOptions", "doDownload")) + r"""
const item = %s;
(async () => {
%s
  process.stdout.write(JSON.stringify({ posts: posts.length, label: page.mdDownloadBtn.textContent,
    status: page.mdDownloadStatus.textContent }));
})().catch(e => { process.stderr.write(String(e.stack)); process.exit(1); });
""" % (json.dumps(ITEM), steps)
    return node(script)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class OneTapOneAdd(unittest.TestCase):
    def test_dialog_drawn_twice_before_profiles_arrive(self):
        out = run(r"""
  draw(); loadDownloadOptions(item);
  draw(); loadDownloadOptions(item);
  profileWaits.forEach(f => f()); await tick(); await tick();
  page.mdDownloadBtn.click(); await tick();
""")
        self.assertEqual(1, out["posts"])

    def test_retry_after_a_failure_sends_one_add(self):
        out = run(r"""
  draw(); loadDownloadOptions(item);
  draw(); loadDownloadOptions(item);
  profileWaits.forEach(f => f()); await tick(); await tick();
  page.mdDownloadBtn.click(); await tick();
  answers.forEach(f => f({ added: 0, already_exists: 0, failed: 1, detail: 'Radarr refused it' })); answers = [];
  await tick(); await tick();
  posts = [];
  page.mdDownloadBtn.click(); await tick();
""")
        self.assertEqual(1, out["posts"])

    def test_a_second_call_while_adding_sends_nothing(self):
        out = run(r"""
  draw(); loadDownloadOptions(item);
  profileWaits.forEach(f => f()); await tick(); await tick();
  page.mdDownloadBtn.click(); await tick();
  doDownload(item, '');
  answers.forEach(f => f({ added: 1, already_exists: 0, failed: 0 })); await tick(); await tick();
""")
        self.assertEqual(1, out["posts"])
        self.assertEqual("Added!", out["label"])

    def test_one_dialog_one_tap_still_adds(self):
        out = run(r"""
  draw(); loadDownloadOptions(item);
  profileWaits.forEach(f => f()); await tick(); await tick();
  page.mdDownloadBtn.click(); await tick();
  answers.forEach(f => f({ added: 1, already_exists: 0, failed: 0 })); await tick(); await tick();
""")
        self.assertEqual(1, out["posts"])
        self.assertIn("Added to Radarr", out["status"])


if __name__ == "__main__":
    unittest.main()
