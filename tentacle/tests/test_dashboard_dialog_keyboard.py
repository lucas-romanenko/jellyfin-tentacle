"""Dashboard dialogs must take the keyboard focus and give it back (issue #552).

Run from the tentacle/ directory:  python -m unittest discover -s tests

showModal() only made the overlay visible: the focus stayed on the button that
opened it, Tab walked the page and the sidebar under the dialog (Enter/Space
acted on covered controls), closing it dropped the focus to <body>, and the box
had no dialog role for screen readers.

Runs the real modal block of static/js/app.js (from "// ── Modals" to
"// ── Sync") under node, each scenario in a fresh context, with a small DOM
stand-in that does what a browser does: Tab moves to the next visible,
non-inert control unless a keydown listener prevents it; a control that is
hidden or made inert loses the focus to <body>; a click on something that
can't take the focus moves it to <body>; events capture from window down and
bubble back up. Its selector engine knows tag, *, #id, .class, [attr],
[attr="v"], :not(...), :disabled, :enabled and the " " and ">" combinators,
and throws on anything else. setTimeout and requestAnimationFrame callbacks
run right after each step. Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"

HARNESS = r"""
const vm = require('vm');
const BLOCK = JSON.parse(require('fs').readFileSync(0, 'utf8')).block;

function splitTop(s, sep) {
  const out = []; let buf = '', depth = 0, q = null;
  for (const ch of s) {
    if (q) { buf += ch; if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") q = ch;
    else if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    if (ch === sep && depth === 0) { out.push(buf); buf = ''; } else buf += ch;
  }
  out.push(buf);
  return out.map(x => x.trim()).filter(Boolean);
}

function makeWorld() {
  let active = null;
  const timers = [];
  const win = { _listeners: [], parentNode: null };
  const NATIVE = ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A'];

  class El {
    constructor(tag, attrs) {
      this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {};
      this.children = []; this.parentNode = null; this.text = ''; this._listeners = [];
      this.style = { display: '' };
      for (const k in attrs || {}) {
        if (k === 'style') this.style.display = attrs.style;
        else if (k === 'text') this.text = attrs.text;
        else this.attrs[k] = String(attrs[k]);
      }
      const self = this;
      this.classList = {
        contains: c => self._classes().includes(c),
        add: (...cs) => { const s = self._classes(); cs.forEach(c => { if (!s.includes(c)) s.push(c); }); self.attrs.class = s.join(' '); },
        remove: (...cs) => { self.attrs.class = self._classes().filter(c => !cs.includes(c)).join(' '); },
        toggle: (c, force) => { const on = force === undefined ? !self.classList.contains(c) : !!force;
          if (on) self.classList.add(c); else self.classList.remove(c); return on; },
      };
    }
    _classes() { return (this.attrs.class || '').split(/\s+/).filter(Boolean); }
    get id() { return this.attrs.id || ''; } set id(v) { this.attrs.id = String(v); }
    get className() { return this.attrs.class || ''; } set className(v) { this.attrs.class = String(v); }
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
    setAttribute(n, v) { this.attrs[n] = String(v); }
    hasAttribute(n) { return n in this.attrs; }
    removeAttribute(n) { delete this.attrs[n]; }
    toggleAttribute(n, f) { const on = f === undefined ? !this.hasAttribute(n) : !!f;
      if (on) this.setAttribute(n, ''); else this.removeAttribute(n); return on; }
    get inert() { return this.hasAttribute('inert'); } set inert(v) { this.toggleAttribute('inert', !!v); }
    get hidden() { return this.hasAttribute('hidden'); } set hidden(v) { this.toggleAttribute('hidden', !!v); }
    get disabled() { return this.hasAttribute('disabled'); } set disabled(v) { this.toggleAttribute('disabled', !!v); }
    get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
    get firstElementChild() { return this.children[0] || null; }
    get lastElementChild() { return this.children[this.children.length - 1] || null; }
    get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === doc; }
    get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
    set textContent(v) { this.text = String(v); this.children.forEach(c => { c.parentNode = null; }); this.children = []; }
    get tabIndex() {
      if (this.hasAttribute('tabindex')) return parseInt(this.attrs.tabindex, 10);
      return NATIVE.includes(this.tagName) && !(this.tagName === 'A' && !this.hasAttribute('href')) ? 0 : -1;
    }
    set tabIndex(v) { this.setAttribute('tabindex', v); }
    appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach(c => this.appendChild(c)); }
    remove() { const p = this.parentNode; if (p) { p.children = p.children.filter(x => x !== this); this.parentNode = null; } }
    contains(o) { while (o) { if (o === this) return true; o = o.parentNode; } return false; }
    querySelectorAll(sel) { return descendants(this).filter(e => matches(e, sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    getElementsByClassName(c) { return descendants(this).filter(e => e.classList.contains(c)); }
    matches(sel) { return matches(this, sel); }
    closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, sel)) return n; return null; }
    focus() { if (focusable(this)) active = this; }
    blur() { if (active === this) active = null; }
    click() { dispatch(this, 'click', {}); }
    addEventListener(t, fn, o) { this._listeners.push({ type: t, fn, capture: o === true || !!(o && o.capture) }); }
    removeEventListener(t, fn) { this._listeners = this._listeners.filter(l => !(l.type === t && l.fn === fn)); }
    get offsetParent() { return rendered(this) ? (this.parentElement || doc.body) : null; }
    get offsetWidth() { return rendered(this) ? 10 : 0; }
    get offsetHeight() { return rendered(this) ? 10 : 0; }
    getClientRects() { return rendered(this) ? [{ width: 10, height: 10 }] : []; }
    getBoundingClientRect() { const r = rendered(this) ? 10 : 0; return { width: r, height: r, top: 0, left: 0, right: r, bottom: r }; }
    checkVisibility() { return rendered(this); }
    scrollIntoView() {}
  }

  const doc = new El('#document');
  doc.nodeType = 9;
  Object.defineProperty(doc, 'activeElement', { get: () => (active && focusable(active) ? active : doc.body) });
  doc.getElementById = id => descendants(doc).find(e => e.id === id) || null;
  doc.createElement = tag => new El(tag);
  doc.hasFocus = () => true;

  function descendants(n) { const out = []; (function walk(x) { x.children.forEach(c => { out.push(c); walk(c); }); })(n); return out; }
  function rendered(e) {
    if (!e.isConnected) return false;
    for (let n = e; n && n !== doc; n = n.parentNode) if (n.style.display === 'none' || n.hasAttribute('hidden')) return false;
    return true;
  }
  function inert(e) { for (let n = e; n && n !== doc; n = n.parentNode) if (n.hasAttribute('inert')) return true; return false; }
  function focusable(e) { return e.nodeType === 1 && rendered(e) && !inert(e) && !e.disabled && (e.tabIndex >= 0 || e.hasAttribute('tabindex')); }
  function tabbable(e) { return focusable(e) && e.tabIndex >= 0; }

  function matches(el, sel) { return splitTop(sel, ',').some(c => matchComplex(el, parseComplex(c))); }
  function parseComplex(s) {
    const parts = []; let buf = '', depth = 0, q = null;
    const flush = () => { if (buf) parts.push({ c: buf }); buf = ''; };
    for (const ch of s) {
      if (q) { buf += ch; if (ch === q) q = null; continue; }
      if (ch === '"' || ch === "'") { q = ch; buf += ch; continue; }
      if (ch === '(' || ch === '[') depth++;
      if (ch === ')' || ch === ']') depth--;
      if (depth === 0 && (ch === ' ' || ch === '>')) {
        flush();
        const last = parts[parts.length - 1];
        if (last && last.comb) { if (ch === '>') last.comb = '>'; } else if (last) parts.push({ comb: ch });
        continue;
      }
      if (depth === 0 && (ch === '+' || ch === '~')) throw new Error('stub selector engine does not support "' + ch + '" in: ' + s);
      buf += ch;
    }
    flush();
    if (parts.length && parts[parts.length - 1].comb) parts.pop();
    return parts;
  }
  function matchComplex(el, parts) {
    if (!parts.length) return true;
    if (!el || el.nodeType !== 1 || !matchCompound(el, parts[parts.length - 1].c)) return false;
    if (parts.length === 1) return true;
    const comb = parts[parts.length - 2].comb, rest = parts.slice(0, -2);
    if (comb === '>') return matchComplex(el.parentNode, rest);
    for (let a = el.parentNode; a && a.nodeType === 1; a = a.parentNode) if (matchComplex(a, rest)) return true;
    return false;
  }
  function matchCompound(el, s) {
    let m;
    while (s) {
      if ((m = /^\*/.exec(s))) { /* any */ }
      else if ((m = /^[a-zA-Z][a-zA-Z0-9-]*/.exec(s))) { if (el.tagName !== m[0].toUpperCase()) return false; }
      else if ((m = /^#([\w-]+)/.exec(s))) { if (el.id !== m[1]) return false; }
      else if ((m = /^\.([\w-]+)/.exec(s))) { if (!el.classList.contains(m[1])) return false; }
      else if ((m = /^\[\s*([\w-]+)\s*(?:(\S?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+))\s*)?\]/.exec(s))) {
        if (m[2] && m[2] !== '=') throw new Error('stub selector engine does not support "' + m[2] + '" in: ' + s);
        if (!el.hasAttribute(m[1])) return false;
        if (m[2]) { const v = m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : m[5]; if (el.getAttribute(m[1]) !== v) return false; }
      }
      else if ((m = /^:not\(/.exec(s))) {
        let depth = 1, i = 5;
        for (; i < s.length && depth; i++) { if (s[i] === '(') depth++; if (s[i] === ')') depth--; }
        if (matches(el, s.slice(5, i - 1))) return false;
        m = [s.slice(0, i)];
      }
      else if ((m = /^:disabled\b/.exec(s))) { if (!el.disabled) return false; }
      else if ((m = /^:enabled\b/.exec(s))) { if (el.disabled) return false; }
      else throw new Error('stub selector engine does not support: ' + s);
      s = s.slice(m[0].length);
    }
    return true;
  }

  function dispatch(target, type, init) {
    const ev = Object.assign({ type, target, defaultPrevented: false, _stop: false, bubbles: true,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this._stop = true; }, stopImmediatePropagation() { this._stop = true; } }, init);
    const path = []; for (let n = target; n; n = n.parentNode) path.push(n); path.push(win);
    const fire = (node, capture) => {
      ev.currentTarget = node;
      for (const l of node._listeners.slice()) {
        if (l.type !== type) continue;
        if (capture ? !l.capture : (l.capture && node !== target)) continue;
        l.fn.call(node, ev);
        if (ev._stop) return;
      }
    };
    for (const n of path.slice().reverse()) { if (n === target) break; fire(n, true); if (ev._stop) return ev; }
    for (const n of path) { fire(n, false); if (ev._stop) return ev; }
    return ev;
  }

  function h(tag, attrs, ...kids) { const e = new El(tag, attrs); kids.forEach(k => e.appendChild(k)); return e; }
  function dialog(id, title, titleId, ...body) {
    return h('div', { class: 'modal-overlay', id, style: 'none' },
      h('div', { class: 'modal' },
        h('div', { class: 'modal-header' },
          h('div', titleId ? { class: 'modal-title', id: titleId, text: title } : { class: 'modal-title', text: title }),
          h('button', { class: 'modal-close', text: '✕' })),
        ...body));
  }
  const body = h('body', {},
    h('div', { class: 'app' },
      h('aside', { class: 'sidebar' },
        h('a', { id: 'nav-library', href: '#library', text: 'Library' }),
        h('a', { id: 'nav-vod', href: '#vod', text: 'VOD' }),
        h('a', { id: 'nav-livetv', href: '#livetv', text: 'Live TV' })),
      h('main', { class: 'main' },
        h('button', { id: 'prov1-delete', class: 'btn', text: 'Delete' }),
        h('button', { id: 'prov2-test', class: 'btn', text: 'Test' }),
        h('button', { id: 'prov2-edit', class: 'btn', text: 'Edit' }),
        h('button', { id: 'prov2-delete', class: 'btn', text: 'Delete' }),
        h('div', { id: 'media-card', class: 'card', tabindex: 0, text: 'Some Film' }),
        dialog('modal-sync-detail', 'Nightly Sync Summary', null,
          h('div', { id: 'sync-detail-body' },
            h('button', { id: 'sync-step-1', class: 'btn', text: 'Movies' }),
            h('button', { id: 'sync-step-2', class: 'btn', text: 'Series' }))))),
    dialog('modal-add-provider', 'Add Provider', 'provider-modal-title',
      h('input', { id: 'prov-name', class: 'form-input' }),
      h('select', { id: 'prov-type', class: 'form-select' }),
      h('div', { class: 'modal-footer' },
        h('button', { id: 'add-cancel', class: 'btn btn-secondary', text: 'Cancel' }),
        h('button', { id: 'add-save', class: 'btn btn-primary', text: 'Save' }))),
    dialog('modal-delete-provider', 'Delete Provider', null,
      h('p', { id: 'del-text', text: 'Are you sure?' }),
      h('div', {},
        h('button', { id: 'del-cancel', class: 'btn btn-secondary', text: 'Cancel' }),
        h('button', { id: 'del-confirm', class: 'btn btn-danger', text: 'Delete Everything' }))),
    dialog('modal-media-detail', 'Some Film', 'detail-title',
      h('div', { id: 'detail-body' }, h('button', { id: 'detail-fix', class: 'btn', text: 'Wrong film?' }))),
    dialog('modal-fix-match', 'Which movie is this really?', null,
      h('input', { id: 'fm-q', class: 'form-input' }),
      h('button', { id: 'fm-leave', class: 'btn btn-secondary', text: 'Not sure' })),
    h('div', { class: 'toast-container', id: 'toasts' }),
    h('div', { class: 'login-overlay', id: 'login-overlay', style: 'none' },
      h('div', { class: 'user-card', id: 'user-a', tabindex: 0, text: 'Alice' }),
      h('div', { class: 'user-card', id: 'user-b', tabindex: 0, text: 'Bob' })));
  const html = h('html', {}, body);
  doc.appendChild(html);
  doc.documentElement = html; doc.body = body;

  function getComputedStyle(e) {
    const display = e.style.display || (e.classList.contains('modal-overlay') ? 'flex' : 'block');
    return { display, visibility: 'visible', getPropertyValue: p => (p === 'display' ? display : '') };
  }
  const later = fn => { timers.push(fn); return timers.length; };
  Object.assign(win, {
    document: doc, getComputedStyle, console, Node: { ELEMENT_NODE: 1 }, HTMLElement: El, Element: El,
    setTimeout: later, requestAnimationFrame: later, clearTimeout() {}, cancelAnimationFrame() {},
    queueMicrotask: fn => Promise.resolve().then(fn),
    addEventListener(t, fn, o) { this._listeners.push({ type: t, fn, capture: o === true || !!(o && o.capture) }); },
    removeEventListener(t, fn) { this._listeners = this._listeners.filter(l => !(l.type === t && l.fn === fn)); },
  });
  win.window = win; win.self = win;
  const ctx = vm.createContext(win);
  vm.runInContext(BLOCK, ctx);

  async function settle() {
    for (let round = 0; round < 3; round++) {
      for (let i = 0; timers.length && i < 100; i++) timers.shift()();
      await new Promise(r => setImmediate(r));
    }
  }
  const $ = id => doc.getElementById(id);
  const name = e => (!e || e === doc.body ? 'body' : e.id ? '#' + e.id : e.tagName.toLowerCase() + (e.className ? '.' + e._classes().join('.') : ''));
  return {
    doc, $, ctx, settle,
    focused: () => name(doc.activeElement),
    focusOn(id) { $(id).focus(); if (doc.activeElement !== $(id)) throw new Error('could not focus ' + id); },
    async call(fn, ...args) { ctx[fn](...args); await settle(); },
    async press(key, shift) {
      const ev = dispatch(doc.activeElement, 'keydown', { key, code: key, shiftKey: !!shift, keyCode: key === 'Tab' ? 9 : 27 });
      if (key === 'Tab' && !ev.defaultPrevented) {
        const list = descendants(doc).filter(tabbable), i = list.indexOf(doc.activeElement);
        active = (shift ? (i === -1 ? list[list.length - 1] : list[i - 1]) : (i === -1 ? list[0] : list[i + 1])) || null;
      }
      await settle();
      return ev.defaultPrevented;
    },
    async clickOn(e) {
      let f = e; while (f && f.nodeType === 1 && !focusable(f)) f = f.parentNode;
      active = f && f.nodeType === 1 ? f : null;
      dispatch(e, 'click', { button: 0 });
      await settle();
    },
    inside: id => $(id).contains(doc.activeElement),
    dialogInfo(id) {
      const o = $(id), boxes = [o, ...descendants(o)].filter(e => e.getAttribute('role') === 'dialog');
      if (boxes.length !== 1) return { dialogs: boxes.length };
      const b = boxes[0], lb = b.getAttribute('aria-labelledby'), t = lb ? $(lb) : null;
      return { dialogs: 1, box: b.classList.contains('modal'), modal: b.getAttribute('aria-modal'), label: t ? t.textContent : null };
    },
  };
}

