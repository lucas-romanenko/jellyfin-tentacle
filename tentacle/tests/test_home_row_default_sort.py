"""A home row with no configured sort must keep the order the backend built.

`GetSectionItems`'s switch documents its `_` arm as "random and any
unspecified/unknown sort: trust the playlist order the Python backend already
populated". "Unspecified" never got there: the line above turned a null
`SortBy` into "releasedate", so every row the dashboard had not given an
explicit sort was silently re-ordered by premiere date — and, because
`.Take(limit)` runs after the sort, lost items from the end of the backend
order.

There is no C# test host in this repo, so this reads the controller source the
way tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")

# Arms that reorder the items rather than passing the backend order through.
RESORTING_ARMS = {"communityrating", "releasedate", "name"}


def section_sort_block(src: str) -> str:
    """From the `var sortBy = ...` that feeds GetSectionItems' switch to the
    end of that switch."""
    m = re.search(r"var sortBy = row\?\.SortBy.*?\n        \};", src, re.S)
    if m is None:
        raise AssertionError("GetSectionItems' sort switch was restructured — "
                             "update this test")
    return m.group(0)


class TestHomeRowDefaultSort(unittest.TestCase):
    def setUp(self):
        self.block = section_sort_block(CONTROLLER.read_text())
        m = re.search(r"row\?\.SortBy\?\.ToLowerInvariant\(\)\s*\?\?\s*\"([^\"]*)\"",
                      self.block)
        self.assertIsNotNone(m, "the null-SortBy default is no longer a literal")
        self.default = m.group(1)
        self.arms = set(re.findall(r'^\s*"([^"]+)" =>', self.block, re.M))

    def test_an_unset_sort_does_not_fall_into_a_resorting_arm(self):
        self.assertNotIn(
            self.default, RESORTING_ARMS,
            f"a row with no sort_by is sorted as {self.default!r}; it must be "
            "left in the order the backend populated",
        )

    def test_an_unset_sort_reaches_the_pass_through_arm(self):
        # Either it matches no arm at all (falls to `_`) or it matches an arm
        # that is documented to pass the order through.
        self.assertTrue(
            self.default not in self.arms or self.default in {"datecreated", "random"},
            f"default {self.default!r} is handled by an arm that is not a "
            "pass-through",
        )

    def test_an_unset_sort_behaves_like_random(self):
        # The issue's core equivalence: null and "random" must agree.
        for value in (self.default, "random"):
            self.assertTrue(
                value not in self.arms or value in {"datecreated", "random"},
                f"{value!r} takes a different path from the other",
            )

    def test_the_explicit_sorts_still_sort(self):
        for arm in RESORTING_ARMS:
            self.assertIn(arm, self.arms, f"explicit {arm!r} sort was lost")

    def test_datecreated_still_passes_through(self):
        self.assertRegex(self.block, r'"datecreated" => grouped')


if __name__ == "__main__":
    unittest.main()
