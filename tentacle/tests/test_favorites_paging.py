"""More than one page of favorites must all be shown, with a true total.

tentacle-plugin/Inject/tentacle-favorites.js fetched each section (Movies,
Shows, Episodes, ...) once, with `Limit=200` and no `StartIndex`, and built both
the cards and the header counts from `data.Items.length`. A user with more than
200 favorites of one type silently saw the first 200 alphabetically, under a
header that said "200" -- no hint that anything was missing, and favorites past
"M" or so unreachable from the page.

Unlike the other injected-script tests this one RUNS the script, under node with
a stub DOM and a fake Jellyfin ApiClient that pages the way the real server does
(`StartIndex`, `Limit`, `TotalRecordCount`). It is skipped if node is absent.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path("../tentacle-plugin/Inject/tentacle-favorites.js")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, countsJson, serverMaxLimit] = process.argv.slice(1);
const counts = JSON.parse(countsJson);        // { Movie: 450, ... }
const requests = [];

function el() {
  return { innerHTML: '', textContent: '', style: {}, id: '',
           classList: { add() {}, remove() {} },
           addEventListener() {}, appendChild() {}, querySelector() { return null; },
           querySelectorAll() { return []; } };
}
const container = el();
const document = { body: el(), addEventListener() {},
                   createElement() { const e = el();
                     Object.defineProperty(e, 'innerHTML', { get() { return this.textContent; }, set(v) { this._h = v; } });
                     return e; },
                   getElementById() { return null; } };
let made = 0;
const realCreate = document.createElement;
document.createElement = function () { return made++ === 0 ? container : realCreate(); };

const ApiClient = {
  getCurrentUserId() { return 'u1'; },
  getUrl(path, q) { return path + (q ? '?' + new URLSearchParams(q) : ''); },
  getJSON(url) {
    requests.push(url);
    const q = new URL('http://x/' + url).searchParams;
    if (url.startsWith('LiveTv/')) return Promise.resolve({ Items: [], TotalRecordCount: 0 });
    const type = q.get('IncludeItemTypes');
    const total = counts[type] || 0;
    const start = parseInt(q.get('StartIndex') || '0', 10);
    let limit = parseInt(q.get('Limit') || String(total), 10);
    limit = Math.min(limit, parseInt(serverMaxLimit, 10));
    const items = [];
    for (let i = start; i < Math.min(total, start + limit); i++) {
      items.push({ Id: type + '-' + i, Name: type + ' ' + i, Type: type, ImageTags: {} });
    }
    return Promise.resolve({ Items: items, TotalRecordCount: total, StartIndex: start });
  },
};
const window = { ApiClient, addEventListener() {}, location: { hash: '#/home?tab=1' } };
const sandbox = { window, document, location: window.location, console,
                  setTimeout: (fn) => setTimeout(fn, 0), clearTimeout, Promise, URLSearchParams };
vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);

setTimeout(() => {
  const html = container.innerHTML || '';
  const ids = [...html.matchAll(/data-id="([^"]+)"/g)].map(m => m[1]);
  const header = (html.match(/tfav-count">(\d+) item/) || [])[1];
  const sections = [...html.matchAll(/tfav-section-title">([^<]+)<span class="tfav-section-count">(\d+)</g)]
    .map(m => [m[1], parseInt(m[2], 10)]);
  console.log(JSON.stringify({ cards: ids.length, unique: new Set(ids).size,
                               header: header ? parseInt(header, 10) : null,
                               sections, requests: requests.length }));
}, 300);
"""


def run(counts, server_max_limit=10_000):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    out = subprocess.run(
        [node, "-e", HARNESS, "--", str(SCRIPT), json.dumps(counts), str(server_max_limit)],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("harness failed:\n" + out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestFavoritesPaging(unittest.TestCase):
    def test_the_harness_renders_a_small_library(self):
        """Control: proves the stub DOM drives the real script."""
        r = run({"Movie": 3, "Series": 2})
        self.assertEqual(r["cards"], 5)
        self.assertEqual(r["header"], 5)

    def test_every_favorite_past_the_first_page_is_shown(self):
        r = run({"Movie": 450, "Series": 7})
        self.assertEqual(r["unique"], 457,
                         f"{r['unique']} of 457 favorites rendered: the rest are "
                         f"unreachable from the page")
        self.assertEqual(r["cards"], r["unique"], "a page was rendered twice")

    def test_the_header_and_section_counts_are_the_true_totals(self):
        r = run({"Movie": 450, "Series": 7})
        self.assertEqual(r["header"], 457)
        self.assertEqual(dict(r["sections"]), {"Movies": 450, "Shows": 7})

    def test_an_exact_multiple_of_the_page_size_terminates(self):
        r = run({"Movie": 400})
        self.assertEqual(r["unique"], 400)
        self.assertLess(r["requests"], 20, "kept asking after the last page")

    def test_a_server_that_returns_short_pages_is_still_read_to_the_end(self):
        """Termination must follow what the server reports, not assume a page
        comes back as long as was asked for."""
        r = run({"Movie": 450}, server_max_limit=100)
        self.assertEqual(r["unique"], 450)

    def test_a_small_library_costs_no_extra_requests(self):
        r = run({"Movie": 3})
        self.assertLessEqual(r["requests"], 6, "5 sections + Live TV, one request each")


if __name__ == "__main__":
    unittest.main()
