"""Jellyfin's phone apps (iOS, Android): Download, back, trailer (#877).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The iOS app (jellyfin-expo) is one WebView with no browser back, no second
window and no handler for a link to a file. It injects `window.NativeShell`;
its downloads go only through `NativeShell.downloadFile({url, item, filename,
...})`, its outside links through `NativeShell.openUrl(url)`.

- Details → More → Download clicked an `<a download>`: the WebView opened the
  raw file, a black page with no way back, and nothing reached the app's
  Downloads tab.
- On Home with the media bar, the details overlay (z-index 10000) covered the
  navbar's back button: `body.moonfin-details-visible .moonfin-navbar` lost on
  specificity to the media bar's `body.moonfin-mediabar-active
  .moonfin-navbar-active .moonfin-navbar {z-index: 10000}`.
- Activity's "Coming up" trailer was a target="_blank" link: no window to open.
- Favorites, Discover and Activity (overlays on the home route) had no back at
  all: in the app, a dead end. Back shows there only without a browser's own
  back (the apps on a touch screen, a home-screen web app); desktop browsers
  and Jellyfin Media Player (a NativeShell too, but a mouse) are unchanged.
- A recording still recording was offered Download: an unfinished file.

Download, back and the trailer run the real code under node with a fake shell.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

INJECT = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject"
DETAILS_JS = INJECT / "tentacle-details.js"
DISCOVER_JS = INJECT / "tentacle-discover.js"
NAVBAR_JS = INJECT / "tentacle-navbar.js"


def _method(src: str, name: str) -> str:
    """`name: function(...) {...},` of the Details object, by brace matching."""
    start = src.index("    " + name + ": function(")
    i = src.index("{", start)
    depth = 0
    while True:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _details_harness(shell: str, item: dict, body: str) -> str:
    src = DETAILS_JS.read_text(encoding="utf-8")
    methods = [m for m in ("handleMoreAction", "_buildMoreMenu", "_isStrm", "_appCanDownload")
               if ("    " + m + ": function(") in src]
    return r"""
var log = { shell: [], clicked: [], menu: null };
var window = { NativeShell: %SHELL% };
var console = { log: function () {}, error: function () {} };
var made = [];
function _el(tag) { var e = { tag: tag, style: {}, innerHTML: '', click: function () { log.clicked.push(this.href); },
  addEventListener: function () {}, querySelector: function () { return null; },
  querySelectorAll: function () { return []; } }; made.push(e); return e; }
var document = { body: { appendChild: function () {}, removeChild: function () {} },
  createElement: _el, querySelector: function () { return null; }, addEventListener: function () {} };
window.ApiClient = { accessToken: function () { return 'TOKEN'; }, serverId: function () { return 'S1'; },
  getCurrentUserId: function () { return 'u1'; } };
