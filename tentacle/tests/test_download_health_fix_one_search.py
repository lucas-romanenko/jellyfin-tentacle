"""The stuck-download Fix (Health -> Downloads, and the auto-fix sweep) must
start exactly one search for the replacement.

resolve_stuck_download() cancels the stuck item with
    DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true
and then grabs a replacement itself: GET /release and POST /release for a
pick from the other protocol. A removal with blocklist=true makes Radarr and
Sonarr search again by themselves (RedownloadFailedDownloadService, the
"Redownload Failed" setting, on by default) unless the DELETE also says
skipRedownload=true. With both searching at once, both grabbed and the title
downloaded twice (torrent + usenet); or the arr's grab was in the queue first,
every release in Tentacle's search came back rejected, and Tentacle reported
"no replacement found" while one was downloading.

When Tentacle grabs nothing (nothing grabbable, its search failed, the arr
refused the grab) the arr is asked to search instead, so a cancelled download
is never left without a search. A season pack is left to Sonarr's own
re-search: Tentacle's search is for one episode and passes over season packs.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import random
import unittest
from collections import Counter

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ActivityLog, DeletionLog, Setting
import services.download_health as dh
from tmp_dirs import temp_dir

TITLE = "Some.Title.2024.1080p.WEBRip"


def _stuck(app, queue_id=7, episode_id=99, download_id="ABC123"):
    item = {
        "id": queue_id,
        "downloadId": download_id,
        "title": TITLE,
        "size": 5_000_000_000,
        "sizeleft": 5_000_000_000,
        "status": "warning",
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "t", "messages": ["The download is stalled with no connections"]}],
        "protocol": "torrent",
    }
    if app == "radarr":
        item["movieId"] = 42
    else:
        item["seriesId"] = 5
        item["episodeId"] = episode_id
    return item


RELEASES = [
    {"guid": "t2", "indexerId": 1, "protocol": "torrent", "title": "Some.Title.2024.1080p.BluRay", "rejected": False},
    {"guid": "u1", "indexerId": 2, "protocol": "usenet", "title": "Some.Title.2024.1080p.WEB-DL", "rejected": False},
]


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Error", response=resp)


class _Arr:
    """Fake Radarr/Sonarr. Besides recording Tentacle's calls it does what the
    real one does after a removal with blocklist=true: search again by itself
    ("Redownload Failed"), unless the removal says skipRedownload=true.

    took:         Tentacle's grabs the arr may have taken (no answer, or a
                  gateway's 502/503/504; a 4xx or the arr's own 500 took nothing)
    own_searches: searches the arr runs itself, each grabbing its best release
    """

    def __init__(self, records, releases=RELEASES, redownload_failed=True, arr_grab_lands_first=False,
                 search_error=None, grab_error=None, command_error=None, delete_error=None):
        self.records = records
        self.releases = releases
        self.redownload_failed = redownload_failed
        self.arr_grab_lands_first = arr_grab_lands_first
        self.search_error = search_error
        self.grab_error = grab_error
        self.command_error = command_error
        self.delete_error = delete_error
        self.deletes, self.searches, self.grabs, self.commands = [], [], [], []
        self.took, self.own_searches = [], []

    def get(self, url, key, path, **params):
        if path == "queue":
            return {"records": self.records, "totalRecords": len(self.records)}
        if path == "release":
            self.searches.append(params)
            if self.search_error:
                raise self.search_error
            if self.arr_grab_lands_first and self.own_searches:
                # the arr's own grab is in its queue: every release is rejected
                return [dict(r, rejected=True) for r in self.releases]
            return self.releases
        return []

    def post(self, url, key, path, body):
        if path == "release":
            self.grabs.append(body)
            e = self.grab_error
            if not (isinstance(e, requests.HTTPError) and e.response.status_code not in (502, 503, 504)):
                self.took.append(body)
            if e:
                raise e
        elif path == "command":
            self.commands.append(body)
            if self.command_error:
                raise self.command_error
            self.own_searches.append(body["name"])
        return {}

    def delete(self, url, key, path, **params):
        self.deletes.append((path, dict(params)))
        if self.delete_error:
            raise self.delete_error
        if (params.get("blocklist") == "true" and params.get("skipRedownload") != "true"
                and self.redownload_failed):
            self.own_searches.append("Redownload Failed")


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr.example"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr.example"), ("sonarr_api_key", "k")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()

    def _fix(self, app, arr, queue_id=7):
        for name, fn in (("_arr_get", arr.get), ("_arr_post", arr.post),
                         ("_arr_delete", arr.delete)):
            orig = getattr(dh, name)
            setattr(dh, name, fn)
            self.addCleanup(setattr, dh, name, orig)
        self.db.query(ActivityLog).delete()
        self.db.query(DeletionLog).delete()
        self.db.commit()
        result = dh.resolve_stuck_download(self.db, app, queue_id, reason="manual")
        self.assertTrue(result.get("ok"), result)
        # Unchanged: one DELETE, which removes the download and blocklists it
        self.assertEqual([p for p, _ in arr.deletes], [f"queue/{queue_id}"])
        self.assertEqual(arr.deletes[0][1].get("removeFromClient"), "true")
        self.assertEqual(arr.deletes[0][1].get("blocklist"), "true")
        return result

    def _one_search(self, arr):
        self.assertEqual(
            len(arr.took) + len(arr.own_searches), 1,
            f"Tentacle's grabs {arr.took} and the arr's own searches {arr.own_searches}: "
            "the replacement must come from exactly one of them")

    def _activity(self):
        return [a.message for a in self.db.query(ActivityLog).all()]

    def _deletion_detail(self):
        return [d.detail for d in self.db.query(DeletionLog).all()]


class StuckDownloadFixSearchesOnce(_Base):
    # ── Tentacle grabs: the arr must not search as well ──────────────────

    def test_radarr_fix_grabs_once(self):
        arr = _Arr([_stuck("radarr")])
        result = self._fix("radarr", arr)
        self._one_search(arr)
        self.assertEqual(arr.deletes[0][1].get("skipRedownload"), "true")
        self.assertEqual(arr.took, [{"guid": "u1", "indexerId": 2}], "the other protocol, as before")
        self.assertTrue(result["replaced"])
        self.assertFalse(result.get("arr_search"))
        self.assertEqual(self._activity(), [f"Stuck download fixed: {TITLE}"])
        self.assertEqual(self._deletion_detail(),
                         ["Cancelled stuck torrent download; replaced with usenet release: "
                          "Some.Title.2024.1080p.WEB-DL"])

    def test_sonarr_fix_grabs_once(self):
        arr = _Arr([_stuck("sonarr")])
        result = self._fix("sonarr", arr)
        self._one_search(arr)
        self.assertEqual(arr.deletes[0][1].get("skipRedownload"), "true")
        self.assertEqual(arr.searches, [{"episodeId": 99}])
        self.assertEqual(arr.took, [{"guid": "u1", "indexerId": 2}])
        self.assertTrue(result["replaced"])

    def test_arr_grab_first_is_no_false_no_replacement_found(self):
        """The arr's own grab landing first made every release in Tentacle's
        search "rejected": Activity said no replacement was found."""
        arr = _Arr([_stuck("radarr")], arr_grab_lands_first=True)
        result = self._fix("radarr", arr)
        self._one_search(arr)
        self.assertTrue(result["replaced"])
        self.assertEqual(self._activity(), [f"Stuck download fixed: {TITLE}"])

    def test_unanswered_grab_is_not_searched_again(self):
        """No answer, or a gateway's 502/503/504, may still be a grab: a
        second search could download the title twice."""
        for error in (requests.Timeout("read timed out"), requests.ConnectionError("reset"),
                      _http_error(502), _http_error(503), _http_error(504)):
            with self.subTest(error=repr(error)):
                arr = _Arr([_stuck("radarr")], grab_error=error)
                result = self._fix("radarr", arr)
                self._one_search(arr)
                self.assertEqual(arr.commands, [])
                self.assertTrue(result["replaced"])
                self.assertFalse(result.get("arr_search"))

    def test_one_episode_next_to_other_downloads_is_still_tentacles(self):
        arr = _Arr([_stuck("sonarr", queue_id=7, episode_id=99, download_id="A"),
                    _stuck("sonarr", queue_id=8, episode_id=100, download_id="B")])
        self._fix("sonarr", arr)
        self._one_search(arr)
        self.assertEqual(arr.took, [{"guid": "u1", "indexerId": 2}])

    # ── Tentacle grabs nothing: the arr searches instead, once ───────────

    def test_failed_search_still_gets_one_search(self):
        """Tentacle's search failing (an interactive search waits on every
        indexer and can outlast its 30 s timeout) must not leave the
        cancelled download without a search, whatever the arr's setting."""
        for app, command in (("radarr", {"name": "MoviesSearch", "movieIds": [42]}),
                             ("sonarr", {"name": "EpisodeSearch", "episodeIds": [99]})):
            for error in (requests.Timeout("read timed out"), requests.ConnectionError("refused"),
                          _http_error(500)):
                for redownload_failed in (True, False):
                    with self.subTest(app=app, error=repr(error), redownload_failed=redownload_failed):
                        arr = _Arr([_stuck(app)], search_error=error, redownload_failed=redownload_failed)
                        result = self._fix(app, arr)
                        self._one_search(arr)
                        self.assertEqual(arr.grabs, [])
                        self.assertFalse(result["replaced"])
                        for c in arr.commands:
                            self.assertEqual(c, command)

    def test_failed_search_reports_the_arr_search(self):
        arr = _Arr([_stuck("radarr")], search_error=requests.Timeout("read timed out"))
        result = self._fix("radarr", arr)
        self.assertEqual(arr.commands, [{"name": "MoviesSearch", "movieIds": [42]}])
        self.assertTrue(result.get("arr_search"))
        self.assertEqual(self._activity(),
                         [f"Stuck download cancelled, Radarr searches for a replacement: {TITLE}"])
        self.assertEqual(self._deletion_detail(),
                         ["Cancelled stuck torrent download; Radarr searches for a replacement"])

    def test_nothing_grabbable_still_gets_one_search(self):
        rejected = [dict(r, rejected=True) for r in RELEASES]
        for releases in (rejected, []):
            for redownload_failed in (True, False):
                with self.subTest(releases=len(releases), redownload_failed=redownload_failed):
                    arr = _Arr([_stuck("radarr")], releases=releases, redownload_failed=redownload_failed)
                    result = self._fix("radarr", arr)
                    self._one_search(arr)
                    self.assertEqual(arr.grabs, [])
                    self.assertFalse(result["replaced"])
                    for c in arr.commands:
                        self.assertEqual(c, {"name": "MoviesSearch", "movieIds": [42]})

    def test_refused_grab_still_gets_one_search(self):
        """404 (release no longer cached), 409 (indexer failed) and the arr's
        own 500 (the download client for that protocol is down, missing or
        does not match the title's tags): nothing was grabbed. On main the
        arr's own re-search covered these; the arr must still search once."""
        for status in (404, 409, 500):
            with self.subTest(status=status):
                arr = _Arr([_stuck("radarr")], grab_error=_http_error(status))
                result = self._fix("radarr", arr)
                self._one_search(arr)
                self.assertEqual(len(arr.grabs), 1)
                self.assertEqual(arr.took, [])
                self.assertFalse(result["replaced"], "a refused grab is no replacement")

    def test_search_command_failing_says_no_replacement(self):
        arr = _Arr([_stuck("radarr")], search_error=requests.Timeout("t"),
                   command_error=requests.ConnectionError("refused"))
        result = self._fix("radarr", arr)
        self.assertFalse(result["replaced"])
        self.assertFalse(result.get("arr_search"))
        self.assertEqual(self._activity(), [f"Stuck download cancelled (no replacement found): {TITLE}"])

    # ── A season pack: Sonarr's own re-search covers every episode ───────

    def test_season_pack_is_left_to_sonarrs_own_search(self):
        pack = [_stuck("sonarr", queue_id=7 + i, episode_id=99 + i, download_id="PACK") for i in range(3)]
        arr = _Arr(pack + [_stuck("sonarr", queue_id=20, episode_id=500, download_id="OTHER")])
        result = self._fix("sonarr", arr, queue_id=8)
        self._one_search(arr)
        self.assertEqual(arr.own_searches, ["Redownload Failed"],
                         "Tentacle searches one episode; Sonarr's own re-search covers the whole pack")
        self.assertEqual(arr.searches, [])
        self.assertEqual(arr.commands, [])
        self.assertFalse(result["replaced"])
        self.assertTrue(result.get("arr_search"))
        self.assertEqual(self._activity(),
                         [f"Stuck download cancelled, Sonarr searches for a replacement: {TITLE}"])


