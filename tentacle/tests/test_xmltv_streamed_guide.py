"""The served XMLTV guide is streamed, and is the same guide as before.

Run from tentacle/:  python -m unittest tests.test_xmltv_streamed_guide

The guide was built whole in memory on every request (ORM rows, dicts, an
element tree, one string: about 2 KB per programme), so a large lineup with a
long guide took gigabytes per guide download. It is now written as it is
read; the output is byte for byte what ElementTree wrote, and XML-illegal
characters are still stripped (#260, tests/test_guide_xml_well_formed.py).
"""
import asyncio
import glob
import os
import random
import tempfile
import tracemalloc
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
from services.xmltv import generate_xmltv
from tmp_dirs import temp_dir


def _elementtree_xmltv(channels, programs):
    """The ElementTree generator as it was before streaming (#260's stripping
    is a no-op for the clean text these tests use): the reference output."""
    root = ET.Element("tv", attrib={"generator-name": "Tentacle"})
    for ch in channels:
        channel_el = ET.SubElement(root, "channel", attrib={"id": ch["id"]})
        ET.SubElement(channel_el, "display-name").text = ch["name"]
        if ch.get("logo_url"):
            ET.SubElement(channel_el, "icon", attrib={"src": ch["logo_url"]})
    for prog in programs:
        prog_el = ET.SubElement(root, "programme", attrib={
            "start": prog["start"].strftime("%Y%m%d%H%M%S +0000"),
            "stop": prog["stop"].strftime("%Y%m%d%H%M%S +0000"),
            "channel": prog["channel_id"],
        })
        ET.SubElement(prog_el, "title").text = prog.get("title") or ""
        if prog.get("sub_title"):
            ET.SubElement(prog_el, "sub-title").text = prog["sub_title"]
        if prog.get("description"):
            ET.SubElement(prog_el, "desc").text = prog["description"]
        if prog.get("category"):
            ET.SubElement(prog_el, "category").text = prog["category"]
        if prog.get("icon_url"):
            ET.SubElement(prog_el, "icon", attrib={"src": prog["icon_url"]})
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


T0 = datetime(2026, 11, 1, 5)


