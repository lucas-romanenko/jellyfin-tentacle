"""#760: on the lineup M3U tuner (the only tuner Tentacle sets up since
v1.13) Jellyfin 10.11's timers carry ExternalChannelId "m3u_" + MD5(tuner
URL) + MD5(stream URL) -- seen on a real 10.11.8 against Tentacle's own
all.m3u. Those are recordings like hdhr_ ones: they take a viewer's slot at
capacity, can be reserved, and the viewer who loses the slot is stopped and
told "Stopped for a recording" instead of being left on a frozen picture."""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from fastapi import HTTPException

from test_livetv_recording_priority import _fresh_db

SAVED = "http://tentacle.test:8888"
TUNER = SAVED + "/api/live/tuner/all.m3u"


class _Base(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._reserved_channels.clear()
        livetv._fired_timers.clear()
        livetv._tentacle_m3u.update(at=-1e9, ids={})
        self.addCleanup(livetv._tentacle_m3u.update, at=-1e9, ids={})
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p", live_tv_enabled=True)
        self.db.add(prov)
        self.db.commit()
        self.a = LiveChannel(provider_id=prov.id, name="A", stream_id="277", stream_url="http://p/a.ts", enabled=True)
        self.b = LiveChannel(provider_id=prov.id, name="B", stream_id="999", stream_url="http://p/b.ts", enabled=True)
        self.db.add_all([self.a, self.b])
        # Tentacle's address changed since the tuner was given to Jellyfin:
        # Jellyfin's ids still hash the saved one.
        set_setting(self.db, "livetv_tuner_base", "http://tentacle-now.test:8888")
        set_setting(self.db, "livetv_all_tuner_url", TUNER)
        self.db.commit()
        from services import livetv_lineups as L
        self.L = L
        self.ext_b = L.m3u_external_id(TUNER, f"{SAVED}/api/live/stream/{self.b.id}")

    def lineup(self, *entries):
        return mock.patch.object(self.L, "lineup", lambda db, *a, **k: list(entries))


class M3uTimersAreRecordings(_Base):
    def test_an_in_progress_timer_on_the_lineup_tuner_is_a_recording(self):
        timers = {"Items": [{"Id": "t1", "Status": "InProgress", "ExternalChannelId": self.ext_b,
                             "EndDate": "2099-01-01T00:00:00.0000000Z"}]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers), \
                mock.patch.object(self.livetv, "SessionLocal", lambda: self.db), \
                mock.patch.object(self.db, "close", lambda: None):
            got = self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k")
        self.assertEqual({"999"}, got, "Jellyfin is recording channel B on Tentacle's lineup tuner")

    def test_a_network_entry_is_the_channel_it_streams_now(self):
        ext = self.L.m3u_external_id(TUNER, f"{SAVED}/api/live/net/7")
        entries = ({"key": "n7", "stream_path": "/api/live/net/7", "channel_id": self.a.id},
                   {"key": f"c{self.b.id}", "stream_path": f"/api/live/stream/{self.b.id}"})
        timers = {"Items": [{"Id": "t1", "Status": "InProgress", "ExternalChannelId": ext,
                             "EndDate": "2099-01-01T00:00:00.0000000Z"}]}
        with self.lineup(*entries), mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            got = self.livetv._recording_stream_ids_from_jellyfin(
                "http://jf.test", "k", lambda: self.livetv._tentacle_m3u_ids(self.db))
        self.assertEqual({"277"}, got)

    def test_a_timer_on_someone_elses_m3u_tuner_is_not_ours(self):
        other = self.L.m3u_external_id("http://elsewhere.test/list.m3u", f"{SAVED}/api/live/stream/{self.b.id}")
        now_addr = self.L.m3u_external_id("http://tentacle-now.test:8888/api/live/tuner/all.m3u",
                                          f"http://tentacle-now.test:8888/api/live/stream/{self.b.id}")
        timers = {"Items": [{"Id": t, "Status": "InProgress", "ExternalChannelId": e,
                             "EndDate": "2099-01-01T00:00:00.0000000Z"}
                            for t, e in (("t1", other), ("t2", now_addr), ("t3", "m3u_5"))]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            got = self.livetv._recording_stream_ids_from_jellyfin(
                "http://jf.test", "k", lambda: self.livetv._tentacle_m3u_ids(self.db))
        self.assertEqual(set(), got)

    def test_a_due_timer_on_the_lineup_tuner_counts_before_jellyfin_marks_it(self):
        from datetime import datetime, timedelta
        soon = (datetime.utcnow() + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        end = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        timers = {"Items": [{"Id": "t1", "Status": "New", "ExternalChannelId": self.ext_b,
                             "StartDate": soon, "EndDate": end}]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            got = self.livetv._recording_stream_ids_from_jellyfin(
                "http://jf.test", "k", lambda: self.livetv._tentacle_m3u_ids(self.db))
        self.assertEqual({"999"}, got)

    def test_an_event_channel_out_of_the_lineup_now_is_still_known(self):
        # no event in its name now: not in the lineup, but Jellyfin still has
        # the channel (until its next refresh) and a timer may be on it
        pool = [{"key": f"c{self.a.id}", "stream_path": f"/api/live/stream/{self.a.id}", "event": "no_event"},
                {"key": f"c{self.b.id}", "stream_path": f"/api/live/stream/{self.b.id}"}]
        with mock.patch.object(self.L, "pool", lambda db: list(pool)):
            ids = self.livetv._tentacle_m3u_ids(self.db)
        self.assertEqual("277", ids.get(self.L.m3u_external_id(TUNER, f"{SAVED}/api/live/stream/{self.a.id}")))
        self.assertEqual("999", ids.get(self.ext_b))

    def test_a_per_person_tuner_of_before_is_ours_too(self):
        from models.database import LiveUserLineup
        old = SAVED + "/api/live/users/abc/tuner.m3u"
        self.db.add(LiveUserLineup(jellyfin_user_id="abc", tuner_url=old))
        self.db.commit()
        ext = self.L.m3u_external_id(old, f"{SAVED}/api/live/stream/{self.b.id}")
        self.assertEqual("999", self.livetv._tentacle_m3u_ids(self.db).get(ext))

    def test_reserve_takes_the_id_a_lineup_timer_carries(self):
        body = self.livetv.ReserveRequest(stream_id=self.ext_b, seconds=60)
        try:
            out = asyncio.run(self.livetv.live_reserve(body, db=self.db))
        except HTTPException as e:
            self.fail(f"/api/live/reserve answered {e.status_code} for the timer's id {self.ext_b}")
        self.assertEqual(self.b.id, out["channel_id"])

    def test_reserve_refuses_an_m3u_id_that_isnt_ours(self):
        body = self.livetv.ReserveRequest(stream_id="m3u_" + "0" * 64, seconds=60)
        with self.assertRaises(HTTPException) as e:
            asyncio.run(self.livetv.live_reserve(body, db=self.db))
        self.assertEqual(404, e.exception.status_code)


class _FakeJellyfin(BaseHTTPRequestHandler):
    sessions: list = []
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.calls.append(("GET", self.path.split("?")[0], None))
        self._send(200, json.dumps(self.sessions).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        self.calls.append(("POST", self.path, body))
        self._send(204)


class TheBumpedViewerIsTold(_Base):
    def setUp(self):
        super().setUp()
        from models.database import set_setting
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeJellyfin)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        set_setting(self.db, "jellyfin_url", f"http://127.0.0.1:{srv.server_port}")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()
        item_b = self.L.m3u_channel_item_id(TUNER, f"{SAVED}/api/live/stream/{self.b.id}")
        item_a = self.L.m3u_channel_item_id(TUNER, f"{SAVED}/api/live/stream/{self.a.id}")
        dashed = f"{item_b[:8]}-{item_b[8:12]}-{item_b[12:16]}-{item_b[16:20]}-{item_b[20:]}"
        _FakeJellyfin.sessions = [{"Id": "web", "NowPlayingItem": {"Id": dashed}},
                                  {"Id": "tv", "NowPlayingItem": {"Id": item_a}},
                                  {"Id": "idle"}]
        _FakeJellyfin.calls = []
        for target, name, value in ((self.livetv, "SessionLocal", lambda: self.db),
                                    (self.db, "close", lambda: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_only_the_sessions_on_that_channel_are_stopped_and_told(self):
        self.assertEqual(1, self.livetv._tell_stopped_for_recording(self.b.id))
        posts = [(p, b) for m, p, b in _FakeJellyfin.calls if m == "POST"]
        self.assertEqual(["/Sessions/web/Playing/Stop", "/Sessions/web/Message"], [p for p, _ in posts])
        self.assertEqual({"Header": "Live TV", "Text": "Stopped for a recording", "TimeoutMs": 10000}, posts[1][1])

    def test_a_preempted_upstream_tells_its_viewers(self):
        told = []
        livetv = self.livetv

        async def drive():
            shared = livetv._SharedUpstream(self.b.id, lambda: None)
            with mock.patch.object(livetv, "_tell_stopped_for_recording", told.append):
                await shared.preempt()
                await asyncio.gather(*list(livetv._background_tasks))
        asyncio.run(drive())
        self.assertEqual([self.b.id], told)

    def test_jellyfin_unreachable_is_harmless(self):
        from models.database import set_setting
        set_setting(self.db, "jellyfin_url", "http://127.0.0.1:9")
        self.db.commit()
        self.assertEqual(0, self.livetv._tell_stopped_for_recording(self.b.id))


if __name__ == "__main__":
    unittest.main()
