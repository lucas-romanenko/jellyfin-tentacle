"""A Radarr/Sonarr import doesn't cancel the Jellyfin library scan running
(#961, follow-up to #935).

Each webhook scan that wrote NFOs ended with POST /Library/Refresh, and
Jellyfin answers that by cancelling the scan running and starting over. A
series imported episode by episode, a minute apart, kept restarting the whole
library scan: live, "Scan Media Library Cancelled after 2 minute(s)" then
"after 0 minute(s) and 41 seconds", while the webhook retried "not in Jellyfin
yet". With a scan running the import asks for none; one scan follows the
running one (it may have passed the new folder already), however many imports
came meanwhile. With no scan running it asks for one at once, as before.
"""
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

import services.jellyfin as jellyfin
import test_arr_scan_tag_push_after_library_scan as base
from test_arr_scan_tag_push_after_library_scan import FakeJellyfin, _Response, jellyfin_class


class ScanningJellyfin(FakeJellyfin):
    """Lists its "Scan Media Library" task as running while it scans, and
    counts the library scans asked for."""

    def __init__(self, *a, **kw):
        kw.setdefault("scan_slows_listings", False)
        super().__init__(*a, **kw)
        self.refreshes = 0

    def get(self, url, params=None, timeout=None, **kw):
        if urlsplit(url).path == "/ScheduledTasks":
            return _Response(200, [
                {"Key": "RefreshLibrary", "Name": "Scan Media Library",
                 "State": "Running" if self.scanning else "Idle"},
                {"Key": "RefreshGuide", "Name": "Refresh Guide", "State": "Idle"}])
        return super().get(url, params=params, timeout=timeout, **kw)

    def post(self, url, json=None, timeout=None, **kw):
        if urlsplit(url).path == "/Library/Refresh":
            self.refreshes += 1
        return super().post(url, json=json, timeout=timeout, **kw)


def wait_for_followup(test):
    for t in threading.enumerate():
        if t.name == "jellyfin-scan-followup":
            t.join(5)
            test.assertFalse(t.is_alive(), "the follow-up scan never ended")


class FollowupFast(unittest.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(jellyfin, "SCAN_FOLLOWUP_POLL_SECONDS", 0.01)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(wait_for_followup, self)


class RadarrImportDuringAScan(FollowupFast, base.RadarrScanTagPush):

    def fake(self, **kw):
        return ScanningJellyfin(base.jf_items(self.kind, self.prefix, self.tmdb_base, 2000, (0, 1)), **kw)

    def test_a_scan_running_is_not_cancelled_one_follows_it(self):
        fake = self.fake()
        fake.scanning = True                     # an earlier import's scan
        stats, log = self.scan(fake)
        self.assertGreater(stats["new"], 0, "the scan had something new for Jellyfin")
        self.assertEqual(0, fake.refreshes, "POST /Library/Refresh would cancel the running scan")
        self.assertIn("not cancelling it", log)
        fake.scanning = False                    # the running scan ends
        wait_for_followup(self)
        self.assertEqual(1, fake.refreshes, "one scan after it, for files it had passed")

    def test_no_scan_running_asks_for_one_at_once(self):
        fake = self.fake()
        stats, log = self.scan(fake)
        self.assertEqual(1, fake.refreshes)
        self.assertIn("Triggered Jellyfin library scan", log)
        fake.scanning = False
        wait_for_followup(self)
        self.assertEqual(1, fake.refreshes, "no follow-up when nothing was running")

    test_titles_jellyfin_lists_get_their_tags_although_the_scan_started_a_library_scan = None
    test_control_jellyfin_that_answers_during_a_scan = None


class SonarrImportDuringAScan(RadarrImportDuringAScan):
    logger = "services.sonarr"
    kind, prefix, tmdb_base = "Series", "Show", 2000
    scan = base.SonarrScanTagPush.scan


class ImportsOneAfterTheOther(FollowupFast):
    """A season imported episode by episode: every import while the scan runs
    leaves it running, and together they get one scan after it."""

    def test_three_imports_during_a_scan_one_scan_after_it(self):
        fake = ScanningJellyfin([])
        jf = jellyfin_class(fake)("http://jellyfin.test", "k", "u1")
        self.assertTrue(jf.scan_library_without_cancelling(), "idle: the first import scans at once")
        self.assertEqual(1, fake.refreshes)
        for _ in range(3):
            self.assertFalse(jf.scan_library_without_cancelling())
        self.assertEqual(1, fake.refreshes, "the running scan was never cancelled")
        fake.scanning = False
        wait_for_followup(self)
        self.assertEqual(2, fake.refreshes, "one scan after it, not three")

    def test_jellyfin_that_cant_say_gets_the_scan_as_before(self):
        fake = ScanningJellyfin([])
        fake.get = lambda *a, **k: _Response(500)
        jf = jellyfin_class(fake)("http://jellyfin.test", "k", "u1")
        self.assertTrue(jf.scan_library_without_cancelling())
        self.assertEqual(1, fake.refreshes)

    def test_running_library_tasks_still_names_both_tasks(self):
        fake = ScanningJellyfin([])
        fake.scanning = True
        jf = jellyfin_class(fake)("http://jellyfin.test", "k", "u1")
        self.assertEqual(["Scan Media Library"], jf.running_library_tasks())
        self.assertTrue(jf.library_scan_running())


if __name__ == "__main__":
    unittest.main()
