"""The provider health check waits while Jellyfin is playing or recording anything.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A VOD .strm plays straight from the provider, past Tentacle, so Tentacle's
own stream counters saw nothing: the hourly health check (and the check 2
minutes after every restart) opened its test stream next to the episode
being watched. On a one-connection account that second connection cuts the
first; Jellyfin takes the cut for the end of the file, shows the backdrop and
starts the next episode. Seen on the maintainer's server: Silicon Valley
stopping at random in Jellyfin web. The check opens no test stream at all
now (25 Oct 2026), and its one login still waits while Jellyfin plays, or
records (GET /api/live/recordings/busy's answer).
"""
import unittest
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Setting
from services import provider_health as ph
from test_provider_health import API, SERVER, Base_, FakeResponse, api_ok
from tmp_dirs import temp_dir



class Answer(FakeResponse):
    """Jellyfin's answer: requests' raise_for_status as well."""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


JELLYFIN = "http://jellyfin.example.test:8096"
KEY = "fake-jellyfin-key"
SESSIONS = f"{JELLYFIN}/Sessions"
PLAYING = Answer(200, [{"Client": "Jellyfin Web"},
                             {"Client": "Jellyfin Web", "NowPlayingItem": {"Name": "Articles of Incorporation"}}])
IDLE = Answer(200, [{"Client": "Jellyfin Web"}])


TIMERS = f"{JELLYFIN}/LiveTv/Timers"
NO_TIMERS = Answer(200, {"Items": []})


class HealthCheck(Base_):
    def setUp(self):
        super().setUp()
        self.db.add_all([Setting(key="jellyfin_url", value=JELLYFIN + "/"),
                         Setting(key="jellyfin_api_key", value=KEY)])
        self.db.commit()

    def logins(self):
        return [u for u in self.net.calls if u.startswith(API)]

    def test_nothing_playing_logs_in_once_and_opens_no_stream(self):
        self.run_check({SESSIONS: IDLE, TIMERS: NO_TIMERS, API: api_ok()})
        self.assertEqual(len(self.logins()), 1)
        self.assertFalse([u for u in self.net.calls if "/movie/" in u or "/live/" in u])
        self.assertEqual(self.state()["state"], "ok")

    def test_an_episode_playing_asks_the_provider_nothing(self):
        self.run_check({SESSIONS: IDLE, TIMERS: NO_TIMERS, API: FakeResponse(403)})
        before = self.state()
        result = self.run_check({SESSIONS: PLAYING, TIMERS: NO_TIMERS, API: api_ok()})
        self.assertTrue(result["skipped"])
        self.assertEqual(self.logins(), [])
        s = self.state()
        self.assertEqual((s["state"], s["since"]), (before["state"], before["since"]))
        self.assertIsNotNone(s["skipped_at"])

    def test_jellyfin_recording_asks_the_provider_nothing(self):
        from datetime import datetime, timedelta
        at = lambda h: (datetime.utcnow() + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        recording = Answer(200, {"Items": [{"Status": "InProgress", "Name": "Leafs at Habs",
                                            "StartDate": at(-1), "EndDate": at(2)}]})
        result = self.run_check({SESSIONS: IDLE, TIMERS: recording, API: api_ok()})
        self.assertTrue(result["skipped"])
        self.assertEqual(self.logins(), [])

    def test_playback_starting_during_the_run_stops_it(self):
        # Idle when the run starts; playing once the first provider was
        # asked: the second is not.
        from models.database import Provider
        self.db.add(Provider(name="Second", provider_type="xtream", server_url="http://two.example.test",
                             username="u2", password="p2", active=True))
        self.db.commit()
        calls = []

        def get(url, **kw):
            calls.append(url)
            if url == SESSIONS:
                return PLAYING if any(u.startswith(API) for u in calls) else IDLE
            if url == TIMERS:
                return NO_TIMERS
            return api_ok()
        with mock.patch("services.provider_health.requests.get", side_effect=get):
            ph.check_providers(self.db, confirm_delay=None)
        self.assertTrue(any(u.startswith(API) for u in calls))
        self.assertEqual([u for u in calls if u.startswith("http://two.example.test")], [])

    def test_jellyfin_not_answering_asks_the_provider_nothing(self):
        for answer in (requests.ConnectionError("refused"), Answer(500), Answer(200, None)):
            with self.subTest(answer=answer):
                self.run_check({SESSIONS: answer, TIMERS: NO_TIMERS, API: api_ok()})
                self.assertEqual(self.logins(), [])
        # Sessions fine, its timers unreadable: can't tell, so busy
        self.run_check({SESSIONS: IDLE, TIMERS: Answer(500), API: api_ok()})
        self.assertEqual(self.logins(), [])

    def test_sessions_asked_with_the_key_not_in_the_url(self):
        seen = []

        def get(url, **kw):
            if url == SESSIONS:
                seen.append(kw)
                return IDLE
            return NO_TIMERS if url == TIMERS else api_ok()
        with mock.patch("services.provider_health.requests.get", side_effect=get):
            ph.check_providers(self.db, confirm_delay=None)
        self.assertTrue(seen)
        self.assertEqual(seen[0]["headers"], {"X-Emby-Token": KEY})


class WithoutJellyfin(Base_):
    def test_no_jellyfin_connection_asks_nothing_and_logs_in(self):
        self.run_check({API: api_ok()})
        self.assertFalse([u for u in self.net.calls if "/Sessions" in u or "/LiveTv/" in u])
        self.assertEqual(len([u for u in self.net.calls if u.startswith(API)]), 1)


class Sweep(unittest.TestCase):
    """The 04:30 stream health sweep opens the same kind of connection."""

    def setUp(self):
        import models.database as mdb
        import services.stream_health as sh
        self.sh = sh
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        Session = sessionmaker(bind=engine)
        db = Session()
        for i in range(4):
            db.add(mdb.Movie(tmdb_id=100 + i, title=f"Film {i}", source="provider_1", strm_path=f"/vod/m{i}.strm"))
        db.commit()
        db.close()
        self.probed = []
        self.playing = [False]
        for p in (mock.patch.object(sh, "SessionLocal", Session),
                  mock.patch.object(sh, "_check_item", lambda db, item, t, pr: self.probed.append(item.title) or True),
                  mock.patch.object(sh, "_live_streams_active", lambda: False),
                  mock.patch("services.provider_activity.jellyfin_playing", lambda db: self.playing[0]),
                  mock.patch.object(sh.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_jellyfin_playing_defers_the_sweep(self):
        self.playing[0] = True
        self.sh._sweep()
        self.assertEqual(self.probed, [])

    def test_playback_starting_mid_sweep_stops_it(self):
        def check(db, item, t, pr):
            self.probed.append(item.title)
            self.playing[0] = True
            return True
        with mock.patch.object(self.sh, "_check_item", check):
            self.sh._sweep()
        self.assertEqual(self.probed, ["Film 0"])

    def test_nothing_playing_sweeps(self):
        self.sh._sweep()
        self.assertEqual(len(self.probed), 4)


if __name__ == "__main__":
    unittest.main()
