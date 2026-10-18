"""Run dashboard functions under node and read their HTML the way a browser does.

Used by the dashboard escaping tests. `functions(file, names)` pulls
top-level `function name(...) {...}` sources out of static/js; `node(script)`
runs a script and returns what it printed as JSON; `Page(html)` parses markup
with Python's HTML parser (character references in text and attributes are
decoded as a browser decodes them); `handler_call(js)` runs a decoded inline
handler with every called function recorded. `render(src, setup, call)` runs
dashboard code against a stub document and returns every piece of markup it
wrote (innerHTML, outerHTML, insertAdjacentHTML), by element id.
"""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "static" / "js"
HAVE_NODE = bool(shutil.which("node"))

# Ordinary values that have to come out unchanged: quotes, ampersands, angle
# brackets, a backslash before an apostrophe, line breaks, text that looks
# like a character reference, curly quotes, non-Latin text.
VALUES = [
    "O'Brien & \"Sons\" <3",
    "It's \\'quoted\\'",
    "Line one\nline two\r\n",
    "Tom &#39; Jerry &amp; co",
    "Ocean’s “Eleven”",
    "Paragraph separator end",
    "Šėšta `tick` ${x} </div>",
    "a\\\\b\\",
]


def functions(filename: str, names) -> str:
    src = (JS / filename).read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(r"^(async )?function %s\(" % re.escape(name), src, re.M)
        if not m:
            raise AssertionError(f"{name} not found in {filename}")
        end = src.index("\n}\n", m.start()) + 2
        out.append(src[m.start():end])
    return "\n".join(out)


def node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout)


class Page(HTMLParser):
    """Elements (tag, attrs) and text of a fragment."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.elements, self.texts = [], []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.texts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.texts)

    def attrs(self, name):
        return [a[name] for _, a in self.elements if name in a]


def handler_call(js: str, stub_names):
    """Run decoded handler JS; return [[name, args...], ...] of the stubbed calls."""
    stubs = "".join(f"function {n}(...a) {{ calls.push([{json.dumps(n)}, ...a]); }}\n" for n in stub_names)
    script = ("const calls = []; const event = {stopPropagation() {}}; const self = {};\n" + stubs +
              "try { (function () { %s }).call({}); } catch (e) { calls.push(['error', String(e)]); }\n"
              "process.stdout.write(JSON.stringify(calls));") % js
    return node(script)


# A document stub: getElementById/querySelector hand out elements that record
# the markup written into them; api() answers from API, keyed by path prefix.
DOM_STUB = r"""
const written = {};
function el(id) {
  const e = { id, style: {}, dataset: {}, value: '', textContent: '', checked: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [], appendChild(c) { this.children.push(c); return c; }, remove() {}, focus() {},
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; },
    querySelector(s) { return el(id + ' ' + s); }, querySelectorAll() { return []; },
    closest() { return null; }, scrollIntoView() {},
    insertAdjacentHTML(pos, html) { (written[id] = written[id] || []).push(html); } };
  Object.defineProperty(e, 'innerHTML', { get() { return (written[id] || []).slice(-1)[0] || ''; },
    set(v) { (written[id] = written[id] || []).push(String(v)); } });
  Object.defineProperty(e, 'outerHTML', { get() { return ''; },
    set(v) { (written[id + ':outer'] = written[id + ':outer'] || []).push(String(v)); } });
  return e;
}
const _els = {};
const document = {
  getElementById(id) { return _els[id] || (_els[id] = el(id)); },
  querySelector(s) { return _els[s] || (_els[s] = el(s)); },
  querySelectorAll() { return []; },
  createElement(t) { return el('new:' + t); },
  createTextNode(t) { return { textContent: t }; },
  addEventListener() {}, body: el('body'),
};
const window = { addEventListener() {}, location: { hash: '' }, innerWidth: 1280 };
const localStorage = { getItem() { return null; }, setItem() {} };
const API = {};
async function api(path) {
  const key = Object.keys(API).sort((a, b) => b.length - a.length).find(k => path.startsWith(k));
  if (key === undefined) throw new Error('no fake for ' + path);
  const v = API[key]; if (v instanceof Error) throw v; return JSON.parse(JSON.stringify(v));
}
function toast() {}
const setTimeout = () => 0, setInterval = () => 0, clearTimeout = () => {}, clearInterval = () => {};
"""


def render(src: str, setup: str, call: str) -> dict:
    """{element id: [markup written, ...]} after `call` (awaited) ran."""
    script = (DOM_STUB + src + "\n" + setup + "\n(async () => { " + call + "; })().then("
              "() => process.stdout.write(JSON.stringify(written)),"
              " e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });")
    return node(script)


def missing(values, html_list):
    """Values not shown exactly (as text or an attribute value) in the markup."""
    pages = [Page(h) for h in html_list]
    shown = [p.text for p in pages] + [v for p in pages for _, a in p.elements for v in a.values() if v]
    return [v for v in values if not any(v in s for s in shown)]


def missing_text(values, html_list):
    """Values not shown exactly as text (whitespace runs count as one space)."""
    texts = [" ".join(Page(h).text.split()) for h in html_list]
    return [v for v in values if not any(" ".join(v.split()) in t for t in texts)]
