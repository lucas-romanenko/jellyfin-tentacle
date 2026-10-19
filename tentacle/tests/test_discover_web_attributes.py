"""Discover (Jellyfin web) keeps titles, reasons and image addresses intact in attributes.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-discover.js escaped attribute values with esc(), which escapes
& < > but not quotes, and put image addresses (TMDB, Radarr and proxied
posters) into src and a CSS url() unescaped: a title, a rejection reason
or an address with a double quote was cut short at the quote. Attributes
now go through escAttr() (as tentacle-search.js does), the upcoming
backdrop is set as a style property, and trailer links and players take
https addresses on the known video hosts only (as tentacle-details.js
does). The functions run under node with a document that escapes text the
way a browser does.
"""
import json
import re
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, NAMES, Page, node

SRC = (INJECT / "tentacle-discover.js").read_text(encoding="utf-8")


def fn(name):
    m = re.search(r"^  function %s\(" % re.escape(name), SRC, re.M)
    end = SRC.index("\n  }\n", m.start()) + 4
    return SRC[m.start():end]


def helpers():
    names = ["_imgUrl", "esc"] + [n for n in ("escAttr", "safeTrailerUrl") if re.search(r"^  function %s\(" % n, SRC, re.M)]
    out = "\n".join(fn(n) if n != "_imgUrl" else SRC[SRC.index("  function _imgUrl("):SRC.index("\n  }\n", SRC.index("  function _imgUrl(")) + 4]
                    for n in names)
    if "function escAttr" not in out:  # before the change: attributes went through esc()
        out += "\nfunction escAttr(s) { return esc(s); }\nfunction safeTrailerUrl(u) { return u; }\n"
    return out


PRELUDE = ESC_DOM + r"""
var window = { location: { href: 'http://jf/web/' }, addEventListener: function () {} };
var appended = [];
document.body.appendChild = function (e) { appended.push(e); return e; };
var _parts = {};
var make = document.createElement;
document.createElement = function (t) { var e = make(t); e.querySelector = function (s) { return _parts[s] || (_parts[s] = make('div')); }; return e; };
function requestAnimationFrame() {}
var _badPosters = {};
var MD = { mediaFilter: 'movies' };
"""

ADDR = 'http://img.test/p.jpg?a=1&b="2"'


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DiscoverAttributes(unittest.TestCase):
    def test_activity_poster_address(self):
        out = node(PRELUDE + helpers() + fn("actPoster") + "process.stdout.write(JSON.stringify(actPoster(%s, 'ph')));" % json.dumps(ADDR))
        self.assertEqual([ADDR], Page(out).attrs("src"))

    def test_upcoming_modal(self):
        item = {"title": NAMES[0], "poster_path": ADDR, "trailer_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "all_dates": {}}
        script = PRELUDE + helpers() + fn("showUpcomingModal") + r"""
try { showUpcomingModal(%s); } catch (e) { process.stderr.write(String(e.stack)); }
var o = appended[0];
process.stdout.write(JSON.stringify({ html: o ? o.innerHTML : '', bg: (_parts['.md-up-backdrop'] || {style: {}}).style.backgroundImage || '' }));
""" % json.dumps(item)
        out = node(script)
        page = Page(out["html"])
        self.assertEqual([ADDR], page.attrs("src"))
        self.assertEqual(["https://www.youtube.com/watch?v=abcdefghijk"], page.attrs("href"))
        bg = out["bg"]
        self.assertTrue(bg.startswith('url("') and bg.endswith('")'), bg)
        self.assertNotIn('"2"', bg[5:-2])  # the quotes inside are percent-encoded
        self.assertEqual([], [a for a in page.attrs("style") if "img.test" in a])  # not in a style attribute

    def test_a_trailer_link_elsewhere_is_left_out(self):
        item = {"title": "Film", "poster_path": "", "trailer_url": "http://other.test/x", "all_dates": {}}
        script = PRELUDE + helpers() + fn("showUpcomingModal") + r"""
showUpcomingModal(%s);
process.stdout.write(JSON.stringify(appended[0].innerHTML));
""" % json.dumps(item)
        self.assertEqual([], Page(node(script)).attrs("href"))

    def test_trailer_player_label(self):
        for title in NAMES:
            script = PRELUDE + helpers() + ("var _trailerOverlay = null, _trailerEscHandler = null;\n"
                                            "function closeDiscoverTrailer() {}\nfunction setTimeout() {}\n") + fn("playDiscoverTrailer") + r"""
playDiscoverTrailer('https://www.youtube.com/watch?v=abcdefghijk', %s);
process.stdout.write(JSON.stringify(appended[0].innerHTML));
""" % json.dumps(title)
            labels = Page(node(script)).attrs("aria-label")
            self.assertIn(title, labels)


if __name__ == "__main__":
    unittest.main()
