"""The People and Requests pages show names and titles as text (family.js).

Run from the tentacle/ directory:  python -m unittest tests.test_dashboard_family_text

Jellyfin names, request titles (TMDB, MusicBrainz) and the admins' decline
reasons are typed by people: every value interpolated into innerHTML in
family.js goes through escHtml, and the plugin's Discover keeps the
declined-reason through esc(). Read from the source (no node needed).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAMILY = (ROOT / "tentacle" / "static" / "js" / "family.js").read_text(encoding="utf-8")
DISCOVER = (ROOT / "tentacle-plugin" / "Inject" / "tentacle-discover.js").read_text(encoding="utf-8")
LIVETV = (ROOT / "tentacle-plugin" / "Inject" / "tentacle-livetv.js").read_text(encoding="utf-8")

# Numbers and fixed words only: ids, counts, a numeric status.
SAFE_FIELDS = {"id", "episodes", "kids_channels"}


class FamilyTextIsEscaped(unittest.TestCase):
    def test_every_interpolated_value_is_escaped(self):
        raw = []
        for m in re.finditer(r"\$\{([^{}]+)\}", FAMILY):
            expr = m.group(1).strip()
            if expr.startswith(("escHtml(", "_fam", "chips.", "meta.", "kidsSel", "reqSel", "poster", "side",
                                "note", "status", "take", "label", "levels.", "named.", "lists.", "rows.", "list.",
                                "d.people", "row(", "jf}", "jf", "encodeURIComponent(")):
                continue
            field = expr.split(".")[-1]
            if field in SAFE_FIELDS or expr in ("n", "i", "mins", "h", "d", "url"):
                continue
            # A choice between two fixed words (' selected' / '', 'Try again' / 'Approve').
            if re.fullmatch(r".+ \? '[^'$]*' : '[^'$]*'", expr):
                continue
            if "escHtml(" in expr and "+" in expr:
                continue
            raw.append(expr)
        # What's left is in toasts and confirm() texts (plain text, never markup) or numbers.
        allowed_plain = {"name", "p.name", "r.name", "keys.length", "card.name", "p ? p.name : 'They'",
                         "FAM_LEVEL_NAMES[level]", "fam.tvFor", "fam.invited.user.name", "r.detail",
                         "bits.join(' and ')", "card.tv_app_address", "card.password", "card.jellyfin_address",
                         "d.tv_app.newest", "r.title", "r.requested_by", "p ? p.name : 'their'", "p ? p.name : 'Their'",
                         "r ? r.title : 'this'", "r ? r.requested_by : 'them'", "keys.length === 1 ? '' : 's'",
                         "n === 1 ? '' : 's'", "fromP.name", "home", "tv", "nfav(p.favourite_count || 0)", "names.join(', ')",
                         "d.kids_channels === 1 ? 'one channel' : d.kids_channels + ' channels'", "name(d.home_from)", "d.kids_channels === 1 ? '' : 's'", "r.episodes === 1 ? '' : 's'"}
        self.assertEqual([], sorted(set(raw) - allowed_plain))

    def test_markup_builders_escape_names_and_titles(self):
        for needle in ("${escHtml(p.name)}", "${escHtml(r.title)}", "${escHtml(r.requested_by)}",
                       "${escHtml(r.note)}", "${escHtml(c.name)}", "${escHtml(m.text)}"):
            with self.subTest(needle=needle):
                self.assertIn(needle, FAMILY)

    def test_plugin_escapes_what_people_typed(self):
        self.assertIn("esc(item.request.note)", DISCOVER)
        self.assertIn("esc(item.request.decided_by || 'Your admin')", DISCOVER)
        self.assertIn("esc(LIVETV.notice)", LIVETV)
        self.assertIn("n.textContent = LIVETV.notice", LIVETV)


if __name__ == "__main__":
    unittest.main()
