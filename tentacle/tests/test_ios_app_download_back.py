"""Jellyfin's phone apps (iOS, Android): Download, back on Home, trailer (#877).

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

Download and the trailer run the real code under node with a fake shell.
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
