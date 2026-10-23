"""#760 check: on the lineup M3U tuner (the only tuner Tentacle sets up since
v1.13) Jellyfin 10.11.8's timers carry ExternalChannelId "m3u_" + MD5(tuner
URL) + MD5(stream URL) -- seen on a real 10.11.8 against Tentacle's own
all.m3u. Tentacle's recording lookup reads only "hdhr_" ids, so a recording
on that tuner is never known as one (and /api/live/reserve can't take it)."""
import asyncio
import unittest

from fastapi import HTTPException
from unittest import mock

from test_livetv_recording_priority import _fresh_db

BASE = "http://tentacle.test:8888"
TUNER = BASE + "/api/live/tuner/all.m3u"


class M3uTimersAreRecordings(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._reserved_channels.clear()
        livetv._fired_timers.clear()
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p", live_tv_enabled=True)
        self.db.add(prov)
        self.db.commit()
        self.b = LiveChannel(provider_id=prov.id, name="B", stream_id="999", stream_url="http://p/b.ts", enabled=True)
        self.db.add(self.b)
        set_setting(self.db, "livetv_tuner_base", BASE)
        set_setting(self.db, "livetv_all_tuner_url", TUNER)
        self.db.commit()
        from services import livetv_lineups as L
        self.ext = L.m3u_external_id(TUNER, f"{BASE}/api/live/stream/{self.b.id}")

    def test_an_in_progress_timer_on_the_lineup_tuner_is_a_recording(self):
        timers = {"Items": [{"Id": "t1", "Status": "InProgress", "ExternalChannelId": self.ext,
                             "EndDate": "2099-01-01T00:00:00.0000000Z"}]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            got = self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k")
        self.assertEqual({"999"}, got, "Jellyfin is recording channel B on Tentacle's lineup tuner")

    def test_reserve_takes_the_id_a_lineup_timer_carries(self):
        body = self.livetv.ReserveRequest(stream_id=self.ext, seconds=60)
        try:
            out = asyncio.run(self.livetv.live_reserve(body, db=self.db))
        except HTTPException as e:
            self.fail(f"/api/live/reserve answered {e.status_code} for the timer's id {self.ext}")
        self.assertEqual(self.b.id, out["channel_id"])
