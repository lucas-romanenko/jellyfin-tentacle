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


if __name__ == "__main__":
    unittest.main()