class FaultInjection(_Base):
    """Random releases, season packs, arr setting and faults at every call
    after the cancel; the invariants after each Fix."""

    def _faulty_arr(self, rng):
        def error(kind):
            if kind in (None, "timeout", "reset"):
                return {None: None, "timeout": requests.Timeout("read timed out"),
                        "reset": requests.ConnectionError("connection reset")}[kind]
            return _http_error(kind)

        app = rng.choice(["radarr", "sonarr"])
        protocol = rng.choice(["torrent", "usenet"])
        pack = 1 if app == "radarr" else rng.choice([1, 1, 2, 3])
        records = [dict(_stuck(app, queue_id=7 + i, episode_id=99 + i, download_id="PACK"), protocol=protocol)
                   for i in range(pack)]
        records += [_stuck(app, queue_id=50 + j, episode_id=600 + j, download_id=f"OTHER{j}")
                    for j in range(rng.randint(0, 2))]
        rng.shuffle(records)
        releases = [{"guid": f"g{i}", "indexerId": i, "protocol": rng.choice(["torrent", "usenet"]),
                     "title": f"Release.{i}", "rejected": rng.random() < 0.3}
                    for i in range(rng.randint(0, 4))]
        arr = _Arr(records, releases=releases,
                   redownload_failed=rng.random() < 0.7,
                   arr_grab_lands_first=rng.random() < 0.5,
                   delete_error=error("reset") if rng.random() < 0.05 else None,
                   search_error=error(rng.choice([None, None, None, None, "timeout", "reset", 500, 503])),
                   grab_error=error(rng.choice([None, None, None, None, "timeout", "reset", 404, 409, 500, 502])),
                   command_error=error(rng.choice([None, None, None, "reset", 500])))
        return app, arr, 7 + rng.randrange(pack), pack, protocol

    def _check(self, seed):
        app, arr, queue_id, pack, protocol = self._faulty_arr(random.Random(seed))
        for name, fn in (("_arr_get", arr.get), ("_arr_post", arr.post), ("_arr_delete", arr.delete)):
            setattr(dh, name, fn)
        self.db.query(ActivityLog).delete()
        self.db.query(DeletionLog).delete()
        self.db.commit()
        result = dh.resolve_stuck_download(self.db, app, queue_id, reason="auto")
        activity = self._activity()
        where = f"seed {seed}: {app}, pack of {pack}, result={result}, activity={activity}"
        if arr.delete_error:
            # the cancel failed: nothing else happens
            self.assertFalse(result.get("ok"), where)
            self.assertEqual((arr.searches, arr.grabs, arr.commands, arr.own_searches), ([], [], [], []), where)
            return "cancel failed"
        self.assertEqual([p for p, _ in arr.deletes], [f"queue/{queue_id}"], where)
        params = arr.deletes[0][1]
        self.assertEqual((params.get("removeFromClient"), params.get("blocklist")), ("true", "true"), where)
        searchers = len(arr.took) + len(arr.own_searches)
        # I1: never two searches grabbing a replacement
        self.assertLessEqual(searchers, 1, f"Tentacle took {arr.took}, arr searched {arr.own_searches}; {where}")
        # a season pack: Tentacle's single-episode grab would cover one episode
        if pack > 1:
            self.assertEqual(arr.took, [], where)
        # I2: never none, but for a failed search command, or a pack left to
        # Sonarr when its "Redownload Failed" is off (Sonarr's own choice)
        left_to_arr = params.get("skipRedownload") == "false"
        command_failed = bool(arr.commands) and arr.command_error is not None
        if not (command_failed or (left_to_arr and not arr.redownload_failed)):
            self.assertEqual(searchers, 1, where)
        # I3: the result and Activity say what happened
        self.assertEqual(result["replaced"], len(arr.took) == 1, where)
        self.assertEqual(activity[0].startswith("Stuck download fixed"), result["replaced"], where)
        self.assertEqual("searches for a replacement" in activity[0], bool(result.get("arr_search")), where)
        if "no replacement found" in activity[0]:
            self.assertEqual(searchers, 0, where)
        if result.get("arr_search") and not left_to_arr:
            self.assertEqual(len(arr.own_searches), 1, where)
        # the other protocol is still preferred
        if arr.took:
            other = "usenet" if protocol == "torrent" else "torrent"
            grabbable = [r for r in arr.releases if not r["rejected"]]
            took = next(r for r in arr.releases if r["guid"] == arr.took[0]["guid"])
            if any(r["protocol"] == other for r in grabbable):
                self.assertEqual(took["protocol"], other, where)
        return ("grabbed" if result["replaced"] else "left to Sonarr" if left_to_arr
                else "arr searches" if result.get("arr_search") else "no replacement found")

    def test_random_faults(self):
        for name in ("_arr_get", "_arr_post", "_arr_delete"):
            self.addCleanup(setattr, dh, name, getattr(dh, name))
        first = int(os.environ.get("DOWNLOAD_FIX_PROPERTY_FIRST_SEED", "1"))
        count = int(os.environ.get("DOWNLOAD_FIX_PROPERTY_SEEDS", "1000"))
        failures, outcomes = [], Counter()
        for seed in range(first, first + count):
            try:
                outcomes[self._check(seed)] += 1
            except AssertionError as err:
                failures.append(f"{err}")
        self.assertEqual([], failures[:5], f"{len(failures)} of {count} seeds broke an invariant")
        if count >= 500:   # every path is exercised, so the test can't quietly stop testing one
            for outcome in ("grabbed", "arr searches", "left to Sonarr", "no replacement found", "cancel failed"):
                self.assertGreater(outcomes[outcome], count // 100, f"{outcome}: {dict(outcomes)}")


if __name__ == "__main__":
    unittest.main()
