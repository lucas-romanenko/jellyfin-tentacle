"""The stuck-download Fix must leave one download, not two (#444).

Removing a queue item with blocklist=true makes Radarr/Sonarr search again by
themselves ("Redownload Failed", on by default) unless skipRedownload=true.
Their grab only shows in the queue seconds later, so when Tentacle grabs its
own replacement as well, the title downloads twice. Exactly one side must look
for the replacement: Tentacle when it can grab one, the arr otherwise.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ActivityLog, Setting
import services.download_health as dh
from tmp_dirs import temp_dir


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


class Arr:
    """Fake Radarr/Sonarr. Like the real one, it searches again by itself after
    a removal with blocklist=true unless skipRedownload=true."""

    def __init__(self, app, records=None, releases=None, grab_error=None, release_error=None):
        self.app = app
        self.grabs, self.own_searches, self.commands, self.release_searches = [], [], [], []
        self.deletes = []
        self.grab_error, self.release_error = grab_error, release_error
        if records is None:
            rec = {"id": 7, "downloadId": "D", "title": "Some.Movie.2000.1080p.WEBRip", "size": 5,
                   "sizeleft": 5, "status": "warning", "trackedDownloadState": "downloading",
                   "protocol": "torrent"}
            rec["movieId" if app == "radarr" else "episodeId"] = 3
            records = [rec]
        self.records = records
        self.releases = releases if releases is not None else [
            {"guid": "t1", "indexerId": 1, "protocol": "torrent",
             "title": "Some.Movie.2000.1080p.BluRay", "rejected": False},
            {"guid": "u1", "indexerId": 2, "protocol": "usenet",
             "title": "Some.Movie.2000.1080p.WEB-DL", "rejected": False},
        ]

    def get(self, url, key, path, **params):
        if path == "queue":
            return {"records": self.records, "totalRecords": len(self.records)}
        if path == "release":
            self.release_searches.append(params)
            if self.release_error:
                raise self.release_error
            return self.releases
        return []

    def post(self, url, key, path, body):
        if path == "release":
            self.grabs.append(body)
            if self.grab_error:
                raise self.grab_error
        elif path == "command":
            self.commands.append(body)
        return {}

    def delete(self, url, key, path, **params):
        self.deletes.append((path, params))
        if params.get("blocklist") == "true" and params.get("skipRedownload") != "true":
            self.own_searches.append(path)


class StuckFixGrabsOnce(unittest.TestCase):
    def _fix(self, arr):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        db.add_all([Setting(key=f"{arr.app}_url", value="http://arr.example"),
                    Setting(key=f"{arr.app}_api_key", value="k")])
        db.commit()
        for name in ("get", "post", "delete"):
            self.addCleanup(setattr, dh, f"_arr_{name}", getattr(dh, f"_arr_{name}"))
            setattr(dh, f"_arr_{name}", getattr(arr, name))
        result = dh.resolve_stuck_download(db, arr.app, 7)
        activity = [a.message for a in db.query(ActivityLog).all()]
        return result, activity

    def _searches(self, arr):
        """Everything that may grab a replacement: Tentacle's grabs, the arr's
        own re-search after the removal, and search commands Tentacle sent."""
        return len(arr.grabs) + len(arr.own_searches) + len(arr.commands)

    def test_radarr_tentacle_grabs_and_radarr_does_not_search(self):
        arr = Arr("radarr")
        result, activity = self._fix(arr)
        self.assertEqual(self._searches(arr), 1,
                         f"Tentacle grabbed {arr.grabs} and radarr searched as well")
        self.assertEqual(arr.grabs, [{"guid": "u1", "indexerId": 2}])
        self.assertEqual(arr.deletes[0][1].get("blocklist"), "true")
        self.assertTrue(result["replaced"])
        self.assertIn("Stuck download fixed: Some.Movie.2000.1080p.WEBRip", activity)

    def test_sonarr_tentacle_grabs_and_sonarr_does_not_search(self):
        arr = Arr("sonarr")
        result, _ = self._fix(arr)
        self.assertEqual(self._searches(arr), 1,
                         f"Tentacle grabbed {arr.grabs} and sonarr searched as well")
        self.assertEqual(arr.release_searches, [{"episodeId": 3}])
        self.assertTrue(result["replaced"])

    def test_nothing_grabbable_asks_the_arr_to_search(self):
        arr = Arr("radarr", releases=[{"guid": "t1", "indexerId": 1, "protocol": "torrent",
                                       "title": "x", "rejected": True}])
        result, activity = self._fix(arr)
        self.assertEqual(arr.commands, [{"name": "MoviesSearch", "movieIds": [3]}])
        self.assertEqual(self._searches(arr), 1)
        self.assertFalse(result["replaced"])
        self.assertTrue(result["searching"])
        self.assertIn("Stuck download cancelled (Radarr is searching for a replacement): "
                      "Some.Movie.2000.1080p.WEBRip", activity)

    def test_search_timeout_asks_the_arr_to_search(self):
        arr = Arr("sonarr", release_error=requests.Timeout("read timed out"))
        result, activity = self._fix(arr)
        self.assertEqual(arr.commands, [{"name": "EpisodeSearch", "episodeIds": [3]}])
        self.assertEqual(self._searches(arr), 1)
        self.assertTrue(result["searching"])
        self.assertFalse(any("no replacement found" in m for m in activity), activity)

    def test_refused_grab_asks_the_arr_to_search(self):
        for status in (400, 500):
            with self.subTest(status=status):
                arr = Arr("radarr", grab_error=_http_error(status))
                result, _ = self._fix(arr)
                self.assertEqual(arr.commands, [{"name": "MoviesSearch", "movieIds": [3]}])
                self.assertFalse(result["replaced"])
                self.assertTrue(result["searching"])

    def test_unanswered_grab_is_not_searched_again(self):
        for error in (requests.Timeout("read timed out"), requests.ConnectionError("reset"),
                      _http_error(504)):
            with self.subTest(error=repr(error)):
                arr = Arr("radarr", grab_error=error)
                result, _ = self._fix(arr)
                self.assertEqual(arr.commands, [], "the grab may have gone through")
                self.assertEqual(arr.own_searches, [])
                self.assertTrue(result["replaced"])

    def test_failed_search_command_says_no_replacement(self):
        arr = Arr("radarr", releases=[])

        def post(url, key, path, body):
            raise _http_error(500)
        arr.post = post
        result, activity = self._fix(arr)
        self.assertFalse(result["replaced"])
        self.assertFalse(result["searching"])
        self.assertIn("Stuck download cancelled (no replacement found): "
                      "Some.Movie.2000.1080p.WEBRip", activity)

    def test_season_pack_is_left_to_sonarrs_own_search(self):
        records = [{"id": 7 + i, "downloadId": "PACK", "title": "Show.S01.1080p", "size": 5,
                    "sizeleft": 5, "status": "warning", "trackedDownloadState": "downloading",
                    "protocol": "torrent", "episodeId": 30 + i} for i in range(3)]
        arr = Arr("sonarr", records=records)
        result, activity = self._fix(arr)
        self.assertEqual(arr.own_searches, ["queue/7"])
        self.assertEqual(arr.grabs, [])
        self.assertEqual(arr.release_searches, [])
        self.assertEqual(arr.commands, [])
        self.assertTrue(result["searching"])
        self.assertIn("Stuck download cancelled (Sonarr is searching for a replacement): "
                      "Show.S01.1080p", activity)


if __name__ == "__main__":
    unittest.main()
