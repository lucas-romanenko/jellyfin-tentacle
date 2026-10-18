"""The media bar's backdrop always belongs to the item it shows.

Run from the tentacle/ directory:  python -m unittest discover -s tests

updateBackdrop() starts loading the new item's backdrop and swaps it in when
the image loads (or after 300 ms, whichever first). Every call kept its swap
armed: on a slow server the backdrop of an item the user had already moved
past (Next pressed twice) could finish loading after the current one and
paint over it, so the picture belonged to one item and the title, overview
and buttons to another. Each call also swapped twice (300 ms fallback, then
the load event), restarting the crossfade. An item without a backdrop could
get the previous item's picture back from a crossfade still running.

Now only the newest call swaps, once.

Runs the real script under node with a fake document, Image and timers.
Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-mediabar.js"

HARNESS = r"""
var timers = [], images = [], swaps = 0;
function setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; }
function clearTimeout(id) { if (timers[id - 1]) timers[id - 1].fn = null; }
function setInterval() { return 0; }
function clearInterval() {}
function runTimers() { var t = timers; timers = []; t.forEach(function (x) { if (x.fn) x.fn(); }); }
function Image() { var me = this; images.push(me); this.complete = false; }
function layer(name) {
  return { style: { backgroundImage: '' }, offsetWidth: 1,
           classList: { set: {}, add: function (c) { this.set[c] = true; if (name === 'next' && c === 'active') swaps++; },
                        remove: function (c) { delete this.set[c]; }, contains: function (c) { return !!this.set[c]; } } };
}
var cur = layer('current'), nxt = layer('next');
var document = { readyState: 'complete', addEventListener: function () {}, createElement: function () { return {}; } };
var window = { addEventListener: function () {} };
%SCRIPT%
var bar = window.TentacleMediaBar;
bar.container = { querySelector: function (sel) { return sel.indexOf('current') !== -1 ? cur : nxt; } };
bar.preloadAdjacent = function () {};
function load(i) { images[i].complete = true; images[i].onload(); }
function shown() { return nxt.classList.contains('active') ? nxt.style.backgroundImage : cur.style.backgroundImage; }
var out = {};

// 1. A (slow) then B; B loads, A loads late.
bar.updateBackdrop('A'); bar.updateBackdrop('B');
images[1].src; load(1); runTimers(); runTimers();
load(0); runTimers(); runTimers();
out.lateEarlierImage = { shown: shown(), current: cur.style.backgroundImage };

// 2. One call: the 300 ms fallback fires, then the image loads.
swaps = 0; images = []; timers = [];
bar.updateBackdrop('C');
runTimers();            // 300 ms fallback (image not loaded) -> swap
load(0); runTimers(); runTimers();
out.singleCallSwaps = swaps; out.singleShown = shown();

// 3. An item with no backdrop after one whose crossfade is still running.
images = []; timers = [];
bar.updateBackdrop('D'); load(0);   // swap started, 500 ms crossfade timer pending
bar.updateBackdrop(null);
runTimers(); runTimers();
out.noBackdrop = { shown: shown(), current: cur.style.backgroundImage };

// 4. Ordinary use: one call, image loads -> shown.
images = []; timers = [];
bar.updateBackdrop('E'); load(0); runTimers(); runTimers();
out.ordinary = shown();
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BackdropCurrentItem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = JS.read_text(encoding="utf-8")
        out = subprocess.run(["node", "-e", HARNESS.replace("%SCRIPT%", script)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_an_earlier_items_image_loading_late_does_not_replace_the_current_one(self):
        self.assertEqual("url('B')", self.r["lateEarlierImage"]["shown"])
        self.assertEqual("url('B')", self.r["lateEarlierImage"]["current"])

    def test_one_call_swaps_once(self):
        self.assertEqual(1, self.r["singleCallSwaps"], "the crossfade restarts when the image loads after 300 ms")
        self.assertEqual("url('C')", self.r["singleShown"])

    def test_an_item_without_a_backdrop_shows_none(self):
        self.assertEqual("", self.r["noBackdrop"]["shown"])
        self.assertEqual("", self.r["noBackdrop"]["current"])

    def test_an_ordinary_load_shows_the_image(self):
        self.assertEqual("url('E')", self.r["ordinary"])


if __name__ == "__main__":
    unittest.main()
