"""An upcoming title with no date yet is listed as upcoming, date TBA.

A film announced with no dates, or in cinemas with no digital date, is not
available to Radarr and has no date ahead; it used to show in neither
Upcoming nor Searching, so a request for it vanished from Activity. A show
announced with no premiere date the same.
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import routers.activity as activity

NOW = datetime.utcnow()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _movie(tmdb, title, *, available, cinemas=None, digital=None):
    return {"tmdbId": tmdb, "title": title, "year": 2026, "monitored": True, "hasFile": False,
            "isAvailable": available, "inCinemas": cinemas, "digitalRelease": digital,
            "physicalRelease": None, "added": _iso(NOW - timedelta(days=1)), "images": []}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


MOVIES = [
    _movie(1, "Announced", available=False),
    _movie(2, "In Cinemas", available=False, cinemas=_iso(NOW - timedelta(days=10))),
    _movie(3, "Coming", available=False, digital=_iso(NOW + timedelta(days=20))),
    _movie(4, "Out", available=True, cinemas=_iso(NOW - timedelta(days=200)),
           digital=_iso(NOW - timedelta(days=100))),
]


class RadarrNoDateYet(unittest.TestCase):
    def wanted(self):
        with mock.patch.object(activity.requests, "get", return_value=_Resp(MOVIES)):
            return activity._fetch_radarr_wanted("http://arr:7878", "k")

    def test_announced_and_in_cinemas_are_upcoming_with_no_date(self):
        w = self.wanted()
        self.assertEqual([3, 1, 2], [m["tmdb_id"] for m in w["unreleased"]])
        tba = {m["tmdb_id"]: m for m in w["unreleased"]}
        for t in (1, 2):
            self.assertEqual("", tba[t]["release_date"])
            self.assertEqual("TBA", tba[t]["release_type"])
        self.assertEqual({"Theatrical"}, set(tba[2]["all_dates"]))

    def test_a_released_film_still_searches_and_a_dated_one_counts_down(self):
        w = self.wanted()
        self.assertEqual([4], [m["tmdb_id"] for m in w["searching"]])
        self.assertTrue(w["unreleased"][0]["release_date"])


class SonarrNoPremiereYet(unittest.TestCase):
    def test_a_show_with_no_dates_is_upcoming_tba_and_an_aired_one_is_not(self):
        series = [
            {"title": "Announced Show", "tvdbId": 1, "tmdbId": 11, "year": 0, "monitored": True,
             "statistics": {"episodeFileCount": 0}, "images": []},
            {"title": "Aired Show", "tvdbId": 2, "tmdbId": 12, "year": 2026, "monitored": True,
             "firstAired": _iso(NOW - timedelta(days=30)), "statistics": {"episodeFileCount": 0},
             "images": []},
            {"title": "Premieres Soon", "tvdbId": 3, "tmdbId": 13, "year": 2026, "monitored": True,
             "firstAired": _iso(NOW + timedelta(days=5)), "statistics": {"episodeFileCount": 0},
             "images": []},
        ]
        out = activity._fetch_sonarr_unreleased("http://arr:8989", "k", series=lambda: series)
        self.assertEqual(["Premieres Soon", "Announced Show"], [s["title"] for s in out])
        self.assertEqual(("", "TBA"), (out[1]["release_date"], out[1]["release_type"]))


if __name__ == "__main__":
    unittest.main()
