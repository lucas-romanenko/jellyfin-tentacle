"""Picked episodes show in Activity from the moment they are asked for.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Manage Episodes, Download More and an add with picked episodes all end in one
Sonarr EpisodeSearch for the picks, and Sonarr works through it one episode at
a time (105 picks took 22 minutes). Two things hid the picks until each one
was grabbed:
- a show added for picked episodes is left unmonitored itself, and Sonarr's
  Wanted list (monitored=true) leaves out every episode of an unmonitored show;
- the first episode to reach the queue took the whole show's Searching card
  with it, so the other 104 were listed nowhere.
"""
import unittest
from datetime import timedelta
from unittest import mock

import models.database as mdb
import routers.activity as activity
from services import download_readiness
from tests import test_activity_searching as base

NOW = base.NOW
_iso = base._iso

SESAME = {"id": 269, "title": "Sesame Street", "tmdbId": 502, "tvdbId": 78419, "year": 1969,
          "monitored": False, "monitorNewItems": "none",
          "added": _iso(NOW - timedelta(days=200)), "images": []}
FOLLOWED = {"id": 30, "title": "Followed Show", "tmdbId": 300, "tvdbId": 3000, "year": 2015,
            "monitored": True, "added": _iso(NOW - timedelta(days=900)), "images": []}


def _episode(series, eid, season, number, *, monitored=True, has_file=False, aired_days_ago=900):
    return {"id": eid, "seriesId": series["id"], "seasonNumber": season, "episodeNumber": number,
            "airDateUtc": _iso(NOW - timedelta(days=aired_days_ago)), "hasFile": has_file,
            "monitored": monitored, "lastSearchTime": None}


def _search(ids, *, status="started", minutes_ago=1, cid=960894):
    return {"id": cid, "name": "EpisodeSearch", "commandName": "Episode Search", "status": status,
            "trigger": "manual", "queued": _iso(NOW - timedelta(minutes=minutes_ago)),
            "body": {"episodeIds": list(ids), "name": "EpisodeSearch", "trigger": "manual",
                     "sendUpdatesToClient": True}}


def _grab(series, ep, *, qid, done=0):
    """A Sonarr queue record for one grabbed episode, `done` of 100 bytes in."""
    return {"id": qid, "seriesId": series["id"], "episodeId": ep["id"], "series": series,
            "episode": {"id": ep["id"], "seasonNumber": ep["seasonNumber"], "episodeNumber": ep["episodeNumber"]},
            "title": f"{series['title']} S{ep['seasonNumber']}E{ep['episodeNumber']}",
            "size": 100, "sizeleft": 100 - done, "status": "downloading",
            "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading", "downloadId": f"sab-{qid}"}


class FakeSonarr:
    """Sonarr's answers, with its real Wanted → Missing filter: monitored=true
    is "episode AND series monitored", monitored=false is everything else."""

    def __init__(self):
        self.series = [SESAME, FOLLOWED]
        # The pick: S53E01-E05 of Sesame Street, ticked in Manage Episodes.
        self.picks = [_episode(SESAME, 3000 + n, 53, n) for n in range(1, 6)]
        self.episodes = self.picks + [
            _episode(SESAME, 2901, 52, 1, has_file=True),         # on disk already
            _episode(SESAME, 3101, 54, 1, monitored=False),       # not picked
            _episode(FOLLOWED, 400, 2, 9, aired_days_ago=2),      # an ordinary missing episode
        ]
        self.commands = []
        self.queue = []
        self.episode_reads = []

    def _with_series(self, ep):
        return dict(ep, series=next(s for s in self.series if s["id"] == ep["seriesId"]))

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        if ":7878" in url:
            if url.endswith("/api/v3/movie"):
                return base._Resp([])
            if url.endswith("/api/v3/queue"):
                return base._Resp({"records": []})
            raise AssertionError(f"unexpected GET {url}")
        path = url.split("/api/v3/", 1)[1]
        if path == "command":
            return base._Resp(self.commands)
        if path == "queue":
            return base._Resp({"records": self.queue})
        if path == "series":
            return base._Resp([dict(s, statistics={"episodeFileCount": sum(
                1 for e in self.episodes if e["seriesId"] == s["id"] and e["hasFile"])}) for s in self.series])
        if path == "wanted/missing":
            want = params.get("monitored", "true") == "true"
            rows = [self._with_series(e) for e in self.episodes if not e["hasFile"]]
            rows = [r for r in rows if (r["monitored"] and r["series"]["monitored"]) == want]
            return base._Resp({"page": 1, "totalRecords": len(rows), "records": rows[:params.get("pageSize", 10)]})
        if path == "episode":
            ids = list(params.get("episodeIds") or [])
            self.episode_reads.append(ids)
            rows = [e for e in self.episodes if e["id"] in ids]
            return base._Resp([self._with_series(e) if params.get("includeSeries") == "true" else dict(e)
                               for e in rows])
        raise AssertionError(f"unexpected GET {url}")


