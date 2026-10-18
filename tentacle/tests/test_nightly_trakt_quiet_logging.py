"""test_nightly_trakt_quiet hands logging back the way it found it (#613).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Its setUp turned logging on and its cleanup forced logging.disable(CRITICAL).
The module has no tearDownModule, so every module run after it had logging
off, and an assertLogs there failed ("no logs of level WARNING or higher"),
but only when the two ran together in that order.
"""
import io
import logging
import unittest

import test_nightly_trakt_quiet


class NightlyTraktQuietLeavesLoggingAlone(unittest.TestCase):
    def setUp(self):
        previous = logging.root.manager.disable
        self.addCleanup(logging.disable, previous)

    def _run_module(self):
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_nightly_trakt_quiet)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_logging_on_before_is_on_after(self):
        logging.disable(logging.NOTSET)
        self._run_module()
        self.assertEqual(logging.NOTSET, logging.root.manager.disable)

    def test_logging_off_before_is_off_after(self):
        logging.disable(logging.CRITICAL)
        self._run_module()
        self.assertEqual(logging.CRITICAL, logging.root.manager.disable)


if __name__ == "__main__":
    unittest.main()
