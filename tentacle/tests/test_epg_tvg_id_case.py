"""#523: a tvg-id that differs from the feed's channel id only in letter case
("cnn.us" vs "CNN.us") is the feed's channel, as Jellyfin's own M3U tuner
reads it (its guide ids are looked up ignoring case).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The tvg-id was compared exactly, so the channel went to the name pass: one
copy got the guide by name and was shown "EPG (by name)", HD/FHD copies of one
channel (same tvg-id, same name) got none, and the sync said "a tvg-id the
feed lacks", which it doesn't.
"""
import logging
import os
import shutil
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
from services.epg_match import coverage_report, resolve_guide_ids
from tmp_dirs import temp_dir

FEED = [{"id": "CNN.us", "names": ["CNN"]}]


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _ch(cid, name, tvg):
    return {"id": cid, "name": name, "tvg_id": tvg, "override": None, "enabled": True}


class Resolve(unittest.TestCase):
    def test_exact_case_as_before(self):
        r = resolve_guide_ids([_ch(1, "News 24/7", "CNN.us")], FEED)[1]
        self.assertEqual(("tvg-id", "CNN.us", None), (r["method"], r["guide_id"], r["name_match"]))

    def test_other_case_takes_the_feeds_spelling(self):
        r = resolve_guide_ids([_ch(1, "News 24/7", "cnn.us")], FEED)[1]
        self.assertEqual(("tvg-id", "CNN.us"), (r["method"], r["guide_id"]), r["reason"])
        # Stored where a name match is (epg_name_match), so the guide is served
        # under the id the feed uses.
        self.assertEqual("CNN.us", r["name_match"])

    def test_hd_and_fhd_copies_both_get_it(self):
        r = resolve_guide_ids([_ch(1, "US: CNN HD", "cnn.us"), _ch(2, "US: CNN FHD", "cnn.us")], FEED)
        self.assertEqual(["CNN.us", "CNN.us"], [r[1]["guide_id"], r[2]["guide_id"]],
                         [r[1]["reason"], r[2]["reason"]])
        self.assertEqual(["tvg-id", "tvg-id"], [r[1]["method"], r[2]["method"]])

    def test_an_exact_match_wins(self):
        feed = FEED + [{"id": "cnn.us", "names": ["CNN Other"]}]
        r = resolve_guide_ids([_ch(1, "News", "cnn.us"), _ch(2, "News", "CNN.us")], feed)
        self.assertEqual(("tvg-id", "cnn.us", None), (r[1]["method"], r[1]["guide_id"], r[1]["name_match"]))
        self.assertEqual(("tvg-id", "CNN.us", None), (r[2]["method"], r[2]["guide_id"], r[2]["name_match"]))

    def test_two_feed_ids_that_differ_only_in_case_are_not_guessed(self):
        feed = [{"id": "CNN.us", "names": ["CNN"]}, {"id": "Cnn.Us", "names": ["Cable News"]}]
        r = resolve_guide_ids([_ch(1, "Mystery", "cnn.us")], feed)[1]
        self.assertEqual((None, "tvg-id-not-in-feed"), (r["guide_id"], r["reason"]))
        # ... and the name pass still runs for it, as for any tvg-id the feed lacks.
        r = resolve_guide_ids([_ch(1, "US: Cable News", "cnn.us")], feed)[1]
        self.assertEqual(("name", "Cnn.Us"), (r["method"], r["guide_id"]))

    def test_coverage_counts_it_by_tvg_id(self):
        channels = [_ch(1, "US: CNN HD", "cnn.us")]
        resolved = resolve_guide_ids(channels, FEED)
        report = coverage_report(channels, resolved, {"CNN.us"})
        self.assertEqual((1, 1, 0, 0), (report["enabled"]["with_guide"], report["enabled"]["by_tvg_id"],
                                        report["enabled"]["by_name"], report["enabled"]["tvg_id_not_in_feed"]))
        self.assertEqual([], report["by_name"])


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class EpgSync(unittest.TestCase):
    """The real sync and the real guide, with the feed in the disk cache."""

    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        for sid, name in (("1", "US: CNN HD"), ("2", "US: CNN FHD")):
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, epg_channel_id="cnn.us",
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        start = datetime.utcnow() + timedelta(hours=1)
        feed = ('<tv><channel id="CNN.us"><display-name>CNN</display-name></channel>'
                f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                'channel="CNN.us"><title>The News</title></programme></tv>')
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(feed)

    def _sync(self):
        data = {"id": self.pid, "channels": [], "enabled_count": 2, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        # A sync that keeps no programmes retries after 30 s and 60 s.
        with mock.patch.object(livetv_router, "SessionLocal", self.Session), mock.patch("time.sleep"):
            return livetv_router._run_epg_sync_background(data)

    def _client(self):
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        return TestClient(app)

    def test_both_copies_get_the_guide_under_the_feeds_id(self):
        self.assertTrue(self._sync(), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        rows = self.db.query(mdb.LiveChannel).all()
        self.assertEqual([("CNN.us", "tvg-id")] * 2, [(r.guide_epg_id, r.epg_match) for r in rows])
        self.assertIn("<title>The News</title>", self._client().get("/hdhr/xmltv.xml").text)

    def test_the_channel_list_and_sync_line_say_tvg_id(self):
        self._sync()
        listed = self._client().get("/api/live/channels").json()["channels"]
        self.assertEqual([(True, "tvg-id")] * 2, [(c["has_epg_data"], c["epg_match"]) for c in listed])
        message = livetv_router._get_sync_status(self.pid)["message"]
        self.assertIn("guide for 2 of 2 enabled channels", message)
        self.assertNotIn("a tvg-id the feed lacks", message)
        self.assertNotIn("by name", message)

    def test_a_second_sync_succeeds(self):
        self.assertTrue(self._sync())
        self.assertTrue(self._sync(), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="CNN.us").count())


if __name__ == "__main__":
    unittest.main()