class _PickBase(base._Base):
    def setUp(self):
        super().setUp()
        activity._command_watch.update(ts=0, seen={})
        download_readiness.reset()
        self.addCleanup(download_readiness.reset)
        self.sonarr = FakeSonarr()

    def poll(self, user=None):
        """One Activity poll, as the TV app makes every 3 s on the Activity tab."""
        activity._command_watch["ts"] = 0   # the 5 s watch throttle has passed
        with mock.patch.object(activity.requests, "get", side_effect=self.sonarr.get):
            return activity.get_activity(request=None, db=self.db, user=user or self.admin)

    def save_pick(self, ids=None, **kw):
        """Manage Episodes saved: Sonarr queues one EpisodeSearch for the picks."""
        self.sonarr.commands = [_search(ids or [e["id"] for e in self.sonarr.picks], **kw)]

    @staticmethod
    def card(out, title="Sesame Street"):
        cards = [x for x in out["searching"] if x["title"] == title]
        return cards[0] if len(cards) == 1 else (None if not cards else cards)


class TestPicksOnAnUnmonitoredShow(_PickBase):
    def test_the_picks_show_as_searching_right_after_the_save(self):
        self.poll()                                     # Activity open before the save
        self.save_pick()
        out = self.poll()
        card = self.card(out)
        self.assertIsNotNone(card, "the 5 picked episodes must show before any of them is grabbed")
        self.assertEqual("S53E01 +4", card["episode"])
        self.assertEqual(5, card["missing_episodes"])
        self.assertEqual([f"S53E0{n}" for n in range(1, 6)], card["missing_labels"])
        self.assertEqual("searching", card["status"])
        self.assertEqual(1, card["episodes_on_disk"], "S52E01 is on disk: stop looking, not remove")
        self.assertEqual([], [d for d in out["downloads"] if d["title"] == "Sesame Street"])

    def test_the_wait_counts_from_the_request_not_from_when_the_show_was_added(self):
        self.save_pick(minutes_ago=2)
        card = self.card(self.poll())
        waited = NOW - activity._parse_dt(card["waiting_since"])
        self.assertLess(abs(waited - timedelta(minutes=2)), timedelta(seconds=5))
        self.assertEqual("Sesame Street", self.poll()["searching"][0]["title"], "just asked for: listed first")

    def test_a_search_still_queued_in_sonarr_counts_too(self):
        self.save_pick(status="queued")
        self.assertEqual(5, self.card(self.poll())["missing_episodes"])

    def test_one_grab_moves_one_episode_and_the_rest_stay_searching(self):
        self.save_pick()
        self.poll()
        e2 = self.sonarr.picks[1]
        self.sonarr.queue = [_grab(SESAME, e2, qid=5001)]
        out = self.poll()
        self.assertEqual([("S53E02", "queued")], [(d["episode"], d["status"]) for d in out["downloads"]])
        card = self.card(out)
        self.assertIsNotNone(card, "the other 4 picks are still being looked for")
        self.assertEqual(4, card["missing_episodes"])
        self.assertEqual(["S53E01", "S53E03", "S53E04", "S53E05"], card["missing_labels"])
        self.assertEqual("S53E01 +3", card["episode"])

    def test_never_in_downloads_and_searching_at_once(self):
        self.save_pick()
        p = self.sonarr.picks
        self.sonarr.queue = [_grab(SESAME, p[0], qid=1, done=40), _grab(SESAME, p[3], qid=2)]
        out = self.poll()
        downloading = {d["episode"] for d in out["downloads"]}
        self.assertEqual({"S53E01", "S53E04"}, downloading)
        self.assertEqual({"downloading", "queued"}, {d["status"] for d in out["downloads"]})
        card = self.card(out)
        self.assertFalse(downloading & set(card["missing_labels"]))
        self.assertEqual(3, card["missing_episodes"])
        self.assertEqual("S53E02 +2", card["episode"])

    def test_all_picks_grabbed_leaves_no_searching_card(self):
        self.save_pick()
        self.sonarr.queue = [_grab(SESAME, e, qid=10 + i) for i, e in enumerate(self.sonarr.picks)]
        out = self.poll()
        self.assertIsNone(self.card(out))
        self.assertEqual(5, len([d for d in out["downloads"] if d["title"] == "Sesame Street"]))

    def test_one_episode_from_asked_for_to_ready_is_in_one_place_at_each_step(self):
        self.save_pick()
        e1 = self.sonarr.picks[0]

        def places(out):
            got = [("downloads", d["status"]) for d in out["downloads"] if d.get("episode") == "S53E01"]
            card = self.card(out)
            if card and "S53E01" in card["missing_labels"]:
                got.append(("searching", card["status"]))
            got += [("ready", r["episode"]) for r in out["recently_downloaded"] if r["title"] == "Sesame Street"]
            return got

        self.assertEqual([("searching", "searching")], places(self.poll()))
        self.sonarr.queue = [_grab(SESAME, e1, qid=7)]                  # grabbed, nothing in yet
        self.assertEqual([("downloads", "queued")], places(self.poll()))
        self.sonarr.queue = [_grab(SESAME, e1, qid=7, done=60)]
        self.assertEqual([("downloads", "downloading")], places(self.poll()))
        e1["hasFile"] = True                                            # Sonarr imported it
        self.sonarr.queue = []
        token = download_readiness.begin("series", 502, "Sesame Street", "S53E01", "sonarr")
        self.assertEqual([("downloads", "importing")], places(self.poll()))
        self.db.add(mdb.Series(tmdb_id=502, title="Sesame Street", source="sonarr",
                               jellyfin_item_id="f" * 32, last_downloaded_episode="S53E01"))
        self.db.commit()
        download_readiness.end(token, ready=True)
        out = self.poll()
        self.assertEqual([("ready", "S53E01")], places(out))
        self.assertEqual(4, self.card(out)["missing_episodes"], "the other picks are still searching")

    def test_after_the_search_ends_the_unfound_picks_are_not_shown_as_searching(self):
        # Sonarr does not look again for an unmonitored show's episodes, so
        # once its search has finished they are not "searching" any more.
        self.save_pick()
        self.poll()
        self.save_pick(status="completed")
        self.assertIsNone(self.card(self.poll()))

    def test_only_the_episodes_sonarr_is_looking_for(self):
        # One pick already has a file, one was unticked again: neither counts.
        p = self.sonarr.picks
        p[4]["hasFile"] = True
        p[3]["monitored"] = False
        self.save_pick()
        card = self.card(self.poll())
        self.assertEqual(["S53E01", "S53E02", "S53E03"], card["missing_labels"])

    def test_a_show_with_no_running_search_is_left_out_as_before(self):
        self.sonarr.commands = [{"id": 1, "name": "RefreshMonitoredDownloads", "status": "started",
                                 "body": {"episodeIds": [3001]}}]
        out = self.poll()
        self.assertIsNone(self.card(out))
        self.assertIsNotNone(self.card(out, "Followed Show"), "the ordinary Wanted list is unchanged")

    def test_the_cards_full_episode_list_stays_server_side(self):
        self.save_pick()
        out = self.poll()
        for x in out["searching"]:
            self.assertFalse([k for k in x if k.startswith("_")], x["title"])

    def test_stop_looking_means_the_episodes_on_the_card(self):
        self.save_pick()
        self.poll()
        sonarr_rec = dict(SESAME)
        self.assertEqual({e["id"] for e in self.sonarr.picks},
                         activity._missing_ids_for(self.db, sonarr_rec))

    def test_the_person_who_asked_sees_it_too(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=502, media_type="series", user_id=self.kid.id))
        self.db.commit()
        self.save_pick()
        self.sonarr.queue = [_grab(SESAME, self.sonarr.picks[0], qid=3)]
        out = self.poll(user=self.kid)
        self.assertEqual(["S53E01"], [d["episode"] for d in out["downloads"]])
        self.assertEqual(4, self.card(out)["missing_episodes"])


