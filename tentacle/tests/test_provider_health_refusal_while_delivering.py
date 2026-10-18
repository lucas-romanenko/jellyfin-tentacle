"""A channel refused while another channel of the same provider delivers is
not the provider failing (#652 follow-up).

Run from the tentacle/ directory:  python -m unittest tests.test_provider_health_refusal_while_delivering

Since #652 a live stream refused with 401/403 is noted for provider health,
and a second refusal 120 s or more later marks the provider "failing" and
sends the Pushover alert. A stream that works clears it, but only once per
stretch (at its 10th second). So during an hour-long recording on channel 2,
two tries of channel 1 that this account may not play (not in the package,
a locked channel) a few minutes apart marked the provider failing and sent
"<provider> is failing", while the recording on that provider was delivering
the whole time. The hourly check stands aside during a recording, so nothing
cleared it until the recording ended.

Now a refusal is not noted while another channel of the same provider is
delivering. An outage still shows: then every stream of the provider is
reconnecting, not delivering.
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
        self.acme, self.locked, self.recording, self.elsewhere = acme.id, locked.id, recording.id, elsewhere.id
        db.close()
        self.clock = [datetime(2026, 10, 8, 20, 0).astimezone()]
        self.sent = []
        for p in (patch("services.provider_health._now", side_effect=lambda: self.clock[0]),
                  patch("services.pushover.send", side_effect=lambda db, title, *a, **k: self.sent.append(title) or True),
                  patch.object(livetv, "SessionLocal", self.Session)):
            p.start()
            self.addCleanup(p.stop)

    def delivering(self, channel_id, fresh=True):
        """An upstream that has been streaming past _HEALTHY_AFTER (heard)."""
        entry = livetv._status_open(channel_id)
        self.addCleanup(livetv._status_clear, channel_id, entry)
        now = asyncio.get_running_loop().time()
        entry["since"] = now - 3600
        entry["heard_since"] = entry["since"]
        entry["last_ok"] = now if fresh else now - 2 * livetv._RIVAL_STREAMING_FRESH
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
        entry = await self.refused_twice()
        self.assertNotEqual("failing", entry.get("state"),
                            "provider marked failing while a recording on it delivers")
        self.assertEqual([], self.sent)

    async def test_a_stream_of_another_provider_says_nothing_about_this_one(self):
        self.delivering(self.elsewhere)
        entry = await self.refused_twice()
        self.assertEqual("failing", entry.get("state"))
        self.assertEqual(["Tentacle: Acme is failing"], self.sent)

    async def test_an_outage_still_shows_when_the_recording_is_reconnecting(self):
        entry = self.delivering(self.recording)
        entry["state"] = "reconnecting"
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_a_stream_that_stopped_marking_is_not_delivering(self):
        self.delivering(self.recording, fresh=False)
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_a_stream_before_its_tenth_second_is_not_delivering_yet(self):
        entry = self.delivering(self.recording)
        entry["heard_since"] = None
        state = await self.refused_twice()
        self.assertEqual("failing", state.get("state"))

    async def test_without_any_stream_two_refusals_still_mark_it_failing(self):
        state = await self.refused_twice()
        self.assertEqual(("failing", "stream", 403), (state.get("state"), state.get("step"), state.get("code")))


if __name__ == "__main__":
    unittest.main()
