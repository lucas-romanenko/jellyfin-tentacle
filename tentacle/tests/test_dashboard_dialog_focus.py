"""Dashboard dialogs and the keyboard.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Opening a dialog (Delete Provider, Resolve Duplicate, Migrate, Add Provider,
...) left the keyboard focus on the page behind it: Tab walked the sidebar and
the page under the dialog, and Enter/Space acted on controls the dialog
covered. Closing it dropped the focus to the top of the document. The real
modal code from app.js (showModal, closeModal and the document listeners) runs
here under node with a small DOM stand-in; skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"


def _modal_block():
    src = APP.read_text(encoding="utf-8")
    start = src.index("// ── Modals ──")
    end = src.index("\n});\n", src.index("// Close modal on Escape", start)) + 5
    return src[start:end]


DOM = r"""
class El {
  constructor(id, cls, parent) {
    this.id = id || ''; this.cls = new Set((cls || '').split(' ').filter(Boolean));
    this.style = {}; this.attrs = {}; this.kids = []; this.parent = parent || null; this.hidden = false;
    if (parent) parent.kids.push(this);
    this.classList = { contains: c => this.cls.has(c), add: c => this.cls.add(c),
      toggle: (c, on) => on ? this.cls.add(c) : this.cls.delete(c) };
  }
  get isConnected() { return true; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  focus() { document.activeElement = this; }
  shown() { for (let e = this; e; e = e.parent) { if (e.style.display === 'none' || e.hidden) return false; } return true; }
  getClientRects() { return this.shown() ? [{}] : []; }
  contains(o) { for (let e = o; e; e = e.parent) if (e === this) return true; return false; }
  all() { return this.kids.flatMap(k => [k, ...k.all()]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const all = this.all();
    if (sel.startsWith('.') && !sel.includes(',')) return all.filter(e => e.cls.has(sel.slice(1)));
    return all.filter(e => e.focusable);   // the focusable-controls selector
  }
}
const body = new El('body');
const listeners = {};
const document = {
  body, activeElement: body,
  getElementById: id => [body, ...body.all()].find(e => e.id === id) || null,
  querySelectorAll: sel => body.querySelectorAll(sel),
  addEventListener: (t, f) => (listeners[t] = listeners[t] || []).push(f),
};
const getComputedStyle = e => ({ display: e.style.display || 'block' });
function ctl(parent, id, cls) { const b = new El(id, cls, parent); b.focusable = true; return b; }

// The page: a sidebar button, a page button, and the Delete button that opens the dialog
const app = new El('app', 'app', body);
const navBtn = ctl(app, 'nav-library');
const deleteBtn = ctl(app, 'provider-delete');
// A confirmation dialog: close cross, Cancel, then the destructive button
const ov = new El('modal-delete-provider', 'modal-overlay', body); ov.style.display = 'none';
const box = new El('', 'modal', ov);
new El('', 'modal-title', box);
const cross = ctl(box, 'x', 'modal-close');
const cancel = ctl(box, 'cancel');
const destroy = ctl(box, 'destroy');
const login = new El('login-overlay', 'login-overlay', body); login.style.display = 'none';

// What the browser does itself: Tab moves to the next visible control in
// document order unless a listener prevented it, and a control that gets
// hidden loses the focus to <body>.
function settle() { if (!document.activeElement.shown()) document.activeElement = body; }
function key(k, shift) {
  let prevented = false;
  (listeners.keydown || []).forEach(f => f({ key: k, shiftKey: !!shift, preventDefault() { prevented = true; } }));
  if (k === 'Tab' && !prevented) {
    const order = body.all().filter(e => e.focusable && e.shown());
    const i = order.indexOf(document.activeElement);
    document.activeElement = order[(i + (shift ? -1 : 1) + order.length) % order.length];
  }
  settle();
  return prevented;
}
const id = e => (e && e.id) || 'body';
"""


def _run(body):
    script = DOM + _modal_block() + "\nconst out = {};\n" + body + "\nprocess.stdout.write(JSON.stringify(out));"
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if res.returncode:
        raise AssertionError(res.stderr[-2000:])
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestDialogKeyboardFocus(unittest.TestCase):
    def test_opening_a_dialog_moves_focus_into_it_onto_cancel(self):
        out = _run("""
          deleteBtn.focus(); showModal('modal-delete-provider');
          out.focus = id(document.activeElement);
          out.role = box.getAttribute('role'); out.modal = box.getAttribute('aria-modal');
          out.labelled = !!box.getAttribute('aria-labelledby');
        """)
        # Cancel: not the close cross, never the destructive button
        self.assertEqual(out["focus"], "cancel")
        self.assertEqual(out["role"], "dialog")
        self.assertEqual(out["modal"], "true")
        self.assertTrue(out["labelled"])

    def test_tab_stays_inside_the_open_dialog(self):
        out = _run("""
          deleteBtn.focus(); showModal('modal-delete-provider');
          const seq = [];
          for (let i = 0; i < 6; i++) { key('Tab'); seq.push(id(document.activeElement)); }
          out.forward = seq;
          const back = [];
          for (let i = 0; i < 6; i++) { key('Tab', true); back.push(id(document.activeElement)); }
          out.backward = back;
          // focus somehow behind the dialog (a click on the page): Tab brings it back
          navBtn.focus(); key('Tab'); out.pulledBack = id(document.activeElement);
        """)
        # Round and round the dialog's own controls, both ways
        self.assertEqual(set(out["forward"]), {"x", "cancel", "destroy"})
        self.assertEqual(set(out["backward"]), {"x", "cancel", "destroy"})
        self.assertEqual(out["pulledBack"], "x")

    def test_closing_returns_focus_to_the_opener(self):
        out = _run("""
          deleteBtn.focus(); showModal('modal-delete-provider'); cancel.focus();
          closeModal('modal-delete-provider'); settle();
          out.afterClose = id(document.activeElement);
          deleteBtn.focus(); showModal('modal-delete-provider'); cancel.focus(); key('Escape');
          out.afterEscape = id(document.activeElement); out.shown = ov.style.display;
        """)
        self.assertEqual(out["afterClose"], "provider-delete")
        self.assertEqual(out["afterEscape"], "provider-delete")
        self.assertEqual(out["shown"], "none")

    def test_no_dialog_open_tab_is_left_alone(self):
        out = _run("""
          navBtn.focus(); out.prevented = key('Tab'); out.focus = id(document.activeElement);
        """)
        self.assertFalse(out["prevented"])
        self.assertEqual(out["focus"], "provider-delete")   # the browser's own next control

    def test_sign_in_screen_keeps_its_own_tab_order(self):
        out = _run("""
          deleteBtn.focus(); showModal('modal-delete-provider');
          login.style.display = 'flex'; navBtn.focus();
          out.prevented = key('Tab'); out.focus = id(document.activeElement);
        """)
        self.assertFalse(out["prevented"])
        self.assertEqual(out["focus"], "provider-delete")

    def test_showing_an_open_dialog_again_does_not_move_focus(self):
        out = _run("""
          deleteBtn.focus(); showModal('modal-delete-provider'); destroy.focus();
          showModal('modal-delete-provider'); out.focus = id(document.activeElement);
          closeModal('modal-delete-provider'); settle(); out.back = id(document.activeElement);
        """)
        self.assertEqual(out["focus"], "destroy")
        self.assertEqual(out["back"], "provider-delete")


if __name__ == "__main__":
    unittest.main()