var Details = {
%METHODS%,
  getServerUrl: function () { return 'http://jf'; },
  getAuthHeaders: function () { return {}; },
  closeMoreMenu: function () {}, showToast: function () {}, esc: function (t) { return String(t); }
};
var item = %ITEM%;
%BODY%
process.stdout.write(JSON.stringify(log));
""".replace("%SHELL%", shell).replace("%METHODS%", ",\n".join(_method(src, m) for m in methods)) \
   .replace("%ITEM%", json.dumps(item)).replace("%BODY%", body)


def _run(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout)


APP_SHELL = "{ downloadFile: function (info) { log.shell.push(info); } }"
MOVIE = {"Id": "i1", "ServerId": "S1", "Name": "Movie", "Type": "Movie", "CanDownload": True,
         "Path": "/data/movies/Movie (2020)/Movie (2020).mkv"}
VOD = dict(MOVIE, Path="/data/vod/Movie (2020)/Movie (2020).strm")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DownloadInJellyfinApps(unittest.TestCase):
    def download(self, shell, item=MOVIE):
        return _run(_details_harness(shell, item, "Details.handleMoreAction('download', item);"))

    def test_the_app_gets_the_download_not_the_page(self):
        log = self.download(APP_SHELL)
        self.assertEqual([], log["clicked"],
                         "the page navigated to the raw file (black page in the iOS app)")
        self.assertEqual(1, len(log["shell"]), "nothing was handed to the app's download queue")
        info = log["shell"][0]
        self.assertEqual("http://jf/Items/i1/Download?api_key=TOKEN", info["url"])
        self.assertEqual("Movie (2020).mkv", info["filename"])
        self.assertEqual("i1", info["item"]["Id"])
        self.assertEqual("S1", info["serverId"])

    def test_a_browser_still_downloads_with_a_link(self):
        log = self.download("undefined")
        self.assertEqual(["http://jf/Items/i1/Download?api_key=TOKEN"], log["clicked"])
        self.assertEqual([], log["shell"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DownloadOfferedOnlyWhereItWorks(unittest.TestCase):
    def menu(self, shell, item):
        body = ("Details._buildMoreMenu(item, { Policy: { EnableContentDownloading: true } }, false, false, false);\n"
                "log.menu = []; made.forEach(function (e) { e.innerHTML.replace(/data-more-action=\"([^\"]+)\"/g,"
                " function (_, a) { log.menu.push(a); }); });\n")
        return _run(_details_harness(shell, item, body))

    def test_a_browser_is_not_offered_a_vod_titles_strm_text(self):
        self.assertNotIn("download", self.menu("undefined", VOD)["menu"])

    def test_a_browser_is_offered_a_real_file(self):
        self.assertIn("download", self.menu("undefined", MOVIE)["menu"])

    def test_the_app_is_offered_a_vod_title(self):
        self.assertIn("download", self.menu(APP_SHELL, VOD)["menu"])

    def test_a_recording_still_recording_is_not_offered(self):
        recording = dict(MOVIE, Path="/rec/Game (2026)/Game.ts", Status="InProgress")
        self.assertNotIn("download", self.menu(APP_SHELL, recording)["menu"])
        self.assertNotIn("download", self.menu("undefined", recording)["menu"])

    def test_a_finished_recording_is(self):
        self.assertIn("download", self.menu(APP_SHELL, dict(MOVIE, Status="Completed"))["menu"])


def _navbar_method(src: str, name: str) -> str:
    start = src.index("        " + name + ": function")
    end = src.index("\n        },\n", start)
    return src[start:end] + "\n        }"


IOS_APP = ("{ AppHost: { getDefaultLayout: function () { return 'mobile'; } }, "
           "downloadFile: function () {} }")
JMP = ("{ AppHost: { getDefaultLayout: function () { return 'desktop'; } }, "
       "downloadFile: function () {} }")


def _navbar_harness(shell: str, media: dict, hash_: str, body: str, details_open=False, popup=None) -> str:
    """The navbar's back logic under node: a fake page at `hash_`, matchMedia
    answering `media`, the app's NativeShell (or none)."""
    src = NAVBAR_JS.read_text(encoding="utf-8")
    methods = ["isHomePage", "isHomeOverlay", "noBrowserBack", "notePage", "closeTopPopup",
               "updateBackButton", "setupEventListeners"]
    return r"""
var log = { went: [], closed: [], back: 0 };
var listeners = {};
function el(name) { return { name: name, style: { display: 'none' }, dataset: {},
  addEventListener: function (t, f) { listeners[name + ':' + t] = f; },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  click: function () { log.closed.push(name); } }; }
var backBtn = el('back'), userBtn = el('user');
var POPUP = %POPUP%;
var document = { querySelector: function (sel) { return POPUP && sel.indexOf(POPUP) !== -1 ? el(POPUP) : null; },
  addEventListener: function () {} };
var location = { hash: %HASH% };
var history = { length: 5, back: function () { log.back++; } };
var navigator = {};
var MEDIA = %MEDIA%;
var window = { NativeShell: %SHELL%, matchMedia: function (q) { return { matches: !!MEDIA[q] }; },
  addEventListener: function () {} };
var Details = { isVisible: %DETAILS%, goBack: function () { log.went.push('details-back'); } };
function MutationObserver() { this.observe = function () {}; }
var Navbar = {
%METHODS%,
  isMobile: function () { return false; },
  updateActiveState: function () {},
  handleNavigation: function (a) { log.went.push(a); }
};
Navbar.container = { addEventListener: function () {}, querySelector: function (sel) {
  return sel === '.moonfin-nav-back' ? backBtn : sel === '.moonfin-user-btn' ? userBtn : null; } };
%BODY%
log.backShown = backBtn.style.display !== 'none';
process.stdout.write(JSON.stringify(log));
""".replace("%METHODS%", ",\n".join(_navbar_method(src, m) for m in methods)) \
   .replace("%SHELL%", shell).replace("%MEDIA%", json.dumps(media)).replace("%HASH%", json.dumps(hash_)) \
   .replace("%DETAILS%", "true" if details_open else "false").replace("%POPUP%", json.dumps(popup)) \
   .replace("%BODY%", body)


