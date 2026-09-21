"""Tabs whose API is admin-only must be hidden from non-admin users.

Run from the tentacle/ directory:  python -m unittest discover -s tests

`applyUserRole()` hides every element carrying `data-admin-only`. The Library
page's Duplicates tab did not carry it, although every /api/duplicates route
requires an admin, so a non-admin saw a tab that could only answer 403.
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

    def test_the_duplicates_tab_is_marked_admin_only(self):
        tab = re.search(r"<button[^>]*data-libtab=\"duplicates\"[^>]*>", HTML)
        self.assertIsNotNone(tab)
        self.assertIn("data-admin-only", tab.group(0))


if __name__ == "__main__":
    unittest.main()
