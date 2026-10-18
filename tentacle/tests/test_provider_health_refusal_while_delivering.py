"""A channel refused while another channel of the same provider delivers is
about that channel, not the provider (#674, a #652 follow-up).

A live stream refused with 401/403 is a refusal for provider health; two of
them 120 s or more apart mark the provider failing and send the alert. A
channel this account may not play (not in its package, locked, adult) is
refused every time, so two tries of it during a recording on another channel
marked the provider failing while that recording delivered the whole time.

- a stream of the same provider delivering now (streaming, heard past
  _HEALTHY_AFTER, marked within _RIVAL_STREAMING_FRESH): the refusal is not
  noted, nothing is sent;
- a delivering stream of ANOTHER provider changes nothing;
- a stream of the same provider that is reconnecting, whose last delivery
  is stale, or that has not reached its 10th second changes nothing (a real
  outage still shows), and with no stream at all two refusals still count.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, Provider
import routers.livetv as livetv
from services import provider_health as ph
from tmp_dirs import temp_dir


class RefusalWhileAnotherChannelDelivers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        acme = Provider(name="Acme", server_url="http://panel.example", username="u", password="p",
                        live_tv_enabled=True)
        other = Provider(name="Other", server_url="http://other.example", username="u", password="p",
                         live_tv_enabled=True)
        db.add_all([acme, other])
        db.commit()
        locked = LiveChannel(provider_id=acme.id, stream_id=1, name="Locked",
                             stream_url="http://panel.example/live/u/p/1.ts")
        recording = LiveChannel(provider_id=acme.id, stream_id=2, name="Recording",
                                stream_url="http://panel.example/live/u/p/2.ts")
        elsewhere = LiveChannel(provider_id=other.id, stream_id=3, name="Elsewhere",
                                stream_url="http://other.example/live/u/p/3.ts")
        db.add_all([locked, recording, elsewhere])
        db.commit()
        self.acme, self.locked = acme.id, locked.id
        self.recording, self.elsewhere = recording.id, elsewhere.id
        db.close()
        self.clock = [datetime(2026, 10, 8, 20, 0).astimezone()]
        self.sent = []
        for p in (patch("services.provider_health._now", side_effect=lambda: self.clock[0]),
                  patch("services.pushover.send",
                        side_effect=lambda db, title, *a, **k: self.sent.append(title) or True),
                  patch.object(livetv, "SessionLocal", self.Session)):
            p.start()
            self.addCleanup(p.stop)

    def delivering(self, channel_id, state="streaming", last_ok_ago=0.0):
        """A stream on channel_id that has delivered for an hour (heard at its
        10th second), last marked last_ok_ago seconds ago."""
        entry = livetv._status_open(channel_id)
        self.addCleanup(livetv._status_clear, channel_id, entry)
        now = asyncio.get_running_loop().time()
        entry["since"] = now - 3600
        entry["heard_since"] = entry["since"]
        entry["last_ok"] = now - last_ok_ago
        entry["state"] = state
        return entry

    async def refused_twice(self):
        await livetv._tell_provider_health(self.locked, False, 403)
        self.clock[0] += timedelta(seconds=180)
        await livetv._tell_provider_health(self.locked, False, 403)
        db = self.Session()
        try:
            return ph.load_state(db).get(str(self.acme)) or {}
        finally:
            db.close()

    async def test_a_delivering_recording_keeps_the_provider_ok(self):
        self.delivering(self.recording)
        state = await self.refused_twice()
        self.assertNotEqual("failing", state.get("state"),
                            "provider marked failing while a recording on it delivers")
        self.assertEqual([], self.sent)

    async def test_a_stream_of_another_provider_changes_nothing(self):
        self.delivering(self.elsewhere)
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))
        self.assertEqual(["Tentacle: Acme is failing"], self.sent)

    async def test_a_reconnecting_stream_of_the_provider_changes_nothing(self):
        self.delivering(self.recording, state="reconnecting", last_ok_ago=30)
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_a_stale_stream_of_the_provider_changes_nothing(self):
        self.delivering(self.recording, last_ok_ago=livetv._RIVAL_STREAMING_FRESH + 5)
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_a_stream_before_its_tenth_second_is_not_delivering_yet(self):
        entry = self.delivering(self.recording)
        entry["heard_since"] = None
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_without_any_stream_two_refusals_still_mark_it_failing(self):
        state = await self.refused_twice()
        self.assertEqual(("failing", "stream", 403),
                         (state.get("state"), state.get("step"), state.get("code")))


if __name__ == "__main__":
    unittest.main()
