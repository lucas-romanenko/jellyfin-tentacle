"""A guide id gets one schedule: programmes never overlap.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A provider's feed merged several guide sources under one id: MLBNetwork.us
came as four blocks that disagree ("Live: MLB Baseball" 16:30-19:30 beside
"Live: MLB Central" 17:00-20:00 and 18:00-20:00, ...). Programmes were kept
per (guide id, start), so every block was stored and the guide drew them on
top of each other (channel 19 in the web guide). Now the feed's first word on
a time wins; a later programme fills only time that is still free.
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
from services.xmltv import one_schedule
from tmp_dirs import temp_dir

DAY = datetime(2026, 10, 22)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _p(title, start, stop, channel="MLBNetwork.us"):
    """A programme from "HH:MM" times on the evening of DAY (before 06:00 is
    the night after; a stop before the start is the next day)."""
    s = DAY + timedelta(days=int(start < "06"), hours=int(start[:2]), minutes=int(start[3:]))
    e = DAY + timedelta(days=int(start < "06"), hours=int(stop[:2]), minutes=int(stop[3:]))
    if e < s:
        e += timedelta(days=1)
    return {"channel_id": channel, "title": title, "start": s, "stop": e}


def _times(programs):
    return [(p["title"], p["start"].strftime("%H:%M"), p["stop"].strftime("%H:%M"))
            for p in sorted(programs, key=lambda p: p["start"])]


def _overlaps(programs):
    by = {}
    for p in programs:
        by.setdefault(p["channel_id"], []).append((p["start"], p["stop"]))
    return sum(1 for spans in by.values() for a, b in zip(sorted(spans), sorted(spans)[1:]) if b[0] < a[1])


# Channel 19 as the feed sent it (UTC): the first source's evening, then a
# second source's that disagrees.
MLB = [
    _p("Live: MLB Baseball", "16:30", "19:30"),
    _p("Live: Intentional Talk", "19:30", "21:00"),
    _p("Live: MLB Tonight", "21:00", "23:00"),
    _p("Live: MLB Big Inning", "23:00", "01:30"),
    _p("Live: MLB Central", "17:00", "20:00"),
    _p("Live: MLB Central", "18:00", "20:00"),
    _p("MLB Tonight", "20:00", "21:00"),
    _p("Path to the Pennant", "00:00", "01:00"),
    _p("MLB Central", "01:00", "03:00"),
    _p("Quick Pitch", "03:00", "04:00"),
]


class OneSchedule(unittest.TestCase):
    def test_channel_19s_merged_sources_become_one_schedule(self):
        kept, changed = one_schedule(MLB)
        self.assertEqual(0, _overlaps(kept))
        self.assertEqual([
            ("Live: MLB Baseball", "16:30", "19:30"),
            ("Live: Intentional Talk", "19:30", "21:00"),
            ("Live: MLB Tonight", "21:00", "23:00"),
            ("Live: MLB Big Inning", "23:00", "01:30"),
            # the second source only where the first says nothing: "MLB
            # Central" 01:00-03:00 keeps 01:30-03:00 (three quarters of it)
            ("MLB Central", "01:30", "03:00"),
            ("Quick Pitch", "03:00", "04:00"),
        ], _times(kept))
        self.assertEqual(5, changed)

    def test_a_clean_guide_is_untouched(self):
        clean = [_p("A", "10:00", "11:00"), _p("B", "11:00", "12:30"), _p("C", "12:30", "13:00")]
        kept, changed = one_schedule(clean)
        self.assertEqual(clean, kept)
        self.assertEqual(0, changed)

    def test_guide_ids_are_apart(self):
        both = [_p("A", "10:00", "11:00", "a.us"), _p("B", "10:00", "11:00", "b.us")]
        self.assertEqual((both, 0), one_schedule(both))

    def test_the_same_programme_twice_is_kept_once(self):
        kept, changed = one_schedule([_p("A", "10:00", "11:00"), _p("A again", "10:00", "11:00")])
        self.assertEqual([("A", "10:00", "11:00")], _times(kept))
        self.assertEqual(1, changed)

    def test_a_programme_running_a_little_into_the_next_is_shortened(self):
        kept, _ = one_schedule([_p("B", "11:00", "12:00"), _p("A", "10:00", "11:05")])
        self.assertEqual([("A", "10:00", "11:00"), ("B", "11:00", "12:00")], _times(kept))
        kept, _ = one_schedule([_p("A", "10:00", "11:00"), _p("B", "10:55", "12:00")])
        self.assertEqual([("A", "10:00", "11:00"), ("B", "11:00", "12:00")], _times(kept))

    def test_one_that_mostly_clashes_is_left_out(self):
        kept, changed = one_schedule([_p("A", "10:00", "11:00"), _p("B", "10:30", "11:20")])
        self.assertEqual([("A", "10:00", "11:00")], _times(kept))
        self.assertEqual(1, changed)

    def test_it_takes_the_largest_free_stretch_between_kept_ones(self):
        kept, _ = one_schedule([_p("A", "10:00", "10:10"), _p("B", "11:50", "12:00"), _p("C", "10:00", "12:00")])
        self.assertEqual([("A", "10:00", "10:10"), ("C", "10:10", "11:50"), ("B", "11:50", "12:00")], _times(kept))

    def test_one_inside_a_kept_one_is_left_out(self):
        kept, _ = one_schedule([_p("A", "10:00", "12:00"), _p("B", "10:30", "11:00")])
        self.assertEqual([("A", "10:00", "12:00")], _times(kept))

    def test_one_that_ends_before_it_starts_is_left_out(self):
        bad = dict(_p("A", "10:00", "11:00"), stop=DAY + timedelta(hours=10))
        self.assertEqual(([], 1), one_schedule([bad]))

    def test_feed_order_is_kept(self):
        progs = [_p("B", "11:00", "12:00"), _p("A", "10:00", "11:00")]
        self.assertEqual(["B", "A"], [p["title"] for p in one_schedule(progs)[0]])


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
        self.db.add(mdb.LiveChannel(provider_id=p.id, name="US: MLB NETWORK", stream_id="1",
                                    epg_channel_id="MLBNetwork.us",
                                    stream_url="http://192.0.2.10/live/1.ts", enabled=True))
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        # Tomorrow, so nothing has ended yet when the guide is served.
        shift = timedelta(days=(datetime.utcnow().date() - DAY.date()).days + 1)
        progs = "".join(
            f'<programme start="{_stamp(p["start"] + shift)}" stop="{_stamp(p["stop"] + shift)}" '
            f'channel="MLBNetwork.us"><title>{p["title"]}</title></programme>' for p in MLB)
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write('<tv><channel id="MLBNetwork.us"><display-name>MLB Network</display-name></channel>'
                    + progs + '</tv>')

    def test_stored_and_served_without_overlaps(self):
        data = {"id": self.pid, "channels": [], "enabled_count": 1, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session), mock.patch("time.sleep"):
            self.assertTrue(livetv_router._run_epg_sync_background(data),
                            livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        rows = [{"channel_id": r.channel_id, "title": r.title, "start": r.start, "stop": r.stop}
                for r in self.db.query(mdb.EPGProgram).all()]
        self.assertEqual(6, len(rows))
        self.assertEqual(0, _overlaps(rows))
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        guide = TestClient(app).get("/hdhr/xmltv.xml").text
        self.assertEqual(6, guide.count("<programme "))
        self.assertNotIn("Path to the Pennant", guide)


if __name__ == "__main__":
    unittest.main()
