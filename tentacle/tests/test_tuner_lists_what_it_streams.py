"""The stream routes serve the channels the tuner lists, and only those.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The HDHomeRun lineup, the M3U playlist and the guide list enabled channels
only, but GET/HEAD /api/live/stream/{id} looked the id up without that
filter, so a channel the admin had switched off still streamed to anything
that asked for its id. Both routes now answer a disabled channel exactly
like an unknown id (404), before a connection slot is taken.
"""
import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


class TunerStreamsWhatItLists(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        prov = mdb.Provider(name="p", server_url="http://prov.test", username="a", password="b")
        self.db.add(prov)
        self.db.commit()
        self.on = mdb.LiveChannel(provider_id=prov.id, name="On", stream_url="http://prov.test/1.ts", enabled=True)
        self.off = mdb.LiveChannel(provider_id=prov.id, name="Off", stream_url="http://prov.test/2.ts", enabled=False)
        self.db.add_all([self.on, self.off])
        self.db.commit()

    def test_head(self):
        from routers import livetv
        self.assertEqual(200, asyncio.run(livetv.stream_head(self.on.id, self.db)).status_code)
        for cid in (self.off.id, 999):
            with self.subTest(channel=cid), self.assertRaises(HTTPException) as ctx:
                asyncio.run(livetv.stream_head(cid, self.db))
            self.assertEqual((404, "Channel not found"), (ctx.exception.status_code, ctx.exception.detail))

    def test_get_refuses_a_disabled_channel_before_taking_a_slot(self):
        from routers import livetv
        opened = []

        async def fake_open(channel_id, db, pending):
            opened.append(channel_id)
            return "stream"

        with mock.patch.object(livetv, "_open_shared_upstream", fake_open):
            self.assertEqual("stream", asyncio.run(livetv.stream_proxy(self.on.id, self.db)))
            for cid in (self.off.id, 999):
                with self.subTest(channel=cid), self.assertRaises(HTTPException) as ctx:
                    asyncio.run(livetv.stream_proxy(cid, self.db))
                self.assertEqual(404, ctx.exception.status_code)
        self.assertEqual([self.on.id], opened)


class SeparatorRowsAreNotListed(unittest.TestCase):
    """A provider's "####### SPORTSNET PPV #######" row switched on (before
    #158, or with its group) is no channel: the tuner lists, the guide and
    the HDHomeRun lineup leave it out; one the admin named is a channel."""

    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        prov = mdb.Provider(name="p", server_url="http://prov.test", username="a", password="b")
        self.db.add(prov)
        self.db.commit()
        for i, (name, custom) in enumerate((("CA: TSN 1", None), ("####### SPORTSNET PPV #######", None),
                                            ("##### BLUE SPORT #####", "Blue Sport"))):
            self.db.add(mdb.LiveChannel(id=i + 1, provider_id=prov.id, name=name, custom_name=custom,
                                        stream_id=str(100 + i), stream_url=f"http://prov.test/{i}.ts",
                                        enabled=True, sort_order=i))
        self.db.commit()

    def test_left_out_everywhere(self):
        from starlette.requests import Request
        from routers import livetv
        self.assertEqual([1, 3], [c.id for c in livetv._shown_channels(self.db)])
        self.assertEqual([2], livetv._hidden_separators(self.db))
        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"host", b"t.lan:8888")],
                       "query_string": b"", "scheme": "http", "server": ("t.lan", 8888), "root_path": ""})
        m3u = livetv.live_playlist_m3u(req, db=self.db).body.decode()
        self.assertIn("TSN 1", m3u)
        self.assertIn("Blue Sport", m3u)
        self.assertNotIn("SPORTSNET", m3u)
        lineup = livetv.hdhr_lineup(req, db=self.db)
        self.assertEqual(["CA: TSN 1", "Blue Sport"], [c["GuideName"] for c in lineup])


if __name__ == "__main__":
    unittest.main()
