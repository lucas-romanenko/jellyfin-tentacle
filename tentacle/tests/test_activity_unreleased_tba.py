"""#953: a monitored title with no file and no date ahead, which Radarr or
Sonarr is still waiting for, is Upcoming with no date (sorted last), not
missing from both Upcoming and Searching."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import routers.activity as activity


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.utcnow()
PAST = _iso(NOW - timedelta(days=20))
SOON = _iso(NOW + timedelta(days=5))


def _movie(tmdb, title, *, available, **dates):
    return {"tmdbId": tmdb, "title": title, "year": 2026, "monitored": True,
            "hasFile": False, "isAvailable": available, "images": [], **dates}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class TestRadarrNoDateAhead(unittest.TestCase):
    def wanted(self, movies):
        with mock.patch.object(activity.requests, "get", return_value=_Resp(movies)):
            return activity._fetch_radarr_wanted("http://arr:7878", "k")

    def test_announced_and_in_cinemas_films_are_upcoming_with_no_date_last(self):
        out = self.wanted([
            _movie(1, "Announced", available=False),
            _movie(2, "In Cinemas", available=False, inCinemas=PAST),
            _movie(3, "Digital Soon", available=False, inCinemas=PAST, digitalRelease=SOON),
        ])
        self.assertEqual([], out["searching"])
        self.assertEqual(["Digital Soon", "Announced", "In Cinemas"],
                         [u["title"] for u in out["unreleased"]])
        announced, cinemas = out["unreleased"][1:]
        self.assertEqual(("", "TBA"), (announced["release_date"], announced["release_type"]))
        self.assertEqual("", cinemas["release_date"])
        self.assertEqual({"Theatrical": PAST[:10]}, cinemas["all_dates"])

    def test_an_available_film_still_searches(self):
        out = self.wanted([_movie(4, "Out Now", available=True, inCinemas=PAST, digitalRelease=PAST)])
        self.assertEqual(["Out Now"], [m["title"] for m in out["searching"]])
        self.assertEqual([], out["unreleased"])


class TestSonarrNoDates(unittest.TestCase):
    def test_an_announced_show_is_upcoming_and_an_aired_one_is_not(self):
        def show(sid, title, **dates):
            return {"id": sid, "title": title, "tmdbId": sid, "tvdbId": sid, "year": 2026,
                    "monitored": True, "statistics": {"episodeFileCount": 0}, "images": [], **dates}
        out = activity._fetch_sonarr_unreleased("http://arr:8989", "k", series=lambda: [
            show(1, "Announced Show"),
            show(2, "Aired Show", firstAired=PAST),
            show(3, "Premieres Soon", firstAired=SOON),
        ])
        self.assertEqual(["Premieres Soon", "Announced Show"], [u["title"] for u in out])
        self.assertEqual(("", "TBA", {}), (out[1]["release_date"], out[1]["release_type"], out[1]["all_dates"]))


if __name__ == "__main__":
    unittest.main()
