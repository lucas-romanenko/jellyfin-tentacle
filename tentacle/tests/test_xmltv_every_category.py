"""A programme listed under several <category> elements keeps all of them in
the guide Tentacle serves to Jellyfin.

Run from the tentacle/ directory:  python -m unittest tests.test_xmltv_every_category

The XMLTV DTD allows any number of <category> elements per programme, and
feeds use it: "Hockey" + "Sports", "Talk" + "News". Jellyfin's XMLTV reader
sets IsSports / IsNews / IsKids / IsMovie when ANY category equals
(case-insensitively) an entry of the listing provider's lists -- defaults:
sports, basketball, baseball, football / news, journalism, documentary,
current affairs / kids, family, children, childrens, disney / movie -- and
keeps them all as the programme's genres. Only the first <category> was read
and served, so a game its provider lists as "Hockey" + "Sports" reached
Jellyfin as "Hockey" alone and was never flagged as sports (no sports badge,
not in the Sports filter, no sports recording defaults). The category
inference (#19) does not step in, because the programme has a category.
"""
import logging
import os
import random
import shutil
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.xmltv as xmltv
from services.xmltv import generate_xmltv, parse_xmltv
from tmp_dirs import temp_dir

# Jellyfin 10.11 ListingsProviderInfo defaults.
JELLYFIN_SPORTS = ("sports", "basketball", "baseball", "football")
JELLYFIN_NEWS = ("news", "journalism", "documentary", "current affairs")


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


def _jellyfin_flag(categories, names):
    """XmlTvListingsProvider: a flag is set when any non-blank category is in its list (OrdinalIgnoreCase)."""
    return any(c.strip() and c.lower() in names for c in categories)


def _feed(*category_lists):
    """A feed with one programme per list, each with those <category> elements (None = <category/>)."""
    progs = []
    for i, cats in enumerate(category_lists):
        start = datetime(2026, 11, 1, 5) + timedelta(hours=i)
        cat_xml = "".join("<category />" if c is None else f'<category lang="en">{c}</category>' for c in cats)
        progs.append(f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" channel="a">'
                     f'<title>P{i}</title>{cat_xml}</programme>')
    return "<tv>" + "".join(progs) + "</tv>"


def _served_categories(xml):
    root = ET.fromstring(xml)
    return [[c.text or "" for c in p.findall("category")] for p in root.findall("programme")]


def _round_trip(feed):
    """The categories of each programme in the guide served from this feed."""
    _c, programs = parse_xmltv(feed)
    return _served_categories(generate_xmltv([{"id": "a", "name": "A"}], programs))


class EveryCategoryIsRead(unittest.TestCase):
    def test_every_category_in_feed_order(self):
        self.assertEqual([["Hockey", "Sports"], ["Talk", "News"]],
                         _round_trip(_feed(["Hockey", "Sports"], ["Talk", "News"])))

    def test_one_category_is_stored_as_before(self):
        _c, programs = parse_xmltv(_feed(["News"]))
        self.assertEqual("News", programs[0]["category"])

    def test_empty_and_repeated_categories_are_dropped(self):
        feed = _feed([None, "Sports", "Sports", "Hockey"], [None], [])
        self.assertEqual([["Sports", "Hockey"], [], []], _round_trip(feed))
        # No category at all is still None, so the #19 inference applies.
        _c, programs = parse_xmltv(feed)
        self.assertEqual([None, None], [p["category"] for p in programs[1:]])

    def test_the_guide_writes_one_category_each_in_dtd_order(self):
        _c, programs = parse_xmltv(
            '<tv><programme start="20261101050000 +0000" stop="20261101080000 +0000" channel="a">'
            '<title>Maple Leafs at Canadiens</title><desc>d</desc>'
            '<category lang="en">Hockey</category><category lang="en">Sports</category>'
            '<icon src="http://img.example/g.png" /></programme></tv>')
        xml = generate_xmltv([{"id": "a", "name": "A"}], programs)
        prog = xml[xml.index("<programme"):]
        self.assertIn("<desc>d</desc><category>Hockey</category><category>Sports</category><icon ", prog)

    def test_random_feeds_round_trip(self):
        """Whatever the feed lists, the served guide carries the same non-empty
        categories, in feed order, each once."""
        rnd = random.Random(19)
        words = ["Sports", "sports", "Hockey", "News", "Talk", "Kids", "Movie", "A & B", "<x>", "Ā日本",
                 " ", "a\tb", "line\nbreak", "\"q\"", "'s", "Sport > all"]
        for _ in range(300):
            lists = [[rnd.choice(words + [None]) for _ in range(rnd.randint(0, 5))] for _ in range(rnd.randint(1, 4))]
            root = ET.Element("tv")
            for i, cats in enumerate(lists):
                start = datetime(2026, 11, 1, 5) + timedelta(hours=i)
                el = ET.SubElement(root, "programme", start=_stamp(start),
                                   stop=_stamp(start + timedelta(hours=1)), channel="a")
                ET.SubElement(el, "title").text = f"P{i}"
                for c in cats:
                    ET.SubElement(el, "category").text = c
            served = _round_trip(ET.tostring(root, encoding="unicode"))
            expected = [list(dict.fromkeys(c for c in cats if c)) for cats in lists]
            self.assertEqual(expected, served, lists)