const SCENARIOS = {
  async openConfirm(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    return { focus: w.focused(), info: w.dialogInfo('modal-delete-provider') };
  },
  async openForm(w) {
    w.focusOn('prov2-edit');
    await w.call('showModal', 'modal-add-provider');
    return { focus: w.focused(), info: w.dialogInfo('modal-add-provider') };
  },
  async tabStaysInside(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    const tab = [], back = [];
    for (let i = 0; i < 10; i++) { await w.press('Tab'); tab.push([w.focused(), w.inside('modal-delete-provider')]); }
    for (let i = 0; i < 4; i++) { await w.press('Tab', true); back.push([w.focused(), w.inside('modal-delete-provider')]); }
    return { tab, back };
  },
  async tabFromBodyGoesIn(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    await w.clickOn(w.$('del-text'));          // text in the dialog: the focus goes to <body>
    const afterClick = w.focused();
    const open = w.$('modal-delete-provider').style.display !== 'none';
    await w.press('Tab');
    return { afterClick, open, focus: w.focused(), inside: w.inside('modal-delete-provider') };
  },
  async closeByButton(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    w.focusOn('del-cancel');
    await w.call('closeModal', 'modal-delete-provider');
    return { focus: w.focused() };
  },
  async closeByEscape(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    w.focusOn('del-cancel');
    await w.press('Escape');
    return { focus: w.focused(), shown: w.$('modal-delete-provider').style.display,
             locked: w.doc.body.classList.contains('modal-open') };
  },
  async closeByClickOutside(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    await w.clickOn(w.$('modal-delete-provider'));
    return { focus: w.focused(), shown: w.$('modal-delete-provider').style.display };
  },
  async showAgainWhileOpen(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    w.focusOn('del-confirm');
    await w.call('showModal', 'modal-delete-provider');
    const stays = w.focused();
    await w.call('closeModal', 'modal-delete-provider');
    return { stays, back: w.focused() };
  },
  async replacedDialog(w) {
    w.focusOn('media-card');
    await w.call('showModal', 'modal-media-detail');
    w.focusOn('detail-fix');
    await w.call('closeModal', 'modal-media-detail');   // openFixMatch(): one dialog hands over to the next
    await w.call('showModal', 'modal-fix-match');
    const opened = w.focused();
    await w.press('Escape');
    return { opened, back: w.focused() };
  },
  async noDialogOpen(w) {
    w.focusOn('nav-library');
    const prevented = await w.press('Tab');
    return { prevented, focus: w.focused() };
  },
  async signInScreenOverDialog(w) {
    w.focusOn('prov1-delete');
    await w.call('showModal', 'modal-delete-provider');
    w.$('login-overlay').style.display = 'flex';    // showLoginOverlay(): session expired
    w.doc.querySelector('.app').setAttribute('inert', '');
    w.focusOn('user-a');
    const prevented = await w.press('Tab');
    return { prevented, focus: w.focused() };
  },
  async dialogInsideApp(w) {
    w.focusOn('prov2-test');
    w.$('modal-sync-detail').style.display = '';     // openSyncDetailModal() shows it without showModal()
    const seen = [];
    for (let i = 0; i < 6; i++) { await w.press('Tab'); seen.push([w.focused(), w.inside('modal-sync-detail')]); }
    return { seen };
  },
};

