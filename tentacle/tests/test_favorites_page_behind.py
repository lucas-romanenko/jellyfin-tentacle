"""Favorites: the page behind the rows stays hidden and still (tentacle-plugin/Inject/tentacle-favorites.css).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Favorites rows are a fixed overlay over Jellyfin's home page. The rule
meant to hide that page named `.view`, which Jellyfin 10.11 doesn't use (its
home page is div#indexPage.page.mainAnimatedPage), so Jellyfin's own
Favorites tab stayed laid out underneath and made the window scrollable.
Once the rows reached their end the wheel went on to the window, and since
the overlay's top fades out under the navbar (#749), Jellyfin's copy of the
same favourites scrolled past behind the navbar's pills (measured in
Chromium against Jellyfin 10.11.8: the window went on scrolling 3224 px at
1920x1080, 3569 px at 1366x768).

Runs the real script under node on the Favorites route for the body class it
sets and where it puts its container, then matches the shipped CSS against
the elements Jellyfin 10.11.8 renders there (selectors made of tags, ids and
classes; the later of two matching rules wins, !important first).
"""
import re
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, node

JS = (INJECT / "tentacle-favorites.js").read_text(encoding="utf-8")
CSS = (INJECT / "tentacle-favorites.css").read_text(encoding="utf-8")

HARNESS = ESC_DOM + r"""
var classes = [];
document.body.classList = {
  add: function (c) { if (classes.indexOf(c) < 0) classes.push(c); },
  remove: function (c) { classes = classes.filter(function (x) { return x !== c; }); },
  contains: function (c) { return classes.indexOf(c) >= 0; }, toggle: function () {} };
var ApiClient = {
  getCurrentUserId: function () { return 'u1'; },
  getUrl: function (p) { return p; },
  getJSON: function () { return new Promise(function () {}); },   // the lists are still on their way
};
var window = { ApiClient: ApiClient, addEventListener: function () {}, location: { hash: '#/home?tab=1' } };
var location = window.location;
%s
setTimeout(function () {
  var out = { body: classes, mounted: document.body.children.map(function (e) { return e.id || ''; }) };
  process.stdout.write(JSON.stringify(out), function () { process.exit(0); });
}, 300);
"""


def el(tag, id_="", classes=""):
    return (tag, id_, set(classes.split()))


def blocks(css: str):
    """(media query or '', body) for the top level and each @media block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, top, i = [], [], 0
    for m in re.finditer(r"@media([^{]+)\{", css):
        if m.start() < i:
            continue
        top.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append((m.group(1).strip(), css[m.end():j - 1]))
        i = j
    top.append(css[i:])
    return [("", "".join(top))] + out


def compound(part: str, e) -> bool:
    tag, id_, classes = e
    for kind, name in re.findall(r"([#.]?)([\w-]+)", part):
        if (kind == "#" and name != id_) or (kind == "." and name not in classes) or (not kind and name != tag):
            return False
    return True


def matches(selector: str, chain) -> bool:
    """Does `selector` pick chain[-1] (chain = the element's ancestors, then the element)?"""
    if not re.fullmatch(r"[\w#.\s-]+", selector):
        return False   # >, :not(), [attr]: none of this file's rules for these elements
    parts, els = selector.split(), list(chain)
    if not compound(parts.pop(), els.pop()):
        return False
    for part in reversed(parts):
        while els and not compound(part, els[-1]):
            els.pop()
        if not els:
            return False
        els.pop()
    return True


def value(body: str, chain, prop: str):
    """`prop` as this block's rules set it on chain[-1], or None."""
    found = None
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        if not any(matches(s.strip(), chain) for s in m.group(1).split(",")):
            continue
        for d in re.finditer(r"(?:^|;)\s*%s\s*:\s*([^;]+)" % re.escape(prop), m.group(2)):
            v = d.group(1).strip()
            if found is None or "!important" in v or "!important" not in found:
                found = v
    return found


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class PageBehindTheRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = node(HARNESS % JS)
        body = el("body", "", " ".join(out["body"]))
        cls.mounted = out["mounted"]
        # Jellyfin 10.11.8 on #/home?tab=1 (read from the page in Chromium)
        pages = [el("html"), body, el("div", "reactRoot"), el("div", "", "mainAnimatedPages skinBody")]
        cls.home = pages + [el("div", "indexPage", "page homePage libraryPage allLibraryPage "
                                                   "pageWithAbsoluteTabs withTabs mainAnimatedPage")]
        cls.native_tab = cls.home + [el("div", "favoritesTab", "tabContent pageTabContent is-active")]
        cls.osd = pages + [el("div", "videoOsdPage", "page mainAnimatedPage")]
        cls.rows = [el("html"), body, el("div", "tentacleFavoritesContainer")]

    def hidden(self, chain):
        """Not drawn: it or one of its ancestors has display: none (rules outside @media)."""
        top = blocks(CSS)[0][1]
        return any((value(top, chain[:i], "display") or "").startswith("none") for i in range(1, len(chain) + 1))

    def test_the_script_shows_its_rows_on_the_favorites_route(self):
        self.assertIn("tentacle-favorites-active", self.rows[1][2])
        self.assertIn("tentacleFavoritesContainer", self.mounted)

    def test_jellyfins_own_favorites_tab_is_hidden_behind_them(self):
        self.assertTrue(self.hidden(self.native_tab),
                        "Jellyfin's Favorites tab stays laid out behind the rows and scrolls under the navbar")

    def test_the_rows_themselves_show(self):
        self.assertFalse(self.hidden(self.rows))

    def test_only_the_home_page_is_hidden(self):
        # The overlay comes up 50 ms after the route changes; a late one must not hide the player.
        self.assertFalse(self.hidden(self.osd))

    def test_the_end_of_the_rows_does_not_scroll_the_window(self):
        for media, body in blocks(CSS):
            v = value(body, self.rows, "overscroll-behavior")
            if media and v is None:
                continue
            self.assertEqual("contain", v, media or "default")


if __name__ == "__main__":
    unittest.main()