class EveryCategoryReachesJellyfin(unittest.TestCase):
    """The real EPG sync (feed in the disk cache) and the real /hdhr/xmltv.xml."""

    def setUp(self):
        self.tmp = temp_dir(self)
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
        self.db.add(mdb.LiveChannel(provider_id=provider.id, name="Sportsnet", stream_id="100",
                                    stream_url="http://192.0.2.10/live/100.ts",
                                    epg_channel_id="sn.ca", enabled=True))
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "xmltv_cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        self.now = datetime.utcnow().replace(microsecond=0)
        feed = ('<tv><channel id="sn.ca"><display-name>Sportsnet</display-name></channel>'
                f'<programme start="{_stamp(self.now + timedelta(hours=1))}" stop="{_stamp(self.now + timedelta(hours=4))}" channel="sn.ca">'
                '<title>Maple Leafs at Canadiens</title>'
                '<category lang="en">Hockey</category><category lang="en">Sports</category>'
                '</programme>'
                f'<programme start="{_stamp(self.now + timedelta(hours=4))}" stop="{_stamp(self.now + timedelta(hours=5))}" channel="sn.ca">'
                '<title>Evening Report</title>'
                '<category lang="en">Talk</category><category lang="en">News</category>'
                '</programme>'
                f'<programme start="{_stamp(self.now + timedelta(hours=5))}" stop="{_stamp(self.now + timedelta(hours=6))}" channel="sn.ca">'
                '<title>NHL Tonight</title>'
                '</programme>'
                '</tv>')
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(feed)

    def _sync(self):
        data = {"id": self.pid, "channels": [{"epg_channel_id": "sn.ca"}], "enabled_count": 1,
                "epg_url": self.url, "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            self.assertTrue(livetv_router._run_epg_sync_background(data))

    def _served_categories(self):
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.db.expire_all()
        root = ET.fromstring(TestClient(app).get("/hdhr/xmltv.xml").content)
        return {p.findtext("title"): [c.text or "" for c in p.findall("category")]
                for p in root.findall("programme")}

    def test_a_hockey_and_sports_programme_is_served_with_sports(self):
        self._sync()
        game = self._served_categories()["Maple Leafs at Canadiens"]
        self.assertIn("Sports", game, f"served categories {game}; the feed listed Hockey + Sports")
        self.assertTrue(_jellyfin_flag(game, JELLYFIN_SPORTS),
                        f"served categories {game}: Jellyfin (default lists) would not flag the game as sports")

    def test_every_category_of_the_feed_is_served_in_order(self):
        self._sync()
        cats = self._served_categories()
        self.assertEqual(["Hockey", "Sports"], cats["Maple Leafs at Canadiens"])
        self.assertEqual(["Talk", "News"], cats["Evening Report"])
        self.assertTrue(_jellyfin_flag(cats["Evening Report"], JELLYFIN_NEWS))

    def test_no_category_is_still_inferred(self):
        """#19 is unchanged: a programme the provider sent no category for gets one from its title."""
        self._sync()
        self.assertEqual(["Sports"], self._served_categories()["NHL Tonight"])

    def test_a_row_stored_before_the_upgrade_is_served_as_before(self):
        """Rows from an older build hold one category; they are served unchanged until the next EPG sync."""
        start = self.now + timedelta(hours=8)
        self.db.add(mdb.EPGProgram(channel_id="sn.ca", title="Old row", start=start,
                                   stop=start + timedelta(hours=1), category="Hockey"))
        self.db.commit()
        self.assertEqual(["Hockey"], self._served_categories()["Old row"])


if __name__ == "__main__":
    unittest.main()
