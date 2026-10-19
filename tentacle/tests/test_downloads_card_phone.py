"""#280: on a phone the Library → Downloads rows must keep the title visible.

In one non-wrapping row the title was the only item allowed to shrink, so
the status pill, progress bar and percent squeezed it to 0 px below about
540 px. Under 600 px the row now wraps, with the title on a line of its own.
(Checked in headless Chromium at 500 px: title 0 px before, full width after.)

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import pathlib
import re
import unittest


class TestDownloadsRowWrapsOnPhones(unittest.TestCase):
    def setUp(self):
        self.html = pathlib.Path("static/index.html").read_text(encoding="utf-8")

    def test_rows_wrap_and_the_title_takes_a_full_line(self):
        rules = [b for b in re.findall(r"@media \(max-width: (\d+)px\) \{(.*?)\n\}", self.html, re.S)
                 if ".dl-item" in b[1]]
        wrap = [(int(w), body) for w, body in rules if "flex-wrap: wrap" in body]
        self.assertTrue(wrap, "the downloads row never wraps on a narrow screen")
        width, body = max(wrap)
        self.assertGreaterEqual(width, 540, "titles vanish below ~540 px")
        self.assertRegex(body, r"\.dl-item-title \{[^}]*flex: 1 1 100%")

    def test_rows_wrap_by_their_own_width_at_any_screen_size(self):
        # Above 600 px the viewport rule no longer applies, but the row can be
        # narrower still: from 769 px the sidebar takes ~250 px, so a tablet in
        # portrait (768-834 px) left the title 0 px wide, and so did 601-680 px
        # with a long status ("import blocked · Bluray-2160p Remux · 2h 15m").
        # The base rules wrap as soon as the title would get less than its basis.
        base = re.search(r"\n\.dl-item \{([^}]*)\}", self.html)
        title = re.search(r"\n\.dl-item-title \{([^}]*)\}", self.html)
        self.assertIsNotNone(base)
        self.assertIsNotNone(title)
        self.assertIn("flex-wrap: wrap", base.group(1))
        basis = re.search(r"flex: 1 1 (\d+)px", title.group(1))
        self.assertIsNotNone(basis, "the title has no minimum share of the row")
        self.assertGreaterEqual(int(basis.group(1)), 160)
        # The phone block must come after the base rules, or they override it.
        phone = self.html.index("@media (max-width: 600px) {\n  .dl-item {")
        self.assertLess(base.start(), phone)
        self.assertLess(title.start(), phone)


if __name__ == "__main__":
    unittest.main()
