"""Tabs whose API is admin-only must be hidden from non-admin users.

Run from the tentacle/ directory:  python -m unittest discover -s tests

`applyUserRole()` hides every element carrying `data-admin-only`. The Library
page's Duplicates tab did not carry it, although every /api/duplicates route
requires an admin, so a non-admin saw a tab that could only answer 403.
Duplicates are on Health now, a page only the admins' part of the sidebar
links to (and showPage() sends anyone else to Library).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class AdminOnlyTabs(unittest.TestCase):
    def test_the_duplicates_router_is_admin_only(self):
        src = (ROOT / "routers" / "duplicates.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"APIRouter\([^)]*dependencies=\[Depends\(require_admin\)\]",
                         "if this stops being admin-only the tab below may be shown again")

    def test_duplicates_are_on_an_admin_only_page(self):
        self.assertNotIn('data-libtab="duplicates"', HTML)
        health = re.search(r'<div class="page" id="page-health".*?(?=<div class="page" id="page-)', HTML, re.S)
        self.assertIn('id="dup-list"', health.group(0))
        server = re.search(r'<div class="nav-section" data-admin-only>.*?</div>\s*</nav>', HTML, re.S)
        self.assertIsNotNone(server, "the admins' nav section carries data-admin-only")
        self.assertIn('data-page="health"', server.group(0))
        app = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const ADMIN_PAGES = new Set(['people', 'requests', 'sources', 'health', 'settings']);", app)


if __name__ == "__main__":
    unittest.main()
