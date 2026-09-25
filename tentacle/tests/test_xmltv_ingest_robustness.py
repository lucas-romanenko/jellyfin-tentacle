"""Provider XMLTV that is valid but unusual must still produce a guide.

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #176: `<title></title>` has text None, and epg_programs.title is NOT NULL,
  so one such programme failed the whole EPG sync for every channel.
- #177: the DTD allows any initial substring of YYYYMMDDhhmmss. A fixed
  14-character slice turned "202609241800 +0200" into "202609241800 +", which
  never parsed, so a minute-precision guide came out empty.
- #147/#148: a programme's <icon> was stored but never served, and the
  provider's <sub-title> and <icon> were not read at all.
"""
import logging
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.xmltv as xmltv
from services.xmltv import _parse_xmltv_time, parse_xmltv, generate_xmltv


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class XmltvTimes(unittest.TestCase):
    def test_full_precision_with_offsets(self):
        self.assertEqual(datetime(2026, 3, 24, 5, 0), _parse_xmltv_time("20260324060000 +0100"))
        self.assertEqual(datetime(2026, 3, 24, 5, 0), _parse_xmltv_time("20260324060000 +01:00"))
        self.assertEqual(datetime(2026, 3, 24, 11, 0), _parse_xmltv_time("20260324060000 -0500"))
        self.assertEqual(datetime(2026, 3, 24, 6, 0), _parse_xmltv_time("20260324060000"))
        self.assertEqual(datetime(2026, 3, 24, 5, 0), _parse_xmltv_time("20260324060000+0100"))

    def test_minute_precision(self):
        self.assertEqual(datetime(2026, 9, 24, 16, 0), _parse_xmltv_time("202609241800 +0200"))

    def test_the_dtds_own_example(self):
        # A named zone is not a numeric offset: UTC, as in Jellyfin's reader.
        self.assertEqual(datetime(2000, 7, 28, 17, 33), _parse_xmltv_time("200007281733 BST"))

    def test_shorter_substrings_are_padded(self):
        self.assertEqual(datetime(2026, 9, 1, 0, 0), _parse_xmltv_time("202609"))
        self.assertEqual(datetime(2026, 1, 1, 0, 0), _parse_xmltv_time("2026"))

    def test_garbage_is_still_rejected(self):
        for bad in ("", "abc", "20261399000000 +0000", "1"):
            self.assertIsNone(_parse_xmltv_time(bad), bad)

    def test_a_minute_precision_feed_has_programmes(self):
        feed = ('<tv><programme start="202609241800 +0200" stop="202609241930 +0200" channel="a">'
                '<title>News</title></programme></tv>')
        _channels, programs = parse_xmltv(feed)
        self.assertEqual(1, len(programs))
        self.assertEqual(datetime(2026, 9, 24, 16, 0), programs[0]["start"])


class ProgrammeFields(unittest.TestCase):
    FEED = ('<tv>'
            '<programme start="20260924180000 +0000" stop="20260924190000 +0000" channel="a">'
            '<title></title></programme>'
            '<programme start="20260924190000 +0000" stop="20260924200000 +0000" channel="a">'
            '<title/></programme>'
            '<programme start="20260924200000 +0000" stop="20260924230000 +0000" channel="a">'
            '<title>NHL Hockey</title><sub-title>TOR vs MTL</sub-title>'
            '<icon src="http://img.example/nhl.png"/></programme>'
            '</tv>')

    def test_an_empty_title_is_an_empty_string(self):
        _c, programs = parse_xmltv(self.FEED)
        self.assertEqual(["", "", "NHL Hockey"], [p["title"] for p in programs])

    def test_sub_title_and_icon_are_read(self):
        _c, programs = parse_xmltv(self.FEED)
        self.assertEqual("TOR vs MTL", programs[2]["sub_title"])
        self.assertEqual("http://img.example/nhl.png", programs[2]["icon_url"])
        self.assertIsNone(programs[0]["sub_title"])
        self.assertIsNone(programs[0]["icon_url"])

    def test_generated_guide_carries_them_in_dtd_order(self):
        xml = generate_xmltv([{"id": "1", "name": "C"}], [{
            "channel_id": "1", "title": "NHL Hockey", "sub_title": "TOR vs MTL",
            "description": "d", "start": datetime(2026, 9, 24, 20), "stop": datetime(2026, 9, 24, 23),
            "category": "Sports", "icon_url": "http://img.example/nhl.png"}])
        prog = xml[xml.index("<programme"):]
        self.assertLess(prog.index("<title>"), prog.index("<sub-title>TOR vs MTL</sub-title>"))
        self.assertLess(prog.index("<sub-title>"), prog.index("<desc>"))
        self.assertIn('<icon src="http://img.example/nhl.png" />', prog)


class EpgSyncStoresAndServes(unittest.TestCase):
    """The real sync and the real /hdhr/xmltv.xml, with the feed in the disk cache."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        provider = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                                live_tv_enabled=True)
        self.db.add(provider)
        self.db.commit()
        self.pid = provider.id
        for sid, epg in (("100", "a.ca"), ("101", "a.ca")):  # two channels share one guide id
            self.db.add(mdb.LiveChannel(provider_id=provider.id, name=f"C{sid}", stream_id=sid,
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts",
                                        epg_channel_id=epg, enabled=True))
        self.db.commit()
        cache_dir = os.path.join(self.tmp, "xmltv_cache")
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        now = datetime.utcnow().replace(microsecond=0)
        self.feed = ('<tv>'
                     f'<programme start="{_stamp(now + timedelta(hours=1))}" stop="{_stamp(now + timedelta(hours=2))}" channel="a.ca">'
                     '<title></title></programme>'
                     f'<programme start="{_stamp(now + timedelta(hours=2))}" stop="{_stamp(now + timedelta(hours=3))}" channel="a.ca">'
                     '<title>NHL Hockey</title><sub-title>TOR vs MTL</sub-title>'
                     '<icon src="http://img.example/nhl.png"/></programme>'
                     '</tv>')
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(self.feed)

    def _sync(self):
        data = {"id": self.pid, "channels": [{"epg_channel_id": "a.ca"}], "enabled_count": 2,
                "epg_url": self.url, "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            return livetv_router._run_epg_sync_background(data)

    def test_an_empty_title_no_longer_fails_the_sync(self):
        self.assertTrue(self._sync())
        self.db.expire_all()
        titles = sorted(p.title for p in self.db.query(mdb.EPGProgram).all())
        self.assertEqual(["", "NHL Hockey"], titles)

    def test_icon_and_sub_title_reach_the_served_guide_for_every_channel(self):
        self.assertTrue(self._sync())
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.db.expire_all()
        xml = TestClient(app).get("/hdhr/xmltv.xml").text
        self.assertEqual(2, xml.count('<icon src="http://img.example/nhl.png" />'))
        self.assertEqual(2, xml.count("<sub-title>TOR vs MTL</sub-title>"))

    def test_a_stored_icon_is_served_without_a_provider_sync(self):
        """#147 on its own: YouTube Live stores icon_url directly."""
        start = datetime.utcnow() + timedelta(hours=1)
        self.db.add(mdb.EPGProgram(channel_id="a.ca", title="Live", start=start,
                                   stop=start + timedelta(hours=1), icon_url="http://i.ytimg.com/x.jpg"))
        self.db.commit()
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        xml = TestClient(app).get("/hdhr/xmltv.xml").text
        self.assertIn('<icon src="http://i.ytimg.com/x.jpg" />', xml)


if __name__ == "__main__":
    unittest.main()
