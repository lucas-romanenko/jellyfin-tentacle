"""#242 follow-up: finishing a request later (at startup, at each daily check)
does only what the request still owes, and only while the user still wants it.

- One read of the album, no waiting: the resume runs every pending album in one
  worker job, and the first try already waited a minute for its releases.
- An album unmonitored in Lidarr since the request is left alone (the resume
  used to monitor it again and search it).
- A request still unfinished after REQUEST_GIVE_UP_DAYS is given up with a
  message on the album, instead of being retried every day for good.
- An album removed from Lidarr since leaves Tentacle's snapshot (it used to stay
  as a "wanted" album nobody could get).
- The same album requested twice at once (a double click, the dashboard and
  Jellyfin, two users) no longer fails the second request with a 500 (both
  inserted its row), and it is searched once.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import random
import unittest
from datetime import datetime, timedelta
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import ARTIST, RG, FakeLidarr, _Base, lidarr_album, lidarr_release, mb_release  # noqa: E402


class _Resume(_Base):
    def setUp(self):
        super().setUp()
        real_get = FakeLidarr.do_GET

        def do_get(handler):   # an album Lidarr no longer has: 404, as the real API answers
            path = handler.path.split("?")[0]
            if path.startswith("/api/v1/album/") and path.rsplit("/", 1)[1].isdigit() \
                    and int(path.rsplit("/", 1)[1]) not in FakeLidarr.state.get("albums", {}):
                FakeLidarr.log.append(("GET", path, {}))
                return handler._send(404, {"message": "NotFound"})
            return real_get(handler)
        p = mock.patch.object(FakeLidarr, "do_GET", do_get)
        p.start()
        self.addCleanup(p.stop)
        FakeLidarr.state["lookup"] = [{"foreignAlbumId": RG, "title": "Radio City",
                                       "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}]
        FakeLidarr.state["after_add"] = lidarr_album(10, RG, [lidarr_release("r13", 13, monitored=True),
                                                              lidarr_release("r12", 12)], title="Radio City")
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star",
                                        "path": "/data/music/Big Star"}]
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]

    def owed(self, days_ago=0):
        """A request whose pin-and-search job a restart lost."""
        from services.media_requests import request_album
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        FakeLidarr.log.clear()
        if days_ago:
            row = self.row()
            row.requested_at = datetime.utcnow() - timedelta(days=days_ago)
            self.db.commit()

    def row(self):
        from models.database import MusicAlbum
        self.db.expire_all()
        return self.db.query(MusicAlbum).filter_by(mbid=RG).first()

    def resume(self):
        from services.music import jobs
        jobs.finish_pending_requests(self.db)

    def searched(self):
        return any((c[2] or {}).get("name") == "AlbumSearch" for c in self.calls("POST", "/api/v1/command"))

    def album_reads(self):
        return len(self.calls("GET", "/api/v1/album/10"))


class TestResume(_Resume):
    def test_an_album_unmonitored_since_is_left_alone(self):
        self.owed()
        FakeLidarr.state["albums"][10]["monitored"] = False
        self.resume()
        self.assertEqual(self.calls("PUT", "/api/v1/album/monitor"), [])
        self.assertFalse(self.searched())
        row = self.row()
        self.assertFalse(row.request_pending)
        self.assertIn("unmonitored in Lidarr", row.verdict["state"])

    def test_the_resume_reads_the_album_once_and_does_not_wait(self):
        self.owed()
        FakeLidarr.state["albums"][10]["releases"] = []
        self.resume()
        self.assertEqual(self.album_reads(), 1)
        self.assertTrue(self.row().request_pending)   # tried again at the next check

    def test_a_request_unfinished_for_two_weeks_is_given_up_with_a_message(self):
        from services.music import jobs, worker
        self.owed(days_ago=jobs.REQUEST_GIVE_UP_DAYS + 1)
        FakeLidarr.state["albums"][10]["releases"] = []
        self.resume()
        row = self.row()
        self.assertFalse(row.request_pending)
        self.assertIn("Request it again", row.verdict["state"])
        self.assertIn("given up", worker.state["last_error"]["message"])
        self.resume()
        self.assertEqual(self.album_reads(), 1)       # not tried again

    def test_a_recent_request_is_not_given_up(self):
        from services.music import jobs
        self.owed(days_ago=jobs.REQUEST_GIVE_UP_DAYS - 1)
        FakeLidarr.state["albums"][10]["releases"] = []
        self.resume()
        self.assertTrue(self.row().request_pending)

    def test_a_request_that_can_be_finished_is_finished_even_when_old(self):
        from services.music import jobs
        self.owed(days_ago=jobs.REQUEST_GIVE_UP_DAYS + 5)
        self.resume()
        self.assertTrue(self.searched())
        self.assertFalse(self.row().request_pending)

    def test_an_album_removed_from_lidarr_leaves_the_snapshot(self):
        self.owed()
        FakeLidarr.state["albums"] = {}
        self.resume()
        self.assertIsNone(self.row())

    def test_the_first_try_still_waits_for_the_releases(self):
        from services.media_requests import request_album
        from services.music import jobs
        request_album(self.db, RG, user_id=1, via="test")
        self.jobs.clear()
        album = FakeLidarr.state["albums"][10]
        loaded = album["releases"]
        album["releases"] = []
        reads = []

        def sleep(delay):
            reads.append(delay)
            if len(reads) == 2:
                album["releases"] = loaded   # Lidarr has loaded them now
        jobs.finish_request(10, RG, sleep=sleep)(self.db)
        self.assertEqual(len(reads), 2)
        self.assertTrue(self.searched())


class TestResumeProperty(_Resume):
    """Random states of an owed album at the resume. Invariants: the resume never monitors,
    never searches an album with files or unmonitored, reads the album once (and once more
    after a pin), never waits, and an
    album stays owed only while it can still be finished and is under two weeks old."""

    def test_random_states(self):
        from services.music import jobs
        from services.musicbrainz import MusicBrainzError
        seeds = int(os.environ.get("GM_SEEDS", "1000"))
        base = int(os.environ.get("GM_SEED", "20260929"))
        bad = []
        for n in range(seeds):
            seed = base + n
            rnd = random.Random(seed)
            from models.database import MusicAlbum
            self.db.query(MusicAlbum).delete()
            self.db.commit()
            FakeLidarr.state["albums"] = {}
            self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
            age = rnd.choice([0, 3, 13, 15, 40])
            self.owed(days_ago=age)
            album = FakeLidarr.state["albums"][10]
            removed = rnd.random() < 0.1
            monitored = rnd.random() < 0.8
            files = rnd.random() < 0.2
            releases = rnd.random() < 0.7
            mb_down = rnd.random() < 0.2
            album["monitored"] = monitored
            album["statistics"]["trackFileCount"] = 12 if files else 0
            if not releases:
                album["releases"] = []
            if mb_down:
                self.mb[RG] = MusicBrainzError("503", 503)
            if removed:
                FakeLidarr.state["albums"] = {}
            self.resume()
            row = self.row()
            problems = []
            if self.calls("PUT", "/api/v1/album/monitor"):
                problems.append("monitored")
            if self.searched() and (files or not monitored or removed):
                problems.append("searched what it shouldn't")
            if self.album_reads() > (2 if self.searched() else 1):   # one more read after a pin
                problems.append(f"{self.album_reads()} reads")
            can_finish = not removed and monitored and not files and releases and not mb_down
            if removed:
                if row is not None:
                    problems.append("removed album kept")
            else:
                still_owed = bool(row.request_pending)
                should_be_owed = not can_finish and monitored and not files and age <= jobs.REQUEST_GIVE_UP_DAYS
                if still_owed != should_be_owed:
                    problems.append(f"owed={still_owed} expected {should_be_owed}")
                if can_finish and not self.searched():
                    problems.append("not searched")
            if problems:
                bad.append((seed, dict(age=age, removed=removed, monitored=monitored, files=files,
                                       releases=releases, mb_down=mb_down), problems))
        print(f"\n[resume] {seeds} seeds from {base}: failures={len(bad)} {bad[:3]}")
        self.assertEqual(bad, [])


class TestResumeFaultsOverDays(_Resume):
    """Fault injection over the days after a request whose own job was lost. Each day
    Lidarr may answer 503 to the album read, the pin or the search (a search may land
    and its answer be lost), MusicBrainz may be down, and the user may unmonitor the
    album, remove it, or its files may arrive. Invariants after every daily resume:
    it never monitors; it never searches an album that is unmonitored, has files or
    is gone; a request is searched at most once more than the searches whose answer
    was lost; one successful read (two after a pin); an album Lidarr no longer has
    leaves the snapshot; a row stays owed only while under REQUEST_GIVE_UP_DAYS; a
    day on which the request can be finished finishes it; after the give-up age
    nothing is owed any more, and a settled request is never touched again."""

    def setUp(self):
        super().setUp()
        FakeLidarr.state["fail"] = set()
        real_get, real_put, real_post = FakeLidarr.do_GET, FakeLidarr.do_PUT, FakeLidarr.do_POST

        def failing(real, kind):
            def handler(h):
                path = h.path.split("?")[0]
                fail = FakeLidarr.state.get("fail") or set()
                hit = ((kind == "GET" and path == "/api/v1/album/10") or
                       (kind == "PUT" and path == "/api/v1/album/10") or
                       (kind == "POST" and path == "/api/v1/command"))
                if hit and kind in fail:
                    if kind == "POST":   # the search lands; its answer is lost
                        body = h._body()
                        FakeLidarr.log.append(("POST", path, body))
                        FakeLidarr.state["lost_searches"] = FakeLidarr.state.get("lost_searches", 0) + 1
                    else:
                        FakeLidarr.log.append((kind, path, "503"))
                    return h._send(503, {"message": "Service Unavailable"})
                return real(h)
            return handler
        for name, real, kind in (("do_GET", real_get, "GET"), ("do_PUT", real_put, "PUT"),
                                 ("do_POST", real_post, "POST")):
            p = mock.patch.object(FakeLidarr, name, failing(real, kind))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.lidarr.time.sleep")   # the client's retry pauses
        p.start()
        self.addCleanup(p.stop)

    def ok_reads(self):
        return len([c for c in self.calls("GET", "/api/v1/album/10") if c[2] != "503"])

    def test_random_faults_over_the_days(self):
        from models.database import MusicAlbum
        from services.music import jobs
        from services.musicbrainz import MusicBrainzError
        seeds = int(os.environ.get("GM_SEEDS", "1000"))
        base = int(os.environ.get("GM_SEED", "20260929"))
        bad, stats = [], {"finished": 0, "given_up": 0, "left_alone": 0, "removed": 0, "files": 0,
                          "lost_searches": 0, "faults": 0}
        for n in range(seeds):
            seed = base + n
            rnd = random.Random(seed)
            self.db.query(MusicAlbum).delete()
            self.db.commit()
            FakeLidarr.state["albums"], FakeLidarr.state["fail"] = {}, set()
            FakeLidarr.state["lost_searches"] = 0
            self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
            self.owed()
            album = FakeLidarr.state["albums"][10]
            loaded = album["releases"]
            if rnd.random() < 0.5:
                album["releases"] = []
            never_loads = rnd.random() < 0.15   # Lidarr never loads this album's releases
            searches, settled_at, problems = 0, None, []
            for day in range(jobs.REQUEST_GIVE_UP_DAYS + 3):
                albums = FakeLidarr.state["albums"]
                if 10 in albums:
                    a = albums[10]
                    if not a["releases"] and not never_loads and rnd.random() < 0.3:
                        a["releases"] = loaded
                    if rnd.random() < 0.04:
                        a["monitored"] = False
                    if rnd.random() < 0.04:
                        a["statistics"]["trackFileCount"] = 12
                    if rnd.random() < 0.03:
                        FakeLidarr.state["albums"] = {}
                fail = {k for k in ("GET", "PUT", "POST") if rnd.random() < 0.15}
                FakeLidarr.state["fail"] = fail
                stats["faults"] += len(fail)
                mb_down = rnd.random() < 0.15
                self.mb[RG] = MusicBrainzError("503", 503) if mb_down else [mb_release("m1", "1974-01-01", 12)]
                row = self.row()
                if row is not None:
                    row.requested_at = datetime.utcnow() - timedelta(days=day)
                    self.db.commit()
                owed_before = row is not None and bool(row.request_pending)
                a = FakeLidarr.state["albums"].get(10)
                gone = a is None
                unmonitored = bool(a) and not a.get("monitored")
                files = bool(a) and int(a["statistics"].get("trackFileCount") or 0) > 0
                can_finish = (owed_before and not gone and not unmonitored and not files and
                              bool(a["releases"]) and not mb_down and not fail)
                FakeLidarr.log.clear()
                self.resume()
                row = self.row()
                now_searched = len([c for c in self.calls("POST", "/api/v1/command")
                                    if (c[2] or {}).get("name") == "AlbumSearch"])
                searches += now_searched
                where = f"day {day}"
                if self.calls("PUT", "/api/v1/album/monitor"):
                    problems.append(f"{where}: monitored")
                if now_searched and (gone or unmonitored or files):
                    problems.append(f"{where}: searched an album it shouldn't")
                if searches > 1 + FakeLidarr.state["lost_searches"]:
                    problems.append(f"{where}: {searches} searches")
                if "GET" not in fail and self.ok_reads() > (2 if now_searched else 1):
                    problems.append(f"{where}: {self.ok_reads()} reads")
                if not owed_before and FakeLidarr.log:
                    problems.append(f"{where}: touched a settled request")
                if gone and owed_before and "GET" not in fail and row is not None:
                    problems.append(f"{where}: an album Lidarr no longer has kept its row")
                owed = row is not None and bool(row.request_pending)
                if owed and day > jobs.REQUEST_GIVE_UP_DAYS:
                    problems.append(f"{where}: still owed after the give-up age")
                if can_finish and (owed or not now_searched):
                    problems.append(f"{where}: could be finished and wasn't")
                if owed_before and not owed and settled_at is None:
                    settled_at = day
                    state = (row.verdict or {}).get("state", "") if row is not None else ""
                    if row is None:
                        stats["removed"] += 1
                    elif now_searched:
                        stats["finished"] += 1
                    elif "Request it again" in state:
                        stats["given_up"] += 1
                    elif "unmonitored" in state:
                        stats["left_alone"] += 1
                    elif files:
                        stats["files"] += 1
                    else:
                        problems.append(f"{where}: settled without a reason: {state!r}")
            stats["lost_searches"] += FakeLidarr.state["lost_searches"]
            if settled_at is None:
                problems.append("never settled")
            if problems:
                bad.append((seed, problems[:3]))
        print(f"\n[resume-faults] {seeds} seeds from {base}: failures={len(bad)} {stats} {bad[:3]}")
        self.assertEqual(bad, [])


class TestSameAlbumTwiceAtOnce(_Resume):
    def test_the_second_of_two_simultaneous_requests_succeeds_and_one_search_is_sent(self):
        from models.database import MusicAlbum
        from services.media_requests import request_album
        from services.music import library
        FakeLidarr.state["albums"] = {10: lidarr_album(10, RG, [lidarr_release("r13", 13, monitored=True),
                                                                lidarr_release("r12", 12)],
                                                       monitored=False, title="Radio City")}
        real = library.upsert_album
        other, calls = self.Session(), []

        def racing(db, album, artist=None):
            row = real(db, album, artist)
            calls.append(1)
            if len(calls) == 1:          # the other request runs to the end in between
                request_album(other, RG, user_id=2, via="Jellyfin")
            return row
        with mock.patch.object(library, "upsert_album", side_effect=racing):
            out = request_album(self.db, RG, user_id=1, via="dashboard")
        other.close()
        self.assertEqual(out["status"], "requested")
        self.assertEqual(self.db.query(MusicAlbum).filter_by(mbid=RG).count(), 1)
        self.run_jobs()
        searches = [c for c in self.calls("POST", "/api/v1/command") if (c[2] or {}).get("name") == "AlbumSearch"]
        self.assertEqual(len(searches), 1)
        self.assertFalse(self.row().request_pending)

    def test_a_second_request_after_the_first_finished_runs_again(self):
        from services.media_requests import request_album
        request_album(self.db, RG, user_id=1, via="test")
        self.run_jobs()
        request_album(self.db, RG, user_id=1, via="test")   # the user asks again: searched again
        self.run_jobs()
        searches = [c for c in self.calls("POST", "/api/v1/command") if (c[2] or {}).get("name") == "AlbumSearch"]
        self.assertEqual(len(searches), 2)


if __name__ == "__main__":
    unittest.main()
