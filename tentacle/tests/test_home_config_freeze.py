"""#33: the home-config safety check must not freeze the file.

write_home_config() returned the OLD config wholesale when dropping aged-out
rows would have halved the playlist rows. Aged rows only get older, so the
condition held on every later run and the file never changed again: remaps,
sort info, hero updates and (since 9bde42e) the per-row `shape` default were
all lost. YouTube rows in a frozen config therefore keep rendering as posters
with their 16:9 thumbnail cropped.

The fix keeps the unresolvable rows but still writes everything else.

Run from tentacle/:  python -m unittest discover -s tests
"""
import inspect
import unittest

import services.smartlists as sl


class TestSafetyCheckDoesNotFreeze(unittest.TestCase):
    def test_safety_path_does_not_return_the_old_config(self):
        src = inspect.getsource(sl.write_home_config)
        safety = src.split("Safety check", 1)
        self.assertEqual(len(safety), 2, "safety check block not found")
        after = safety[1]
        self.assertNotIn(
            "return existing_config", after,
            "the safety check must keep the aged rows and carry on, not freeze the file",
        )

    def test_aged_rows_are_decided_after_the_safety_check(self):
        src = inspect.getsource(sl.write_home_config)
        self.assertIn("aged_out", src)
        self.assertLess(
            src.index("aged_out.append"), src.index("Safety check"),
            "aged rows must be collected before the safety check decides",
        )

    def test_shape_default_is_applied_after_the_safety_check(self):
        # 9bde42e's per-row shape default lives below the safety check; a frozen
        # config never reached it.
        src = inspect.getsource(sl.write_home_config)
        self.assertIn('r.setdefault("shape"', src)
        self.assertGreater(
            src.index('r.setdefault("shape"'), src.index("Safety check"),
            "shape is set after the safety check, so the check must not return early",
        )


if __name__ == "__main__":
    unittest.main()
