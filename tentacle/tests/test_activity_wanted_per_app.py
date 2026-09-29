"""#204: Activity's wanted lists are cached per app.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Radarr's and Sonarr's lists were cached as one, and any search changing state
in either app dropped both: a Sonarr search re-read Radarr's whole /movie
library (megabytes of JSON on a big install) up to three times per search.
Every refresh also read Sonarr's /series twice.
"""
import time
import unittest
from collections import Counter
from unittest import mock

import models.database as mdb
from routers import activity
from tmp_dirs import temp_dir


class _Resp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class WantedPerApp(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in {"radarr_url": "http://arr:7878", "radarr_api_key": "k",
                     "sonarr_url": "http://arr:8989", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        activity.invalidate_wanted_cache()
        self.addCleanup(activity.invalidate_wanted_cache)
        activity._command_watch.update(ts=0, seen={})
        activity._last_queue_keys = set()
        self.reads = Counter()
        self.commands = {"radarr": [], "sonarr": []}

        def get(url, headers=None, params=None, timeout=None):
            app = "radarr" if ":7878" in url else "sonarr"
            path = url.split("/api/v3/")[1]
            self.reads[f"{app} {path}"] += 1
            if path == "command":
                return _Resp(self.commands[app])
            if path == "wanted/missing":
                return _Resp({"records": []})
            return _Resp([])
        for p in (mock.patch.object(activity.requests, "get", side_effect=get),
                  mock.patch.object(activity, "_enrich_posters")):
            p.start()
            self.addCleanup(p.stop)

    def _watch(self):
        activity._command_watch["ts"] = 0
        activity._watch_arr_searches(self.db)

    def test_one_refresh_reads_sonarrs_series_list_once(self):
        activity._get_wanted(self.db)
        self.assertEqual(1, self.reads["sonarr series"])
        self.assertEqual(1, self.reads["radarr movie"])

    def test_a_sonarr_search_rereads_sonarr_only(self):
        activity._get_wanted(self.db)
        self._watch()                                   # the baseline
        for status in ("queued", "started", "completed"):
            self.commands["sonarr"] = [{"id": 7, "name": "EpisodeSearch", "status": status}]
            self._watch()
            activity._get_wanted(self.db)
        self.assertEqual(1, self.reads["radarr movie"], "Radarr's library was read again for a Sonarr search")
        self.assertEqual(4, self.reads["sonarr series"])

    def test_a_radarr_search_rereads_radarr_only(self):
        activity._get_wanted(self.db)
        self._watch()
        self.commands["radarr"] = [{"id": 2, "name": "MoviesSearch", "status": "started"}]
        self._watch()
        activity._get_wanted(self.db)
        self.assertEqual(2, self.reads["radarr movie"])
        self.assertEqual(1, self.reads["sonarr series"])

    def test_a_download_leaving_radarrs_queue_rereads_radarr_only(self):
        activity._get_wanted(self.db)
        activity._note_queue([{"source": "radarr", "queue_id": 1}])
        activity._note_queue([])
        activity._get_wanted(self.db)
        self.assertEqual(2, self.reads["radarr movie"])
        self.assertEqual(1, self.reads["sonarr series"])

    def test_a_plain_invalidation_still_rereads_both(self):
        activity._get_wanted(self.db)
        activity.invalidate_wanted_cache()
        activity._get_wanted(self.db)
        self.assertEqual(2, self.reads["radarr movie"])
        self.assertEqual(2, self.reads["sonarr series"])

    def test_a_cached_part_is_not_changed_by_the_combined_result(self):
        # The combined list is edited (waits, card ids, posters); the part it came
        # from is reused on the next rebuild and must still hold the card's ids.
        entry = {"title": "Show", "media_type": "series", "tmdb_id": 5, "waiting_since": "2026-01-01",
                 "last_searched": "2026-09-01", "_missing_ids": [1, 2], "_sonarr_id": 9}
        with mock.patch.object(activity, "_fetch_sonarr_searching", return_value=[entry]):
            first = activity._get_wanted(self.db)
            activity._unreleased_cache.update(data=None, ts=0)   # rebuild from the cached parts
            second = activity._get_wanted(self.db)
        self.assertEqual([1, 2], first["_missing_ids"][9])
        self.assertEqual([1, 2], second["_missing_ids"][9])
        self.assertEqual(1, self.reads["sonarr series"])


if __name__ == "__main__":
    unittest.main()
