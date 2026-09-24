"""Which stream an Xtream channel URL asks the provider for is a setting:
HLS (.m3u8, the default), continuous MPEG-TS (.ts), or auto.

Run from the tentacle/ directory:  python -m unittest discover -s tests

HLS is a playlist reload every few seconds plus one request per segment --
about 3,600 requests over a three-hour game. On a connection-limited
account each one is a chance for the provider to answer 509 because
something else opened a connection meanwhile (seen live 2026-09-23: every
509 that split an NHL recording landed on a playlist refresh). The
continuous .ts stream is ONE connection for the whole programme, served
through the raw-TS path with its re-dial (#103).
"""
import json
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _set(self, key, value):
        from models.database import set_setting
        set_setting(self.db, key, value)
        self.db.commit()


class FormatSetting(_Base):
    def test_default_is_hls_so_nothing_changes_for_existing_installs(self):
        from routers import livetv
        self.assertEqual("m3u8", livetv._live_stream_format(self.db))
        self.assertEqual("m3u8", livetv._live_extension(self.db, 1))

    def test_ts_and_auto_are_accepted_and_garbage_keeps_the_default(self):
        from routers import livetv
        for v in ("ts", "TS ", "auto", "m3u8"):
            self._set("livetv_stream_format", v)
            self.assertEqual(v.strip().lower(), livetv._live_stream_format(self.db))
        self._set("livetv_stream_format", "rtmp")
        self.assertEqual("m3u8", livetv._live_stream_format(self.db))

    def test_explicit_ts_wins_regardless_of_what_the_account_advertises(self):
        from routers import livetv
        self._set("livetv_stream_format", "ts")
        self.assertEqual("ts", livetv._live_extension(self.db, 1))


class AutoUsesWhatTheAccountAdvertises(_Base):
    def test_auto_without_any_record_stays_hls(self):
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        self.assertEqual("m3u8", livetv._live_extension(self.db, 1))

    def test_auto_takes_ts_when_the_account_allows_it(self):
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["m3u8", "ts", "rtmp"]}})
        self.db.commit()
        self.assertEqual(["m3u8", "ts", "rtmp"], livetv._remembered_output_formats(self.db, 1))
        self.assertEqual("ts", livetv._live_extension(self.db, 1))
        self.assertEqual("m3u8", livetv._live_extension(self.db, 2), "per provider")

    def test_auto_stays_hls_when_the_account_does_not_allow_ts(self):
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["m3u8"]}})
        self.assertEqual("m3u8", livetv._live_extension(self.db, 1))

    def test_a_reply_without_formats_leaves_the_record_alone(self):
        from routers import livetv
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["ts"]}})
        livetv._remember_output_formats(self.db, 1, {"user_info": {"status": "Active"}})
        livetv._remember_output_formats(self.db, 1, {})
        self.assertEqual(["ts"], livetv._remembered_output_formats(self.db, 1))

    def test_a_corrupt_record_reads_as_nothing(self):
        from routers import livetv
        self._set("livetv_output_formats_1", "{not json")
        self.assertEqual([], livetv._remembered_output_formats(self.db, 1))


class ChannelSyncWritesTheChosenFormat(_Base):
    def _client(self):
        from services.xtream_client import XtreamClient
        return XtreamClient(server="http://panel.test", username="u", password="p", user_agent="UA")

    def _streams(self):
        return [{"stream_id": 101, "name": "One", "category_id": "5"},
                {"stream_id": 102, "name": "Two", "category_id": "5"}]

    def test_ts_urls_are_written_and_a_change_rewrites_existing_rows_in_place(self):
        from routers import livetv
        from models.database import LiveChannel
        client = self._client()
        livetv._upsert_channels(1, self._streams(), {"5": "Sports"}, client, self.db, extension="m3u8")
        self.db.commit()
        rows = {r.stream_id: r for r in self.db.query(LiveChannel).all()}
        self.assertEqual("http://panel.test/live/u/p/101.m3u8", rows["101"].stream_url)
        rows["101"].enabled = True
        rows["101"].channel_number = 7
        self.db.commit()
        ids = {sid: r.id for sid, r in rows.items()}

        out = livetv._upsert_channels(1, self._streams(), {"5": "Sports"}, client, self.db, extension="ts")
        self.db.commit()
        rows = {r.stream_id: r for r in self.db.query(LiveChannel).all()}
        self.assertEqual({"new": 0, "updated": 2, "total": 2}, out)
        self.assertEqual("http://panel.test/live/u/p/101.ts", rows["101"].stream_url)
        self.assertEqual(ids["101"], rows["101"].id, "the row must be re-keyed in place, not replaced")
        self.assertTrue(rows["101"].enabled)
        self.assertEqual(7, rows["101"].channel_number)

    def test_the_default_call_still_writes_hls(self):
        from routers import livetv
        from models.database import LiveChannel
        livetv._upsert_channels(1, self._streams(), {}, self._client(), self.db)
        self.db.commit()
        self.assertTrue(all(r.stream_url.endswith(".m3u8") for r in self.db.query(LiveChannel).all()))


if __name__ == "__main__":
    unittest.main()