TOUCH = {"(pointer: coarse)": True}
MOUSE = {}
HOME_WEB_APP = {"(pointer: coarse)": True, "(display-mode: standalone)": True}
OVERLAYS = ["#/home.html?tab=1", "#/home.html?tentacle=discover", "#/home?tentacle=activity"]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BackOnFavoritesDiscoverActivity(unittest.TestCase):
    def shown(self, shell, media, hash_, details_open=False):
        return _run(_navbar_harness(shell, media, hash_, "Navbar.updateBackButton();",
                                    details_open))["backShown"]

    def test_the_ios_app_gets_back_on_every_home_overlay(self):
        for h in OVERLAYS:
            self.assertTrue(self.shown(IOS_APP, TOUCH, h), h)

    def test_also_when_the_app_reports_no_touch_but_the_mobile_layout(self):
        self.assertTrue(self.shown(IOS_APP, MOUSE, "#/home.html?tentacle=discover"))

    def test_a_web_app_on_the_home_screen_too(self):
        self.assertTrue(self.shown("undefined", HOME_WEB_APP, "#/home.html?tab=1"))

    def test_browsers_and_jellyfin_media_player_are_unchanged(self):
        for shell, media in (("undefined", MOUSE), ("undefined", TOUCH), (JMP, MOUSE)):
            for h in OVERLAYS:
                self.assertFalse(self.shown(shell, media, h), (shell, media, h))

    def test_home_itself_has_no_back_anywhere(self):
        for shell, media in (("undefined", MOUSE), (IOS_APP, TOUCH)):
            for h in ("#/home.html", "#/home", ""):
                self.assertFalse(self.shown(shell, media, h), (shell, h))

    def test_other_pages_and_details_keep_theirs(self):
        for shell, media in (("undefined", MOUSE), (IOS_APP, TOUCH)):
            self.assertTrue(self.shown(shell, media, "#/search.html"))
            self.assertTrue(self.shown(shell, media, "#/home.html", details_open=True))

    def click_back(self, hash_, visited=True, popup=None, details_open=False):
        body = ("Navbar._visitedOtherPage = %s;\n" % ("true" if visited else "false") +
                "Navbar.setupEventListeners();\nlisteners['back:click']();\n")
        return _run(_navbar_harness(IOS_APP, TOUCH, hash_, body, details_open, popup))

    def test_back_goes_to_the_page_before(self):
        log = self.click_back("#/home.html?tentacle=discover")
        self.assertEqual((1, []), (log["back"], log["went"]))

    def test_opened_straight_onto_an_overlay_back_goes_home(self):
        log = self.click_back("#/home.html?tab=1", visited=False)
        self.assertEqual((0, ["home"]), (log["back"], log["went"]))

    def test_a_pop_up_closes_first(self):
        log = self.click_back("#/home.html?tentacle=discover", popup="#mdDetailModal")
        self.assertEqual((["#mdDetailModal"], 0, []), (log["closed"], log["back"], log["went"]))

    def test_details_close_before_leaving(self):
        log = self.click_back("#/home.html?tab=1", details_open=True)
        self.assertEqual((0, ["details-back"]), (log["back"], log["went"]))

    def test_a_first_page_counts_once_its_shown(self):
        body = "location.hash = '#/home.html?tentacle=discover'; Navbar.notePage(); log.a = !!Navbar._visitedOtherPage;\n" \
               "location.hash = '#/search.html'; Navbar.notePage(); log.b = !!Navbar._visitedOtherPage;\n"
        log = _run(_navbar_harness(IOS_APP, TOUCH, "", body))
        self.assertEqual((False, True), (log["a"], log["b"]))


