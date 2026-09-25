"""A YouTube Live TV channel must not share a guide number with an IPTV channel (#179).

Run from the tentacle/ directory:  python -m unittest discover -s tests

YouTube channels were numbered 9000 + id, "well clear of IPTV stream ids", but
IPTV channels use their Xtream stream id as GuideNumber and a panel of any
size has ids in 9001-9999. Jellyfin's HDHomeRun tuner names a channel
hdhr_<GuideNumber>, so the two became one channel item: one vanished from
Live TV and the guide mapped both schedules onto the other.
"""
import logging
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
from services.youtube import livetv as yt_livetv


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class GuideNumberCollision(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.iptv = mdb.LiveChannel(provider_id=p.id, name="IPTV 9001", stream_id="9001",
                                    stream_url="http://192.0.2.10/live/9001.ts", enabled=True)
        self.db.add(self.iptv)
        self.yt = mdb.YouTubeChannel(input_url="u", kind="channel", channel_id="UC" + "x" * 22,
                                     title="YT Live", slug="yt-live", live_enabled=True,
                                     enabled=True, extra_tags=[])
        self.db.add(self.yt)
        self.db.commit()
        self.assertEqual(1, self.yt.id, "the collision below needs YouTube channel id 1")
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.client = TestClient(app)

    def _lineup_numbers(self):
        return [e["GuideNumber"] for e in self.client.get("/hdhr/lineup.json").json()]

    def test_the_lineup_has_no_duplicate_numbers(self):
        numbers = self._lineup_numbers()
        self.assertEqual(len(numbers), len(set(numbers)), numbers)
        self.assertIn("9001", numbers)
        self.assertIn("90001", numbers)

    def test_the_guide_has_no_duplicate_channel_ids(self):
        root = ET.fromstring(self.client.get("/hdhr/xmltv.xml").content)
        ids = [c.get("id") for c in root.findall("channel")]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_a_disabled_iptv_channel_does_not_move_the_youtube_one(self):
        self.iptv.enabled = False
        self.db.commit()
        self.assertEqual("9001", yt_livetv.live_channels(self.db)[0]["guide_number"])

    def test_a_pinned_number_is_kept(self):
        self.yt.channel_number = "9001"
        self.db.commit()
        self.assertEqual("9001", yt_livetv.live_channels(self.db)[0]["guide_number"])

    def test_the_youtube_page_reports_the_number_the_lineup_uses(self):
        import routers.youtube as yt_router
        rows = {r["title"]: r for r in yt_router.list_channels(db=self.db)}
        self.assertEqual("90001", rows["YT Live"]["guide_number"])


if __name__ == "__main__":
    unittest.main()