(async () => {
  const out = {};
  for (const [k, fn] of Object.entries(SCENARIOS)) {
    try { out[k] = await fn(makeWorld()); } catch (e) { out[k] = { error: String(e && e.stack || e) }; }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _modal_block() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("// ── Modals")
    return src[start:src.index("// ── Sync ", start)]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DialogKeyboardFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "-e", HARNESS], input=json.dumps({"block": _modal_block()}),
                             capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def scenario(self, name):
        r = self.r[name]
        if "error" in r:
            self.fail(r["error"])
        return r

    def test_a_confirmation_opens_on_cancel_never_on_the_destructive_button(self):
        r = self.scenario("openConfirm")
        self.assertEqual("#del-cancel", r["focus"], "the focus stayed behind the dialog (or went to ✕ or Delete Everything)")

    def test_a_form_opens_on_its_first_field(self):
        self.assertEqual("#prov-name", self.scenario("openForm")["focus"])

    def test_the_box_is_a_modal_dialog_named_by_its_title(self):
        for name, title in (("openConfirm", "Delete Provider"), ("openForm", "Add Provider")):
            with self.subTest(name):
                info = self.scenario(name)["info"]
                self.assertEqual(1, info["dialogs"], "no role=dialog in the overlay")
                self.assertTrue(info["box"], "role=dialog belongs on the .modal box")
                self.assertEqual("true", info["modal"])
                self.assertEqual(title, info["label"], "aria-labelledby must point at the .modal-title")

    def test_tab_and_shift_tab_stay_inside_the_open_dialog(self):
        r = self.scenario("tabStaysInside")
        self.assertEqual([], [f for f, inside in r["tab"] if not inside], "Tab left the dialog")
        self.assertEqual([], [f for f, inside in r["back"] if not inside], "Shift+Tab left the dialog")
        self.assertIn("#del-confirm", [f for f, _ in r["tab"]], "Tab never reached the dialog's last button")

    def test_tab_from_a_lost_focus_goes_back_into_the_dialog(self):
        r = self.scenario("tabFromBodyGoesIn")
        self.assertEqual("body", r["afterClick"])
        self.assertTrue(r["open"], "a click inside the dialog must not close it")
        self.assertTrue(r["inside"], "Tab went to %s, under the dialog" % r["focus"])

    def test_closing_gives_the_focus_back_to_the_control_that_opened_it(self):
        for name in ("closeByButton", "closeByEscape", "closeByClickOutside"):
            with self.subTest(name):
                self.assertEqual("#prov1-delete", self.scenario(name)["focus"])

    def test_escape_and_a_click_outside_still_close_and_unlock_the_page(self):
        esc = self.scenario("closeByEscape")
        self.assertEqual("none", esc["shown"])
        self.assertFalse(esc["locked"])
        self.assertEqual("none", self.scenario("closeByClickOutside")["shown"])

    def test_showing_an_open_dialog_again_leaves_the_focus_and_the_opener_alone(self):
        r = self.scenario("showAgainWhileOpen")
        self.assertEqual("#del-confirm", r["stays"])
        self.assertEqual("#prov1-delete", r["back"])

    def test_a_dialog_that_replaces_another_returns_to_the_first_opener(self):
        r = self.scenario("replacedDialog")
        self.assertEqual("#fm-q", r["opened"])
        self.assertEqual("#media-card", r["back"])

    def test_with_no_dialog_open_tab_is_left_to_the_browser(self):
        r = self.scenario("noDialogOpen")
        self.assertFalse(r["prevented"])
        self.assertEqual("#nav-vod", r["focus"])

    def test_the_sign_in_screen_keeps_its_own_tab_order(self):
        r = self.scenario("signInScreenOverDialog")
        self.assertFalse(r["prevented"], "a dialog under the sign-in screen pulled the focus away from it")
        self.assertEqual("#user-b", r["focus"])

    def test_a_dialog_inside_the_app_shell_keeps_tab_too(self):
        r = self.scenario("dialogInsideApp")
        self.assertEqual([], [f for f, inside in r["seen"] if not inside], "Tab walked the page under Nightly Sync Summary")


if __name__ == "__main__":
    unittest.main()
