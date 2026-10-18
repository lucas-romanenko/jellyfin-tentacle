"""The details page shows track titles and Fix Match poster addresses exactly.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-details.js put the selected audio and subtitle track's DisplayTitle
into the Audio / Subtitles button labels with no escaping (the same field is
escaped in the track menus), so a track title with angle brackets or an
"&...;" sequence showed different text. The Fix Match list put each
candidate's poster_path into the image address unescaped. Both now use the
file's own esc()/escAttr(). Runs the real script under node.
"""
import json
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, NAMES, Page, node

SETUP = ESC_DOM + r"""
var window = { addEventListener: function(){}, location: { hash: '' },
  ApiClient: { serverAddress: function(){ return 'http://jf'; }, accessToken: function(){ return 't'; },
               getCurrentUserId: function(){ return 'u'; } } };
var ApiClient = window.ApiClient; var navigator = { userAgent: 'x' };
var localStorage = { getItem: function(){ return null; }, setItem: function(){} };
var MutationObserver = function(){ this.observe = function(){}; this.disconnect = function(){}; };
"""


def _render(audio, sub):
    item = {"Id": "i1", "Name": "Film", "Type": "Movie", "UserData": {},
            "MediaSources": [{"Id": "m1", "Name": "v", "DefaultAudioStreamIndex": 1, "DefaultSubtitleStreamIndex": 3,
                              "MediaStreams": [{"Type": "Audio", "Index": 1, "DisplayTitle": audio},
                                               {"Type": "Audio", "Index": 2, "DisplayTitle": "other"},
                                               {"Type": "Subtitle", "Index": 3, "DisplayTitle": sub}]}]}
    script = SETUP + (INJECT / "tentacle-details.js").read_text(encoding="utf-8") + r"""
var c = document.createElement('div');
var panel = document.createElement('div');
panel.querySelector = function(){ return document.createElement('div'); };
c.querySelector = function(s){ return s === '.moonfin-details-panel' ? panel : document.createElement('div'); };
Details.container = c;
try { Details.renderDetails(__ITEM__, [], [], [], [], [], []); } catch (e) { /* after the markup is written */ }
process.stdout.write(JSON.stringify((panel._writes || []).join('\n')));
""".replace("__ITEM__", json.dumps(item))
    return node(script)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DetailsTrackTitles(unittest.TestCase):
    def test_audio_and_subtitle_labels(self):
        for name in NAMES:
            page = Page(_render(name + " audio", name + " sub"))
            labels = [t for t in page.texts if t.strip()]
            self.assertIn(name + " audio", labels, name)
            self.assertIn(name + " sub", labels, name)

    def test_fix_match_poster_address(self):
        script = SETUP + (INJECT / "tentacle-details.js").read_text(encoding="utf-8") + r"""
var parts = {};
var overlay = document.createElement('div');
overlay.querySelector = function (s) { return parts[s] || (parts[s] = document.createElement('div')); };
var make = document.createElement;
var first = true;
document.createElement = function (t) { if (first && t === 'div') { first = false; return overlay; } return make(t); };
document.body.appendChild = function () {};
var fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ candidates: __C__ }); } }); };
Details.getServerUrl = function () { return 'http://jf'; };
Details.getAuthHeaders = function () { return {}; };
try { Details.showFixMatch({ Id: 'i1', Name: 'Film', ProviderIds: { Tmdb: '5' } }); } catch (e) {}
setTimeout(function () { var l = parts['.tfm-list']; process.stdout.write(JSON.stringify(l ? (l._writes || []).join('') : '')); }, 200);
"""
        cands = [{"title": "Film", "poster_path": '/a"b&c.jpg', "tmdb_id": 1}]
        html = node(script.replace("__C__", json.dumps(cands)))
        self.assertEqual(['https://image.tmdb.org/t/p/w92/a"b&c.jpg'], Page(html).attrs("src"))

if __name__ == "__main__":
    unittest.main()
