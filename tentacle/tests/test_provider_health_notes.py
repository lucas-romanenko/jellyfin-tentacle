"""Provider health hears the Live TV channel sync and live streams, and
`note` keeps its rules (#652).

test_provider_health_hears_the_syncs.py covers the VOD sync. Here:

- `note` itself: a stream that works clears only a stream failure, a sync
  that works only an API one; an API 401 reads as a refused login; a refusal
  older than SUSPECT_SECONDS starts the count again; a provider not watched
  is ignored; a note made while the hourly check waits to confirm is not
  written over by the check's save; a check's verdict settles a refusal
  still waiting for its second.
- the channel sync: refused twice (2+ min apart) is failing, a sync that
  read its groups clears an API failure, one restricted group is nothing.
- live streams: delivering for 10 s is heard once per stretch; a raw stream
  that ends on a 401/403, an open refused with 403, an HLS recording refused
  for good are refusals; a stream that ends on a 404 is not. The executor
  note reaches the state on a session of its own.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import json
import unittest
import unittest.mock
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, LiveChannelGroup, Provider, Setting
import routers.livetv as livetv
from services import provider_health as ph
from test_livetv_channel_sync_all_groups_fail import LOGIN, _Session
from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live
from test_livetv_reconnect_budget import _play
from test_livetv_recording_denied_grace import _ip_block
from test_livetv_token_expiry import Panel, _stream
from test_provider_health import API, Base_, FakeResponse
from tmp_dirs import temp_dir

A = b"G" + b"a" * 187          # one TS packet
OTHER_API = "http://other.example.test/player_api.php"


class Notes(Base_):
    def set_failing(self, step, code=403):
        entry = {**ph._unknown(), "name": self.provider.name, "state": "failing",
                 "since": self.clock[0].isoformat(), "step": step, "code": code, "kind": "http",
                 "alerted": True, "reason": "Provider refusing"}
        self.db.add(Setting(key=ph.SETTING_KEY, value=json.dumps({str(self.provider.id): entry})))
        self.db.commit()

    def entry(self):
        self.db.expire_all()
        return ph.load_state(self.db).get(str(self.provider.id)) or {}

    def later(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def test_a_stream_that_works_clears_only_a_stream_failure(self):
        self.set_failing("stream")
        ph.note(self.db, self.provider.id, True, "api")
        self.assertEqual("failing", self.entry()["state"], "a sync working says nothing about streams")
        self.assertEqual([], self.sent)
        ph.note(self.db, self.provider.id, True, "stream")
        self.assertEqual("ok", self.entry()["state"])
        self.assertEqual(["Tentacle: Acme IPTV works again"], [m["title"] for m in self.sent])

    def test_a_stream_that_works_leaves_a_refused_login_alone(self):
        self.set_failing("api")
        ph.note(self.db, self.provider.id, True, "stream")
        self.assertEqual("failing", self.entry()["state"])
        self.assertEqual([], self.sent)

    def test_an_api_401_twice_is_a_refused_login(self):
        ph.note(self.db, self.provider.id, False, "api", 401)
        self.later(ph.RETRY_DELAY_SECONDS)
        ph.note(self.db, self.provider.id, False, "api", 401)
        e = self.entry()
        self.assertEqual(("failing", "login", 401), (e["state"], e["kind"], e["code"]))
        self.assertIn("rejects the login since 07:50", e["reason"], "since the first refusal")
        self.assertIsNone(e["suspect"])
        self.assertEqual(1, len(self.sent))

    def test_a_stream_refused_twice_says_streams(self):
        ph.note(self.db, self.provider.id, False, "stream", 403)
        self.later(300)
        ph.note(self.db, self.provider.id, False, "stream", 403)
        e = self.entry()
        self.assertEqual(("failing", "stream", 403), (e["state"], e["step"], e["code"]))
        self.assertTrue(e["reason"].startswith("Provider refusing streams: 403 since 07:50"), e["reason"])

    def test_a_refusal_an_hour_old_starts_the_count_again(self):
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.later(ph.SUSPECT_SECONDS + 1)
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.assertNotEqual("failing", self.entry()["state"])
        self.later(ph.RETRY_DELAY_SECONDS)
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.assertEqual("failing", self.entry()["state"])
        self.assertIn("since 08:50", self.entry()["reason"])

    def test_a_refusal_while_failing_changes_nothing(self):
        self.set_failing("api", 401)
        before = self.entry()
        ph.note(self.db, self.provider.id, False, "stream", 403)
        self.later(ph.RETRY_DELAY_SECONDS)
        ph.note(self.db, self.provider.id, False, "stream", 403)
        self.assertEqual(before, self.entry())
        self.assertEqual([], self.sent)

    def test_a_provider_not_watched_is_ignored(self):
        self.provider.active = False
        self.provider.live_tv_enabled = False
        self.db.commit()
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.later(ph.RETRY_DELAY_SECONDS)
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.assertEqual({}, ph.load_state(self.db))

    def test_a_check_verdict_settles_a_pending_refusal(self):
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.assertIsNotNone(self.entry()["suspect"])
        self.later(60)
        self.run_check({API: FakeResponse(200, {"user_info": {"auth": 1, "status": "Active"}}),
                        "http://iptv.example.test:8080/movie/": FakeResponse(206)})
        self.assertIsNone(self.entry()["suspect"])
        self.later(ph.RETRY_DELAY_SECONDS)
        ph.note(self.db, self.provider.id, False, "api", 403)
        self.assertNotEqual("failing", self.entry()["state"], "the check's ok came in between")

    def test_a_note_made_while_the_check_waits_is_kept(self):
        other = Provider(name="Other", provider_type="xtream", server_url="http://other.example.test",
                         username="x", password="y", active=True)
        self.db.add(other)
        self.db.commit()
        ph.note(self.db, other.id, False, "stream", 403)

        def wait(seconds):
            self.clock[0] += timedelta(seconds=seconds)
            db = self.Session()        # a stream, on its own session, during the wait
            try:
                ph.note(db, other.id, False, "stream", 403)
            finally:
                db.close()

        from test_provider_health import Net
        net = Net({API: FakeResponse(403, data=b""), OTHER_API: FakeResponse(429)})
        with patch("services.provider_health.requests.get", side_effect=net.get):
            ph.check_providers(self.db, confirm_delay=120, sleep=wait)
        self.db.expire_all()
        state = ph.load_state(self.db)
        self.assertEqual("failing", state[str(self.provider.id)]["state"], "the check's own verdict")
        self.assertEqual("failing", state[str(other.id)]["state"],
                         "the check's save wrote over what a stream noted during its wait")


class ChannelSyncNotes(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        provider = Provider(name="P", server_url="http://panel.example", username=LOGIN[0],
                            password=LOGIN[1], active=True, live_tv_enabled=True)
        db.add(provider)
        db.commit()
        self.pid = provider.id
        db.close()
        self.clock = [datetime(2026, 10, 8, 20, 0).astimezone()]
        for p in (patch("services.provider_health._now", side_effect=lambda: self.clock[0]),
                  patch("services.pushover.requests.post")):
            p.start()
            self.addCleanup(p.stop)
        self.groups = set()

    def sync(self, answers):
        from services.xtream_client import XtreamClient
        db = self.Session()
        for cat in answers:
            if cat not in self.groups:
                self.groups.add(cat)
                db.add(LiveChannelGroup(provider_id=self.pid, name=f"G{cat}", category_id=cat, enabled=True))
        db.commit()
        db.close()
        client = XtreamClient("http://panel.example", *LOGIN)
        client.session = _Session(answers)
        client.guard.gap = 0
        with patch("services.xtream_client.XtreamClient", lambda **kw: client), \
                patch.object(livetv, "SessionLocal", self.Session), \
                patch.object(livetv, "_run_epg_sync_background", lambda data: None):
            livetv._run_channel_sync_background({
                "id": self.pid, "name": "P", "provider_type": "xtream",
                "server_url": "http://panel.example", "username": LOGIN[0], "password": LOGIN[1],
                "user_agent": "x", "m3u_url": None, "epg_url": None,
            })

    def entry(self):
        db = self.Session()
        try:
            return ph.load_state(db).get(str(self.pid)) or {}
        finally:
            db.close()

    def test_refused_twice_two_minutes_apart_is_failing(self):
        self.sync({"1": 403})
        self.assertNotEqual("failing", self.entry().get("state"))
        self.clock[0] += timedelta(seconds=ph.RETRY_DELAY_SECONDS)
        self.sync({"1": 403})
        e = self.entry()
        self.assertEqual(("failing", "api", 403), (e.get("state"), e.get("step"), e.get("code")))

    def test_the_guard_stopping_is_a_refusal(self):
        self.sync({str(n): 403 for n in range(1, 6)})
        self.assertEqual(403, (self.entry().get("suspect") or {}).get("code"))

    def test_a_sync_that_read_its_groups_clears_an_api_failure(self):
        self.sync({"1": 403})
        self.clock[0] += timedelta(seconds=ph.RETRY_DELAY_SECONDS)
        self.sync({"1": 403})
        self.sync({"1": 200})
        self.assertEqual("ok", self.entry().get("state"))

    def test_one_restricted_group_is_not_a_refusal(self):
        self.sync({"1": 403, "2": 200})
        e = self.entry()
        self.assertIsNone(e.get("suspect"))
        self.assertNotEqual("failing", e.get("state"))


class StreamsTell(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.told = []
        p = patch.object(livetv, "_tell_provider_health", lambda *a: self.told.append(a))
        p.start()
        self.addCleanup(p.stop)

    async def test_delivering_for_ten_seconds_is_heard_once_per_stretch(self):
        entry = livetv._status_open(91)
        self.addCleanup(livetv._status_clear, 91, entry)
        livetv._status_set(91, "streaming")
        self.assertEqual([], self.told, "not before 10 s")
        entry["since"] -= livetv._HEALTHY_AFTER
        livetv._status_set(91, "streaming")
        livetv._status_set(91, "streaming")
        self.assertEqual([(91, True)], self.told, "once per stretch")
        livetv._status_set(91, "reconnecting", "dropped")
        livetv._status_set(91, "streaming")
        self.assertEqual(1, len(self.told), "a new stretch starts at 0 s")
        entry["since"] -= livetv._HEALTHY_AFTER
        livetv._status_set(91, "streaming")
        self.assertEqual([(91, True), (91, True)], self.told)

    async def test_a_raw_stream_that_ends_on_403_is_a_refusal(self):
        for status in (403, 401):
            self.told.clear()
            script = {PANEL: [_redirect(), _resp(status, PANEL)], TOKENIZED: [_live([A], then=_dropped())]}
            await _play(script, failure_budget=120, is_recording=lambda: False)
            self.assertEqual([(1, False, status)], self.told)

    async def test_a_raw_stream_that_ends_on_404_is_not(self):
        script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [_live([A], then=_dropped())]}
        await _play(script, failure_budget=120, is_recording=lambda: False)
        self.assertEqual([], self.told)

    async def test_a_recording_that_rides_out_a_403_is_not(self):
        script = {PANEL: [_redirect()] + [_resp(403, PANEL)] * 3 + [_redirect(), _resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped()), _live([A])]}
        await _play(script, failure_budget=120, is_recording=lambda: True)
        self.assertEqual([], self.told, "the provider answered again before it ended")

    async def test_an_open_refused_with_403_is_a_refusal(self):
        with self.assertRaises(HTTPException):
            await _play({PANEL: [_resp(403, PANEL)]}, failure_budget=120)
        self.assertEqual([(1, False, 403)], self.told)

    async def test_an_hls_recording_refused_for_good_is_a_refusal(self):
        panel = _ip_block(Panel(ttl=50), start_seq=4, n=10 ** 9)
        with patch.object(livetv, "_DENIED_GRACE", 0.3, create=True):
            got, ended = await _stream(panel, segments=50, recording=True)
        self.assertTrue(ended)
        self.assertIn((7, False, 403), self.told)


class StreamNoteReachesTheState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        provider = Provider(name="P", server_url="http://panel.example", username="u", password="p",
                            live_tv_enabled=True)
        db.add(provider)
        db.commit()
        channel = LiveChannel(provider_id=provider.id, stream_id=1, name="One",
                              stream_url="http://panel.example/live/u/p/1.ts")
        db.add(channel)
        db.commit()
        self.pid, self.cid = provider.id, channel.id
        db.close()
        self.clock = [datetime(2026, 10, 8, 20, 0).astimezone()]
        for p in (patch("services.provider_health._now", side_effect=lambda: self.clock[0]),
                  patch("services.pushover.requests.post"),
                  patch.object(livetv, "SessionLocal", self.Session)):
            p.start()
            self.addCleanup(p.stop)

    async def test_two_refused_streams_then_one_that_works(self):
        await livetv._tell_provider_health(self.cid, False, 403)
        self.clock[0] += timedelta(seconds=ph.RETRY_DELAY_SECONDS)
        await livetv._tell_provider_health(self.cid, False, 403)
        db = self.Session()
        try:
            e = ph.load_state(db)[str(self.pid)]
            self.assertEqual(("failing", "stream"), (e["state"], e["step"]))
            await livetv._tell_provider_health(self.cid, True)
            db.expire_all()
            self.assertEqual("ok", ph.load_state(db)[str(self.pid)]["state"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
