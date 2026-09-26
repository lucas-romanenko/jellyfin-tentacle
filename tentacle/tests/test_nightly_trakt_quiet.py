"""The nightly job does not warn about every Trakt list every night (#160).

Run from the tentacle/ directory:  python -m unittest discover -s tests

With no Trakt client ID configured, fetch_trakt_list already warns once per
process. The nightly job then logged its own WARNING for every Trakt list on
every run ("List 'X' not refreshed: Not refreshed: no Trakt client ID..."),
so a household with three Trakt lists had three warnings a night forever.
"""
import logging
import unittest

import models.database as mdb
import test_nightly_epg_live_provider as nightly


class NightlyTraktQuiet(nightly.NightlyEpgForLiveProvider):
    test_the_helper_selects_on_live_tv_alone = None   # the base module runs it

    def setUp(self):
        super().setUp()
        self.db.add(mdb.ListSubscription(name="Watchlist", type="trakt", tag="Watchlist",
                                         url="https://trakt.tv/users/someone/lists/watchlist"))
        self.db.commit()
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)

    def test_the_nightly_job_syncs_its_guide(self):
        with self.assertLogs("main", level="INFO") as logs:
            super().test_the_nightly_job_syncs_its_guide()
        about = [r for r in logs.records if "Watchlist" in r.getMessage()]
        self.assertTrue(about, "the list is still mentioned")
        self.assertEqual([], [r.getMessage() for r in about if r.levelno >= logging.WARNING])
        self.assertNotIn("not refreshed: Not refreshed", " ".join(r.getMessage() for r in about))


if __name__ == "__main__":
    unittest.main()
