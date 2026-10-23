"""Health → Duplicates offers "Show all N" past the first five, and Resolve
All says how many it acts on (#800).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The button was added inside the list's items.map(), before the list's HTML
replaced the element, so it never showed: an admin with 13 pending saw 5 and
Resolve All resolved 13. Runs the real renderDupList() and
resolveAllKeepRadarr() from pages.js under node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js"


def _function(src, name):
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start) + 2]


SCRIPT = """
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, innerHTML: '', textContent: '', style: {},
    insertAdjacentHTML(where, html) { this.innerHTML += html; } });
}
const document = { getElementById: el };
const escapeAttr = s => String(s), escapeJS = s => String(s);
let shown = null;
function showModal(id) { shown = id; }
function closeModal() {}
const pages = { dup: { filter: 'pending', showAll: false, items: [] } };
for (let i = 1; i <= 13; i++)
  pages.dup.items.push({ id: i, tmdb_id: i, title: 'Show ' + i, media_type: 'series', resolution: 'pending',
                         sources: [{ source: 'provider_1', path: '/vod/' + i }, { source: 'sonarr', path: '/tv/' + i }] });
pages.dup.items.push({ id: 99, tmdb_id: 99, title: 'Done', media_type: 'movie', resolution: 'keep_both', sources: [] });
%s
const out = {};
renderDupList();
out.rows = (el('dup-list').innerHTML.match(/class="dup-row"/g) || []).length;
out.button = /showAllDuplicates\\(\\)">Show all 13</.test(el('dup-list').innerHTML);
showAllDuplicates();
out.rowsAll = (el('dup-list').innerHTML.match(/class="dup-row"/g) || []).length;
out.buttonAll = el('dup-list').innerHTML.includes('Show all');
resolveAllKeepRadarr();
out.message = el('resolve-dup-message').textContent;
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShowAllDuplicates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = PAGES.read_text(encoding="utf-8")
        code = "\n".join(_function(src, n) for n in ("showAllDuplicates", "renderDupList", "resolveAllKeepRadarr"))
        run = subprocess.run(["node", "-e", SCRIPT % code], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.out = json.loads(run.stdout)

    def test_first_five_and_a_show_all_button(self):
        self.assertEqual(5, self.out["rows"])
        self.assertTrue(self.out["button"], "no 'Show all 13' button under the first five")

    def test_show_all_lists_every_pending_one(self):
        self.assertEqual(13, self.out["rowsAll"])
        self.assertFalse(self.out["buttonAll"])

    def test_resolve_all_names_the_count(self):
        self.assertIn("all 13 pending", self.out["message"])


if __name__ == "__main__":
    unittest.main()
