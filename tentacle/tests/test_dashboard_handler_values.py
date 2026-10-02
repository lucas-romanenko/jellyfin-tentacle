"""Values in the dashboard's click handlers arrive exactly as stored.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Inline handlers are built as onclick="f('${escapeJS(value)}')". The browser
decodes character references in an attribute before the JS runs, and
escapeJS() did not escape & < > or line breaks, so a title containing the
text "&#39;" arrived cut short (the rest was read as code) and one with a line
break stopped the button working (SyntaxError). Several handlers used
escapeAttr() or a hand-made backslash escape instead, and a playlist name
with a backslash before an apostrophe broke its sort menu the same way.

escapeJS() now escapes those too, and every string in an inline handler goes
through it. The tests run the real helpers and producers under node, decode
the markup as a browser does, run the handler and compare the argument with
the stored value.
"""
import json
import random
import re
import unittest

from dashboard_js import HAVE_NODE, JS, VALUES, Page, functions, handler_call, node

HELPERS = functions("pages.js", ["escapeAttr", "escapeJS"])


def _norm(s):  # escapeJS turns curly quotes into straight ones (Lucas's choice)
    return s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class HandlerValues(unittest.TestCase):
    def roundtrip(self, values):
        escaped = node(HELPERS + "process.stdout.write(JSON.stringify(%s.map(escapeJS)));" % json.dumps(values))
        for v, e in zip(values, escaped):
            page = Page(f"<button onclick=\"f('{e}')\">x</button>")
            self.assertEqual(1, len(page.elements), repr(v))
            calls = handler_call(page.attrs("onclick")[0], ["f"])
            self.assertEqual([["f", _norm(v)]], calls, repr(v))

    def test_ordinary_values(self):
        self.roundtrip(VALUES)

    def test_random_values(self):
        """Property: 1,000 random strings over quotes, &, <, >, backslash and line breaks."""
        alphabet = ["'", '"', "&", "#", "3", "9", ";", "<", ">", "x", "\\", "\n", "\r", "(", ")", "/",
                    " ", " ", " ", "`", "a", "m", "p", "q", "u", "o", "t", "$", "{", "}", "é"]
        values = []
        for seed in range(1, 1001):
            rnd = random.Random(seed)
            values.append("".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 16))))
        escaped = node(HELPERS + "process.stdout.write(JSON.stringify(%s.map(escapeJS)));" % json.dumps(values))
        # All of them in one node run: decode each attribute here, evaluate there.
        decoded = [Page(f"<b onclick=\"f('{e}')\"></b>").attrs("onclick")[0] for e in escaped]
        script = ("const out = []; const src = %s;\n"
                  "for (const js of src) { let got = null; const f = (a) => { got = a; };\n"
                  "  try { new Function('f', js)(f); } catch (e) { got = {error: String(e)}; } out.push(got); }\n"
                  "process.stdout.write(JSON.stringify(out));") % json.dumps(decoded)
        got = node(script)
        for seed, (v, g) in enumerate(zip(values, got), 1):
            self.assertEqual(_norm(v), g, f"seed {seed}: {v!r}")

    def test_the_playlist_sort_menu(self):
        src = (HELPERS + "const _LOCKED_SORT_PLAYLISTS = []; const _smartlistSortCache = {};\n"
               + functions("pages.js", ["_sortDropdown"]))
        for name in VALUES:
            html = node(src + "process.stdout.write(JSON.stringify(_sortDropdown(%s)));" % json.dumps(name))
            page = Page(html)
            calls = handler_call(page.attrs("onchange")[0], ["setPlaylistSort"])
            self.assertEqual("setPlaylistSort", calls[0][0], repr(name))
            self.assertEqual(_norm(name), calls[0][1], repr(name))

    def test_the_artist_picture_fallback_letter(self):
        src = HELPERS + functions("music.js", ["_musicArtistCard"])
        for name in ["&Co", "'Til Tuesday", "<Bracket>", "\"Quoted\"", "Émile", "😀 Emoji"]:
            html = node(src + "process.stdout.write(JSON.stringify(_musicArtistCard(%s)));"
                        % json.dumps({"name": name, "mbid": "m1", "picture": "https://img.test/p.jpg"}))
            img = [a for t, a in Page(html).elements if t == "img"][0]
            # onerror="this.outerHTML='...'" -> the markup it puts in place
            code = img["onerror"]
            got = node("const self = {}; (function () { %s }).call(self); process.stdout.write(JSON.stringify(self.outerHTML));"
                       % code)
            self.assertEqual(name[0], Page(got).text, repr(name))

    def test_the_library_source_pills(self):
        """The pill's label and the tag its click passes are both the tag as stored."""
        from dashboard_js import render
        src = HELPERS + "const pages = {lib: {sourceTag: null}};\n" + functions("pages.js", ["renderSourcePills"])
        tags = [v for v in VALUES if "\r" not in v and "\u2028" not in v]
        html = render(src, "", "renderSourcePills(%s)" % json.dumps({t: i + 1 for i, t in enumerate(tags)}))
        page = Page(html["lib-source-pills"][-1])
        for t in tags:
            self.assertIn(" ".join(t.split()), " ".join(page.text.split()))
        passed = [handler_call(js, ["filterByTag"])[0][1] for js in page.attrs("onclick")[1:]]
        self.assertEqual(sorted(_norm(t) for t in tags), sorted(passed))


class EveryHandlerStringIsEscaped(unittest.TestCase):
    """Every '${...}' string inside an on*="..." attribute goes through escapeJS()."""

    def test_source(self):
        attr = re.compile(r'\bon[a-z]+="([^"]*)"')
        item = re.compile(r"'\$\{((?:[^{}]|\{[^{}]*\})*)\}'")
        bad = []
        for f in ("pages.js", "app.js", "music.js"):
            src = (JS / f).read_text(encoding="utf-8")
            for m in attr.finditer(src):
                for e in item.findall(m.group(1)):
                    if not e.strip().startswith("escapeJS("):
                        bad.append(f"{f}:{src.count(chr(10), 0, m.start()) + 1}: {e}")
        self.assertEqual([], bad)


if __name__ == "__main__":
    unittest.main()
