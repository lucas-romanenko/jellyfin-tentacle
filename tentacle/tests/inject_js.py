"""Run the plugin's injected scripts (tentacle-plugin/Inject) under node and
read their markup the way a browser does.

`ESC_DOM` is a document stub whose elements escape textContent into
innerHTML as a browser does (& < > only, not quotes), so a script's own
escaping helpers behave as they do in Jellyfin web. `node(script)` runs a
script and returns its printed JSON; `Page(html)` parses markup with
Python's HTML parser, decoding character references in text and attributes.
"""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

INJECT = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject"
HAVE_NODE = bool(shutil.which("node"))

# Ordinary names that have to come out unchanged.
NAMES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
         "Šėšta `tick` </div>", "a\\\\b\\"]

ESC_DOM = r"""
function _escText(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/ /g, '&nbsp;'); }
function _el(tag) {
  var e = { tagName: (tag || 'div').toUpperCase(), dataset: {}, children: [], className: '',
    style: { setProperty: function () {}, removeProperty: function () {} },
    classList: { add: function () {}, remove: function () {}, toggle: function () {}, contains: function () { return false; } },
    setAttribute: function () {}, getAttribute: function () { return null; }, addEventListener: function () {},
    removeEventListener: function () {}, appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function () {}, remove: function () {}, focus: function () {},
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    closest: function () { return null; } };
  var html = '';
  Object.defineProperty(e, 'textContent', { get: function () { return this._text || ''; },
    set: function (v) { this._text = String(v); html = _escText(v); } });
  Object.defineProperty(e, 'innerHTML', { get: function () { return html; },
    set: function (v) { html = String(v); (e._writes = e._writes || []).push(html); } });
  return e;
}
var document = { body: _el('body'), head: _el('head'), readyState: 'complete',
  createElement: function (t) { return _el(t); }, getElementById: function () { return null; },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  addEventListener: function () {}, removeEventListener: function () {} };
"""


def node(script: str):
    out = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.elements, self.texts = [], []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.texts.append(data)

    @property
    def text(self):
        return "".join(self.texts)

    def attrs(self, name):
        return [a[name] for _, a in self.elements if name in a]