class StreamedGuideIsStillClean(unittest.TestCase):
    """#260's stripping, through the streamed writer and the streamed route."""

    def _gen(self, name="Ch", title="T", desc=None, cid="1001", icon=None):
        return generate_xmltv([{"id": cid, "name": name, "logo_url": icon}],
                              [{"channel_id": cid, "title": title, "description": desc,
                                "start": T0, "stop": T0 + timedelta(hours=1)}])

    def test_illegal_characters_in_attributes_and_non_characters(self):
        root = ET.fromstring(self._gen(cid="x\x01y", icon="http://img.example/a\x1f.png",
                                       title="a￾b￿c"))
        self.assertEqual("xy", root.find("channel").get("id"))
        self.assertEqual("xy", root.find("programme").get("channel"))
        self.assertEqual("http://img.example/a.png", root.find("channel/icon").get("src"))
        self.assertEqual("abc", root.find("programme/title").text)

    def test_the_served_guide_drops_them_too(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        db.add(p)
        db.commit()
        db.add(mdb.LiveChannel(provider_id=p.id, name="Bad\x0bName", custom_name="Esc\x1bName",
                               stream_id="1001", stream_url="http://192.0.2.10/live/1001.ts",
                               epg_channel_id="a.ca", enabled=True))
        now = datetime.utcnow().replace(microsecond=0)
        db.add(mdb.EPGProgram(channel_id="a.ca", title="Game\x00", start=now, stop=now + timedelta(hours=1)))
        db.commit()
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: db
        root = ET.fromstring(TestClient(app).get("/hdhr/xmltv.xml").content)
        self.assertEqual("EscName", root.find("channel/display-name").text)
        self.assertEqual("Game", root.find("programme/title").text)


class SameOutputAsElementTree(unittest.TestCase):
    """For text without illegal characters the output is byte for byte what
    ElementTree wrote, so Jellyfin reads the same guide."""

    def test_edge_cases(self):
        channels = [
            {"id": "1001", "name": "A & B <HD>", "logo_url": "http://l.example/a.png?x=1&y=\"2\""},
            {"id": "1002", "name": None, "logo_url": None},
            {"id": "id with\ttab\nnewline\rcr", "name": "", "logo_url": ""},
            {"id": "90001", "name": "日本語 – «Liga» 'q'", "logo_url": None},
        ]
        programs = [
            {"channel_id": "1001", "title": "", "start": T0, "stop": T0 + timedelta(minutes=30)},
            {"channel_id": "1001", "title": None, "sub_title": "", "description": "", "category": "",
             "icon_url": "", "start": T0, "stop": T0},
            {"channel_id": "1002", "title": "NHL <Hockey> & \"more\"", "sub_title": "TOR vs MTL",
             "description": "line1\nline2\ttab\r\n>", "category": "Sports",
             "icon_url": "http://img.example/n.png?a=1&b=2", "start": T0, "stop": T0 + timedelta(hours=3)},
            {"channel_id": "id with\ttab\nnewline\rcr", "title": "]]> ]]>", "start": T0, "stop": T0},
        ]
        self.assertEqual(_elementtree_xmltv(channels, programs), generate_xmltv(channels, programs))

    def test_no_channels(self):
        self.assertEqual(_elementtree_xmltv([], []), generate_xmltv([], []))

    def test_random_guides(self):
        rnd = random.Random(260)
        alphabet = "abcXYZ 09&<>\"'\t\n\r;:/?=#%éüß日本–«»  \U0001F3D2"

        def text():
            return None if rnd.random() < 0.15 else "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))

        for _ in range(50):
            channels = [{"id": text() or "c", "name": text(), "logo_url": text()} for _ in range(rnd.randint(0, 5))]
            programs = [{"channel_id": text() or "c", "title": text(), "sub_title": text(),
                         "description": text(), "category": text(), "icon_url": text(),
                         "start": T0 + timedelta(minutes=rnd.randint(0, 10 ** 5)),
                         "stop": T0 + timedelta(minutes=rnd.randint(0, 10 ** 5))}
                        for _ in range(rnd.randint(0, 20))]
            self.assertEqual(_elementtree_xmltv(channels, programs), generate_xmltv(channels, programs))


class GuideIsStreamed(unittest.TestCase):
    """The route's peak memory does not grow with the guide."""

    def _db(self, channels, per_channel):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        db.add(p)
        db.commit()
        db.execute(mdb.LiveChannel.__table__.insert(), [
            {"provider_id": p.id, "name": f"Channel {i}", "stream_id": str(1000 + i),
             "stream_url": f"http://192.0.2.10/live/{1000 + i}.ts", "epg_channel_id": f"c{i}.example",
             "enabled": True, "sort_order": 0} for i in range(channels)])
        start = datetime.utcnow().replace(microsecond=0)
        db.execute(mdb.EPGProgram.__table__.insert(), [
            {"channel_id": f"c{i}.example", "title": f"Programme {i}-{k}", "description": "x" * 120,
             "category": "Sports", "start": start + timedelta(minutes=30 * k),
             "stop": start + timedelta(minutes=30 * k + 30)}
            for i in range(channels) for k in range(per_channel)])
        db.commit()
        return db

    def _peak(self, db):
        """Peak Python memory while serving the guide, and the number of programmes served."""
        tracemalloc.start()
        try:
            response = livetv_router.hdhr_xmltv(db)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        path = getattr(response, "path", None)
        if path is None:                                       # built whole in memory
            return peak, response.body.count(b"<programme ")
        with open(path, "rb") as f:
            served = f.read().count(b"<programme ")
        os.unlink(path)
        return peak, served

    def test_peak_memory_does_not_grow_with_the_guide(self):
        small, n_small = self._peak(self._db(50, 100))
        big, n_big = self._peak(self._db(50, 1000))
        self.assertEqual((5000, 50000), (n_small, n_big))
        # Ten times the programmes: the whole-guide build took about ten times
        # the memory (about 2 KB per programme); streamed, it stays flat.
        self.assertLess(big, 2 * small + 1024 * 1024, f"peak {small} B for 5k programmes, {big} B for 50k")


class GuideDownloadsGiveTheDatabaseBack(unittest.TestCase):
    """Through the real route with a real connection pool and a get_db that
    closes its session, as the app's does: after complete downloads and after
    clients that leave mid-download, no connection stays checked out and no
    guide file is left behind -- without a garbage collection."""

    def setUp(self):
        tmp = temp_dir(self)
        self.engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False},
                                    pool_size=10, max_overflow=20, pool_timeout=5)
        self.addCleanup(self.engine.dispose)
        mdb.Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        db = Session()
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        db.add(p)
        db.commit()
        db.execute(mdb.LiveChannel.__table__.insert(), [
            {"provider_id": p.id, "name": f"Channel {i}", "stream_id": str(1000 + i),
             "stream_url": f"http://192.0.2.10/live/{1000 + i}.ts", "epg_channel_id": f"c{i}.example",
             "enabled": True, "sort_order": 0} for i in range(30)])
        start = datetime.utcnow().replace(microsecond=0)
        db.execute(mdb.EPGProgram.__table__.insert(), [
            {"channel_id": f"c{i}.example", "title": f"Programme {i}-{k}", "description": "x" * 120,
             "start": start + timedelta(minutes=30 * k), "stop": start + timedelta(minutes=30 * k + 30)}
            for i in range(30) for k in range(100)])
        db.commit()
        db.close()

        def get_db():
            session = Session()
            try:
                yield session
            finally:
                session.close()
        self.app = FastAPI()
        self.app.include_router(livetv_router.router)
        self.app.dependency_overrides[mdb.get_db] = get_db
        self.files_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "tentacle-xmltv-*")))

    def _leftovers(self):
        return set(glob.glob(os.path.join(tempfile.gettempdir(), "tentacle-xmltv-*"))) - self.files_before

    def test_complete_downloads(self):
        client = TestClient(self.app)
        for _ in range(12):
            body = client.get("/hdhr/xmltv.xml").content
            self.assertEqual(3000, body.count(b"<programme "))
            self.assertTrue(body.endswith(b"</tv>"))
        self.assertEqual(0, self.engine.pool.checkedout())
        self.assertEqual(set(), self._leftovers())

    def test_clients_that_leave_mid_download(self):
        async def leave_after_first_chunk():
            sent = []

            async def receive():
                await asyncio.sleep(3600)

            async def send(message):
                if message["type"] == "http.response.body":
                    sent.append(len(message.get("body", b"")))
                    raise OSError("client went away")
            scope = {"type": "http", "method": "GET", "path": "/hdhr/xmltv.xml", "raw_path": b"/hdhr/xmltv.xml",
                     "query_string": b"", "headers": [], "http_version": "1.1", "scheme": "http",
                     "server": ("testserver", 80), "client": ("192.0.2.1", 1234), "root_path": ""}
            with self.assertRaises(OSError):
                await self.app(scope, receive, send)
            return sent

        for _ in range(12):
            self.assertEqual(1, len(asyncio.run(leave_after_first_chunk())))
        self.assertEqual(0, self.engine.pool.checkedout())
        self.assertEqual(set(), self._leftovers())


if __name__ == "__main__":
    unittest.main()
