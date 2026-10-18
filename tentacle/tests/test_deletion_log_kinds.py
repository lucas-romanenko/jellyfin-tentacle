"""Every Deletion Log entry kind has a label, and refused removals don't look like deletions (#643).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Health -> Deletion Log labels each entry from `_DELETION_KIND_META` in
static/js/pages.js; a kind missing there showed its raw code (`sync-prune`,
`arr-remove`, ...). The `*-blocked` kinds record removals a safety guard
refused, so nothing was deleted, yet they got the same accent badge as real
deletions. The docs sent people to Settings for the log, which is on Health,
and every `jellyfin-delete` said "via Jellyfin native UI" though the same route
also takes the plugin's Discover delete.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PAGES_JS = (ROOT / "static" / "js" / "pages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _logged_kinds():
    """{kind: [file:line, ...]} for every log_deletion(...) / DeletionLog(...) call in services/ and routers/."""
    kinds, unnamed = {}, []
    for sub in ("services", "routers"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\b(?:log_deletion|DeletionLog)\(", src):
                if src[max(0, m.start() - 4):m.start()] == "def ":
                    continue
                if re.match(r"\s*\)", src[m.end():]):
                    continue
                where = f"{path.relative_to(ROOT)}:{src.count(chr(10), 0, m.start()) + 1}"
                k = re.search(r'\bkind="([^"]+)"', src[m.end():m.end() + 400])
                if k:
                    kinds.setdefault(k.group(1), []).append(where)
                else:
                    unnamed.append(where)
    return kinds, unnamed


def _kind_meta():
    """{kind: (label, cls)} from _DELETION_KIND_META."""
    block = re.search(r"const _DELETION_KIND_META = \{(.*?)\n\};", PAGES_JS, re.S)
    assert block, "_DELETION_KIND_META not found in pages.js"
    return {k: (label, cls) for k, label, cls in re.findall(
        r"'([a-z-]+)':\s*\{\s*label:\s*'([^']*)',\s*cls:\s*'([^']*)'\s*\}", block.group(1))}


class EveryKindHasALabel(unittest.TestCase):
    def test_every_logged_kind_is_in_the_label_map(self):
        kinds, unnamed = _logged_kinds()
        self.assertEqual([], unnamed, "deletion-log writes without a literal kind=\"...\"")
        self.assertGreaterEqual(len(kinds), 20, sorted(kinds))
        meta = _kind_meta()
        missing = {k: where for k, where in kinds.items() if k not in meta}
        self.assertEqual({}, missing, "kinds the Deletion Log shows as raw codes")

    def test_the_label_map_has_no_stale_kinds(self):
        kinds, _ = _logged_kinds()
        self.assertEqual([], sorted(set(_kind_meta()) - set(kinds)))

    def test_labels_are_words_not_codes(self):
        for kind, (label, _) in _kind_meta().items():
            self.assertNotEqual(label, kind)
            self.assertNotRegex(label, r"_|^[a-z]", kind)


class RefusedRemovalsDontLookLikeDeletions(unittest.TestCase):
    def test_blocked_kinds_say_refused_with_a_neutral_badge(self):
        kinds, _ = _logged_kinds()
        blocked = sorted(k for k in kinds if k.endswith("-blocked"))
        self.assertEqual(["orphan-sweep-blocked", "sync-prune-blocked", "vod-sweep-blocked"], blocked)
        meta = _kind_meta()
        for kind in blocked:
            label, cls = meta.get(kind, ("", ""))
            self.assertIn("refused", label.lower(), kind)
            self.assertIn(cls, ("badge-green", "badge-gray"), kind)

    def test_real_deletions_never_use_the_refused_badge(self):
        for kind, (label, cls) in _kind_meta().items():
            if not kind.endswith("-blocked"):
                self.assertNotIn("refused", label.lower(), kind)
                self.assertNotIn(cls, ("badge-green", "badge-gray"), kind)


class DocsPointToHealth(unittest.TestCase):
    def test_the_card_is_on_the_health_page(self):
        health = re.search(r'<div class="page" id="page-health".*?(?=<div class="page" id="page-)', INDEX_HTML, re.S)
        self.assertIsNotNone(health)
        self.assertIn('<div class="card-title">Deletion Log</div>', health.group(0))

    def test_no_doc_sends_people_to_settings(self):
        wrong = [str(p.relative_to(REPO)) for p in (REPO / "docs").rglob("*.md")
                 if re.search(r"Settings\s*(?:→|->|&rarr;)\s*Deletion Log", p.read_text(encoding="utf-8"), re.I)]
        self.assertEqual([], wrong)
        vod = (REPO / "docs" / "features" / "vod-library.md").read_text(encoding="utf-8")
        self.assertRegex(vod, r"Health\s*→\s*Deletion Log")


class JellyfinDeleteDetail(unittest.TestCase):
    def test_detail_does_not_claim_the_native_ui(self):
        src = (ROOT / "routers" / "library.py").read_text(encoding="utf-8")
        call = re.search(r'log_deletion\(db, kind="jellyfin-delete".*?\)\n', src, re.S)
        self.assertIsNotNone(call)
        self.assertNotIn("native UI", call.group(0))
        self.assertIn("Deleted in Jellyfin", call.group(0))


if __name__ == "__main__":
    unittest.main()
