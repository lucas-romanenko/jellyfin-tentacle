"""Dashboard Activity cards open the title they show (#143).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Every card on the Activity tab (Downloading, Searching, Recently Downloaded,
Coming up, Upcoming) had a hover border but no handler, so clicking did
nothing. Runs the real _actOpenAttrs() and _activityCardOpen() under node.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")


def _fn(name):
    start = PAGES.index(f"function {name}(")
    return PAGES[start:PAGES.index("\n}\n", start) + 2]


SCRIPT = """
const opened = [];
function showDiscoverDetail(...args) { opened.push([args[0], args[1], args[6]]); }
%s
%s
// A card element, built from the attributes the template writes.
function card(item) {
  const attrs = _actOpenAttrs(item);
  const m = attrs.match(/data-open="([^"]*)"/);
  const el = { dataset: m ? { open: m[1] } : {}, attrs,
               closest(sel) { return sel.startsWith('.activity-card') && m ? el : null; } };
  return el;
}
function click(target) {
  let prevented = false;
  _activityCardOpen({ type: 'click', target, preventDefault() { prevented = true; } });
  return prevented;
}
function button(inside) {
  return { closest(sel) { return sel === 'button, a, input, label' ? this : (sel.startsWith('.activity-card') ? inside : null); } };
}
const out = {};
const movie = card({ tmdb_id: 603, media_type: 'movie' });
const show = card({ tmdb_id: 0, tvdb_id: 81189, media_type: 'series' });
const tvdbMovie = card({ tmdb_id: 0, tvdb_id: 5, media_type: 'movie' });
out.role = movie.attrs.includes('role="button" tabindex="0"');
out.tvdb_movie_attrs = tvdbMovie.attrs;
click(movie); click(show);
click(button(movie));   // the card's own Search again / Remove button
_activityCardOpen({ type: 'keydown', key: 'Enter', target: movie, preventDefault() {} });
_activityCardOpen({ type: 'keydown', key: 'a', target: movie, preventDefault() {} });
_activityCardOpen({ type: 'keydown', key: ' ', target: button(movie), preventDefault() {} });
out.opened = opened;
process.stdout.write(JSON.stringify(out));
"""


class ActivityCardsOpen(unittest.TestCase):
    def setUp(self):
        start = PAGES.index("function renderActivity(data) {")
        self.render = PAGES[start:PAGES.index("// ── DISCOVER PAGE", start)]

    def test_every_card_is_openable(self):
        cards = re.findall(r'<div class="activity-card"([^>]*)>', self.render)
        self.assertEqual(5, len(cards), "one per section")
        for attrs in cards:
            self.assertRegex(attrs, r"\$\{_actOpenAttrs\((dl|item)\)\}")
        self.assertIn("content.addEventListener('click', _activityCardOpen);", self.render)
        self.assertIn("content.addEventListener('keydown', _activityCardOpen);", self.render)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_click_and_keyboard_open_the_title_and_buttons_keep_theirs(self):
        run = subprocess.run(["node", "-e", SCRIPT % (_fn("_actOpenAttrs"), _fn("_activityCardOpen"))],
                             capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        out = json.loads(run.stdout)
        self.assertTrue(out["role"])
        # Clicked movie, clicked TVDB-only show, Enter on the movie: nothing else.
        self.assertEqual([[603, "movie", 0], [0, "series", 81189], [603, "movie", 0]], out["opened"])
        self.assertEqual("", out["tvdb_movie_attrs"], "a movie has no TVDB detail to open")


if __name__ == "__main__":
    unittest.main()
