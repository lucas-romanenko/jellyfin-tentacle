"""The media bar shows an item's Overview as plain text.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-mediabar.js turned the Overview into text by assigning it to the
innerHTML of an element made with document.createElement. That element belongs
to the page's own document, so the browser parses the markup as live HTML even
though the element is never attached (an <img> in it is fetched, for example).
Overview text comes from metadata (NFO plots, TMDB, anyone allowed to edit
metadata), so it is read in an inert document (DOMParser) instead, like the
other places that show it escape it. The text shown is the same for normal
markup.

Runs the real script under node with a fake document: the page's document
records any markup handed to it, DOMParser strips tags the way an inert
document does. Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-mediabar.js"

HARNESS = r"""
var liveMarkup = [], parseTypes = [];
function el() {
  var e = { textContent: '', src: '', alt: '', style: {},
            classList: { add: function () {}, remove: function () {} } };
  var html = '';
  Object.defineProperty(e, 'innerHTML', {
    get: function () { return html; },
    set: function (v) { html = v; if (/</.test(v)) liveMarkup.push(v); }
  });
  return e;
}
var document = { readyState: 'complete', addEventListener: function () {},
                 createElement: function () { return el(); } };
function DOMParser() {}
DOMParser.prototype.parseFromString = function (s, type) {
  parseTypes.push(type);
  return { body: { textContent: s.replace(/<[^>]*>/g, '') } };
};
var window = { addEventListener: function () {} };
function setTimeout() { return 0; }
function clearTimeout() {}
%SCRIPT%
var bar = window.TentacleMediaBar;
var els = {};
bar.container = { querySelector: function (sel) { return els[sel] || (els[sel] = el()); } };
bar.stopTrailer = bar.updateBackdrop = bar.updateActiveDot = function () {};
bar.getBackdropUrl = bar.getLogoUrl = function () { return null; };
bar._trailerPreview = false;
bar.items = [{ Id: '1', Name: 'Film', Overview: 'Fun.<img src="x"> <b>Bold</b> end' }];
bar.currentIndex = 0;
bar.updateDisplay();
var first = els['.moonfin-mediabar-overview'].textContent;
bar.items = [{ Id: '2', Name: 'Other' }];
bar.updateDisplay();
process.stdout.write(JSON.stringify({
  overview: first,
  cleared: els['.moonfin-mediabar-overview'].textContent,
  liveMarkup: liveMarkup,
  parseTypes: parseTypes
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class MediaBarOverviewText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = JS.read_text(encoding="utf-8")
        out = subprocess.run(["node", "-e", HARNESS.replace("%SCRIPT%", script)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_the_overview_markup_never_reaches_the_page_document(self):
        self.assertEqual([], self.r["liveMarkup"],
                         "the Overview was parsed as HTML in the page's own document")

    def test_the_overview_is_shown_as_its_text(self):
        self.assertEqual("Fun. Bold end", self.r["overview"])

    def test_the_overview_is_read_as_html(self):
        # Another type (text/xml, image/svg+xml) parses differently: entities,
        # tags and errors would not come out as the Overview's plain text.
        self.assertEqual(["text/html"], self.r["parseTypes"])

    def test_an_item_without_overview_clears_it(self):
        self.assertEqual("", self.r["cleared"])


if __name__ == "__main__":
    unittest.main()