class TouchPolish(unittest.TestCase):
    """Only what doesn't change the look: no pull-to-refresh behind an overlay,
    44 pt to tap without bigger buttons, no iOS preview on a long press."""

    def css(self, name):
        return re.sub(r"/\*.*?\*/", "", (INJECT / name).read_text(encoding="utf-8"), flags=re.S)

    def block(self, css, selector):
        m = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, selector)
        return m.group(1)

    def test_overlays_keep_a_pull_at_the_top_to_themselves(self):
        for name, sel in (("tentacle-details.css", ".moonfin-details-panel"),
                          ("tentacle-discover.css", ".md-discover-overlay"),
                          ("tentacle-discover.css", ".md-activity-overlay")):
            self.assertIn("overscroll-behavior-y: contain", self.block(self.css(name), sel), sel)

    def test_touch_targets_grow_only_on_touch_screens(self):
        css = self.css("tentacle-navbar.css")
        coarse = css[css.index("@media (pointer: coarse)"):]
        self.assertRegex(coarse, r"\.moonfin-nav-back::after[^{]*\{[^}]*inset: -4px")
        before = css[:css.index("@media (pointer: coarse)")]
        self.assertNotIn("::after", before.replace(".moonfin-libraries-list::before", ""))

    def test_long_press_cells_have_no_ios_callout(self):
        css = self.css("tentacle-livetv.css")
        m = re.search(r"\.tg-prog,\s*\.tg-chan,\s*\.mv-tile\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("-webkit-touch-callout: none", m.group(1))


class BackAboveDetailsOnHome(unittest.TestCase):
    @staticmethod
    def specificity(selector: str):
        sel = selector.strip()
        ids = len(re.findall(r"#[\w-]+", sel))
        classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+", sel))
        tags = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", sel))
        return (ids, classes, tags)

    def rules(self):
        """(selector, z-index, file order) for every rule that sets the navbar's z-index."""
        out = []
        order = 0
        for name in ("tentacle-navbar.css", "tentacle-mediabar.css", "tentacle-details.css"):
            css = re.sub(r"/\*.*?\*/", "", (INJECT / name).read_text(encoding="utf-8"), flags=re.S)
            for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                z = re.search(r"z-index:\s*(\d+)", body)
                if not z:
                    continue
                for sel in sels.split(","):
                    if sel.strip().endswith(".moonfin-navbar"):
                        order += 1
                        out.append((sel.strip(), int(z.group(1)), order))
        return out

    def winning_z(self, body_classes):
        """The navbar's z-index with these classes on <body>: the most specific
        matching rule, the later one on a tie (the order the plugin injects them)."""
        matching = []
        for sel, z, order in self.rules():
            m = re.fullmatch(r"(?:body((?:\.[\w-]+)*)\s+)?\.moonfin-navbar", sel)
            if not m:
                continue  # a rule for a narrower case (another state, a child): not this one
            needs = [c for c in (m.group(1) or "").split(".") if c]
            if all(c in body_classes for c in needs):
                matching.append((sel, z, order))
        return max(matching, key=lambda r: (self.specificity(r[0]), r[2]))[1]

    def test_the_back_button_is_above_details_on_home_with_the_media_bar(self):
        z = self.winning_z({"moonfin-details-visible", "moonfin-mediabar-active", "moonfin-navbar-active"})
        self.assertGreater(z, 10000, "the details overlay (10000) covers the navbar's back button on Home")

    def test_and_on_every_other_page(self):
        z = self.winning_z({"moonfin-details-visible", "moonfin-navbar-active"})
        self.assertGreater(z, 10000)

    def test_without_details_the_media_bar_value_stays(self):
        z = self.winning_z({"moonfin-mediabar-active", "moonfin-navbar-active"})
        self.assertEqual(10000, z)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ComingUpTrailerInTheApp(unittest.TestCase):
    def handler(self, shell):
        src = DISCOVER_JS.read_text(encoding="utf-8")
        m = re.search(r"var upTrailer = overlay\.querySelector\('\.md-up-trailer-btn'\);.*?\n    \}\);\n",
                      src, re.S)
        self.assertIsNotNone(m, "the Coming up trailer link has no handler for Jellyfin's apps")
        script = r"""
var opened = [], prevented = false, handler = null;
var window = { NativeShell: %SHELL% };
var btn = { getAttribute: function () { return 'https://www.youtube.com/watch?v=x'; },
  addEventListener: function (t, f) { handler = f; } };
var overlay = { querySelector: function () { return btn; } };
%CODE%
handler({ preventDefault: function () { prevented = true; } });
process.stdout.write(JSON.stringify({ opened: opened, prevented: prevented }));
""".replace("%SHELL%", shell).replace("%CODE%", m.group(0))
        return _run(script)

    def test_the_app_opens_it_itself(self):
        r = self.handler("{ AppHost: { supports: function (f) { return f === 'externallinks'; } }, "
                         "openUrl: function (u) { opened.push(u); } }")
        self.assertEqual(["https://www.youtube.com/watch?v=x"], r["opened"])
        self.assertTrue(r["prevented"])

    def test_a_browser_follows_the_link(self):
        r = self.handler("undefined")
        self.assertEqual([], r["opened"])
        self.assertFalse(r["prevented"])


if __name__ == "__main__":
    unittest.main()