class TestPicksOnAFollowedShow(_PickBase):
    """A monitored show was always on the Wanted list; a single grab still
    took its whole card."""

    def setUp(self):
        super().setUp()
        self.sonarr.episodes = [_episode(FOLLOWED, 500 + n, 1, n) for n in range(1, 6)]

    def test_five_picks_one_grabbed_shows_four_searching(self):
        self.save_pick([500 + n for n in range(1, 6)])
        self.sonarr.queue = [_grab(FOLLOWED, self.sonarr.episodes[0], qid=9)]
        out = self.poll()
        card = self.card(out, "Followed Show")
        self.assertIsNotNone(card, "4 episodes are still being looked for")
        self.assertEqual(4, card["missing_episodes"])
        self.assertEqual("S01E02 +3", card["episode"])

    def test_no_search_running_the_wanted_list_alone_still_counts(self):
        self.sonarr.queue = [_grab(FOLLOWED, self.sonarr.episodes[2], qid=9)]
        card = self.card(self.poll(), "Followed Show")
        self.assertEqual(["S01E01", "S01E02", "S01E04", "S01E05"], card["missing_labels"])

    def test_a_download_without_an_episode_still_hides_the_card(self):
        # Sonarr did not say which episode it is: whose card it empties is
        # unknown, so the show is treated as downloading, as before.
        grab = _grab(FOLLOWED, self.sonarr.episodes[0], qid=9)
        grab["episode"] = {}
        self.sonarr.queue = [grab]
        self.assertIsNone(self.card(self.poll(), "Followed Show"))


