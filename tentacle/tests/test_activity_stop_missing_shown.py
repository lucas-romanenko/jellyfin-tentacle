"""'Stop looking' with every shown episode ticked stops what the card counted.

Sonarr's wanted/missing is read one page deep (the newest-aired 250 + 50
recently searched), so on a large library a show's card can say "S12E03"
while 80 older missing episodes are beyond that page. Every client sends no
episode list when all shown labels are ticked; that used to unmonitor all 81.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import routers.activity as activity
from tests.test_activity_arr_actions import FakeSonarr, PAST, _Base

NOW = datetime.utcnow()


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestStopMissingShownOnly(_Base):
    def setUp(self):
        super().setUp()
        FakeSonarr.episodes = FakeSonarr.episodes + [
            {"id": 5, "seasonNumber": 2, "episodeNumber": 3, "monitored": True, "hasFile": False, "airDateUtc": PAST},
            {"id": 6, "seasonNumber": 2, "episodeNumber": 4, "monitored": True, "hasFile": False, "airDateUtc": PAST}]

    def stop(self, wanted, **kw):
        with mock.patch.object(activity, "_get_wanted", return_value=wanted):
            return activity.stop_missing(activity.ArrTitle(**kw), db=self.db, user=self.admin)

    def test_all_ticked_stops_only_the_episodes_on_the_card(self):
        wanted = {"searching": [], "_missing_ids": {("tmdb", 200): [5]}}
        r = self.stop(wanted, media_type="series", tmdb_id=200)
        self.assertEqual([([5], False)], self.sonarr.monitoring, "not episodes 1 and 6, which the card never showed")
        self.assertEqual(1, r["stopped"])

    def test_a_tvdb_only_show_is_matched_by_tvdb(self):
        wanted = {"searching": [], "_missing_ids": {("tvdb", 3000): [1, 6]}}
        self.stop(wanted, media_type="series", tvdb_id=3000)
        self.assertEqual([([1, 6], False)], self.sonarr.monitoring)

    def test_no_card_keeps_the_old_meaning_every_missing_episode(self):
        r = self.stop({"searching": [], "_missing_ids": {("tmdb", 999): [5]}}, media_type="series", tmdb_id=200)
        self.assertEqual([([1, 5, 6], False)], self.sonarr.monitoring)
        self.assertEqual(3, r["stopped"])

    def test_an_explicit_list_is_unchanged(self):
        wanted = {"searching": [], "_missing_ids": {("tmdb", 200): [5]}}
        self.stop(wanted, media_type="series", tmdb_id=200, episodes=["S02E04"])
        self.assertEqual([([6], False)], self.sonarr.monitoring, "a chosen label is honoured even if off the card")

    def test_an_old_cache_without_the_map_behaves_as_before(self):
        self.stop({"searching": []}, media_type="series", tmdb_id=200)
        self.assertEqual([([1, 5, 6], False)], self.sonarr.monitoring)


class TestWantedCarriesTheMap(unittest.TestCase):
    def setUp(self):
        activity.invalidate_wanted_cache()
        self.addCleanup(activity.invalidate_wanted_cache)

    def test_ids_per_card_are_kept_server_side_and_not_in_the_api(self):
        series = {"id": 7, "tmdbId": 70, "tvdbId": 71, "title": "Long Runner", "monitored": True,
                  "added": _iso(NOW - timedelta(days=900)), "images": []}
        recs = [{"id": 700 + e, "seriesId": 7, "seasonNumber": 1, "episodeNumber": e, "monitored": True,
                 "hasFile": False, "airDateUtc": _iso(NOW - timedelta(days=e)), "series": series} for e in (1, 2)]

        class R:
            def __init__(self, b): self.b = b
            def raise_for_status(self): pass
            def json(self): return self.b

        with mock.patch.object(activity.requests, "get", return_value=R({"records": recs})):
            out = activity._fetch_sonarr_searching("http://s", "k")
        self.assertEqual([701, 702], out[0]["_missing_ids"])
        db = mock.Mock()
        with mock.patch.object(activity, "get_setting", side_effect=lambda d, k, *a: "x" if k.startswith("sonarr") else None), \
                mock.patch.object(activity, "_fetch_sonarr_unreleased", return_value=[]), \
                mock.patch.object(activity, "_fetch_sonarr_file_counts", return_value={}), \
                mock.patch.object(activity, "_fetch_sonarr_searching", return_value=[dict(out[0])]), \
                mock.patch.object(activity, "_enrich_posters"):
            w = activity._get_wanted(db)
        self.assertNotIn("_missing_ids", w["searching"][0], "never sent to clients")
        self.assertEqual([701, 702], w["_missing_ids"][("tmdb", 70)])
        self.assertEqual([701, 702], w["_missing_ids"][("tvdb", 71)])


if __name__ == "__main__":
    unittest.main()
