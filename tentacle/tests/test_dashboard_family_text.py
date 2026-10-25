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
SAFE_FIELDS = {"id", "episodes", "kids_channels", "bps", "away_quality"}


class FamilyTextIsEscaped(unittest.TestCase):
    def test_every_interpolated_value_is_escaped(self):
        raw = []
        for m in re.finditer(r"\$\{([^{}]+)\}", FAMILY):
            expr = m.group(1).strip()
            if expr.startswith(("escHtml(", "_fam", "chips.", "meta.", "kidsSel", "reqSel", "placeSel", "places", "poster", "side",
                                "note", "status", "take", "label", "levels.", "named.", "lists.", "rows.", "list.",
                                "d.people", "row(", "jf}", "jf", "encodeURIComponent(", "libList",
                                "fam.open.has(", "(d.away_quality / 1e6)")):
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
                         "n === 1 ? '' : 's'", "fromP.name", "where", "home", "tv", "nfav(p.favourite_count || 0)", "names.join(', ')",
                         "d.kids_channels === 1 ? 'one channel' : d.kids_channels + ' channels'", "name(d.home_from)", "d.kids_channels === 1 ? '' : 's'", "r.episodes === 1 ? '' : 's'",
                         "a.libraries.length === 1 ? '1 library' : a.libraries.length + ' libraries'",
                         "c.libraries.length === 1 ? 'one library' : c.libraries.length + ' libraries'",
                         "p.is_admin || !p.access || !fam.open.has(p.id) ? '' : _famAccessPanel(p)",
                         # Add someone: the suggested password (letters and digits only) and
                         # texts set with textContent.
                         "base", "String(n[0] % 100000).padStart(5, '0')", "home.join(' and ')",
                         "nfav(p.favourite_count)"}
        self.assertEqual([], sorted(set(raw) - allowed_plain))

    def test_markup_builders_escape_names_and_titles(self):
        for needle in ("${escHtml(p.name)}", "${escHtml(r.title)}", "${escHtml(r.requested_by)}",
                       "${escHtml(r.note)}", "${escHtml(c.name)}", "${escHtml(m.text)}"):
            with self.subTest(needle=needle):
                self.assertIn(needle, FAMILY)

    def test_plugin_escapes_what_people_typed(self):
        self.assertIn("esc(item.request.note)", DISCOVER)
        self.assertIn("esc(item.request.decided_by || 'Your admin')", DISCOVER)
        self.assertIn("esc(G.notice)", LIVETV)
        self.assertIn("n.textContent = G.notice", LIVETV)


class ProviderStatusReachesEveryScreen(unittest.TestCase):
    """What's wrong with Live TV's provider (services/provider_status.py)
    goes away by itself on every screen: Tentacle's "status" live update,
    passed on by the plugin to Jellyfin Web's guide and the TV app."""

    def test_the_guide_reads_it_again_on_status_and_every_minute(self):
        self.assertIn("indexOf('status') >= 0", LIVETV)
        self.assertIn("msg.MessageType === 'TentacleStatus') { loadNotice().then(showNotice)", LIVETV)
        self.assertIn("Date.now() - (G.noticeAt || 0) >= 60000", LIVETV)

    def test_the_plugin_passes_status_on(self):
        ctl = (ROOT / "tentacle-plugin" / "Api" / "TentacleController.cs").read_text(encoding="utf-8")
        self.assertIn('"recordings", "status" }', ctl)

    def test_the_dashboard_redraws_on_status(self):
        live = (ROOT / "tentacle" / "static" / "js" / "live.js").read_text(encoding="utf-8")
        self.assertIn("live.handlers.status = ", live)
        from services.live_updates import CLIENT_TOPICS
        self.assertIn("status", CLIENT_TOPICS)


if __name__ == "__main__":
    unittest.main()