class TestSonarrSearchesNow(unittest.TestCase):
    def setUp(self):
        self.sonarr = FakeSonarr()

    def fetch(self):
        with mock.patch.object(activity.requests, "get", side_effect=self.sonarr.get):
            return {x["title"]: x for x in activity._fetch_sonarr_searching("http://arr:8989", "k")}

    def test_picks_are_read_by_id_with_their_show(self):
        self.sonarr.commands = [_search([3001, 3002])]
        out = self.fetch()
        self.assertEqual(["S53E01", "S53E02"], out["Sesame Street"]["missing_labels"])
        self.assertEqual([3001, 3002], out["Sesame Street"]["_missing_ids"])
        self.assertEqual([[3001, 3002]], self.sonarr.episode_reads)

    def test_a_long_pick_is_read_in_pieces(self):
        self.sonarr.picks = [_episode(SESAME, 10000 + n, 40 + n // 30, n % 30 + 1) for n in range(250)]
        self.sonarr.episodes = list(self.sonarr.picks)
        self.sonarr.commands = [_search([e["id"] for e in self.sonarr.picks])]
        out = self.fetch()
        self.assertEqual(250, out["Sesame Street"]["missing_episodes"])
        self.assertTrue(all(len(ids) <= activity.EPISODE_READ_CHUNK for ids in self.sonarr.episode_reads))
        self.assertEqual(250, sum(len(ids) for ids in self.sonarr.episode_reads))

    def test_episodes_already_on_the_wanted_list_are_not_read_again(self):
        self.sonarr.commands = [_search([400])]   # Followed Show S02E09, on the Wanted list
        out = self.fetch()
        self.assertEqual(1, out["Followed Show"]["missing_episodes"])
        self.assertEqual([], self.sonarr.episode_reads)

    def test_an_unreadable_command_list_is_the_wanted_list_alone(self):
        def get(url, **kw):
            if url.endswith("/api/v3/command"):
                raise activity.requests.ConnectionError("busy")
            return self.sonarr.get(url, **kw)
        with mock.patch.object(activity.requests, "get", side_effect=get):
            out = activity._fetch_sonarr_searching("http://arr:8989", "k")
        self.assertEqual(["Followed Show"], [x["title"] for x in out])

    def test_an_episode_read_failing_is_the_wanted_list_alone(self):
        self.sonarr.commands = [_search([3001])]

        def get(url, **kw):
            if url.endswith("/api/v3/episode"):
                raise activity.requests.ConnectionError("busy")
            return self.sonarr.get(url, **kw)
        with mock.patch.object(activity.requests, "get", side_effect=get):
            out = activity._fetch_sonarr_searching("http://arr:8989", "k")
        self.assertEqual(["Followed Show"], [x["title"] for x in out])

    def test_other_and_finished_commands_are_ignored(self):
        self.sonarr.commands = [_search([3001], status="completed", cid=1),
                                _search([3002], status="failed", cid=2),
                                dict(_search([3003], cid=3), name="RenameFiles"),
                                {"id": 4, "name": "EpisodeSearch", "status": "started"},   # no body
                                "garbage"]
        self.assertNotIn("Sesame Street", self.fetch())


if __name__ == "__main__":
    unittest.main()
