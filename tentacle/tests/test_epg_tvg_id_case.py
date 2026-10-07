"""A tvg-id that differs from the guide feed's channel id only in letter case
still finds that channel's guide.

Run from the tentacle/ directory:  python -m unittest tests.test_epg_tvg_id_case

The EPG sync compared the provider's tvg-id with the feed's <channel id> as an
exact string (services/epg_match.py `tvg in feed_ids`), and then kept only the
programmes whose channel attribute is one of the resolved ids, again exactly
(services/xmltv.py `ch_id in channel_ids`). A playlist that says
tvg-id="cnn.us" for a feed that lists <channel id="CNN.us"> got no guide, and
the sync reported it as "a tvg-id the feed lacks". Jellyfin, given the same
playlist and feed directly, matches the ids ignoring case (its EpgChannelData
dictionary is StringComparer.OrdinalIgnoreCase), so the channel had a guide
before Tentacle sat in between.

The name pass (#141) rescues only a channel whose name matches the feed's
display name exactly once; the usual HD / FHD copies of one channel share
their tvg-id AND their name, so the name pass calls them ambiguous and
neither gets a guide.
"""
import logging
import os
import random
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
from services.epg_match import resolve_guide_ids
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _ch(cid, name, tvg=None, enabled=True):
    return {"id": cid, "name": name, "tvg_id": tvg, "override": None, "enabled": enabled}


FEED = [{"id": "CNN.us", "names": ["CNN"]}, {"id": "AMC.ca", "names": ["AMC"]}]


class ResolveIgnoresCase(unittest.TestCase):
    def test_exact_case_control(self):
        """Control: the same channel with the feed's exact casing has a guide."""
        r = resolve_guide_ids([_ch(1, "News 24/7", tvg="CNN.us")], FEED)
        self.assertEqual(("tvg-id", "CNN.us"), (r[1]["method"], r[1]["guide_id"]))

    def test_a_tvg_id_in_other_case_is_in_the_feed(self):
        r = resolve_guide_ids([_ch(1, "News 24/7", tvg="cnn.us")], FEED)
        self.assertNotEqual("tvg-id-not-in-feed", r[1]["reason"],
                            "tvg-id 'cnn.us' reported missing although the feed lists 'CNN.us'")
        self.assertIsNotNone(r[1]["guide_id"])
        self.assertEqual("cnn.us", r[1]["guide_id"].lower())

    def test_hd_and_fhd_copies_with_a_lower_case_tvg_id_both_get_the_guide(self):
        r = resolve_guide_ids([_ch(1, "US: CNN HD", tvg="cnn.us"), _ch(2, "US: CNN FHD", tvg="cnn.us")], FEED)
        for cid in (1, 2):
            self.assertIsNotNone(r[cid]["guide_id"], f"channel {cid}: {r[cid]['reason']}")
            self.assertEqual("cnn.us", r[cid]["guide_id"].lower())


    def test_an_exact_match_wins_over_another_case(self):
        feed = FEED + [{"id": "cnn.us", "names": ["CNN Lower"]}]
        r = resolve_guide_ids([_ch(1, "News", tvg="cnn.us"), _ch(2, "News 2", tvg="CNN.us")], feed)
        self.assertEqual(("tvg-id", "cnn.us", None), (r[1]["method"], r[1]["guide_id"], r[1]["name_match"]))
        self.assertEqual(("tvg-id", "CNN.us", None), (r[2]["method"], r[2]["guide_id"], r[2]["name_match"]))

    def test_two_feed_ids_in_other_cases_are_not_guessed(self):
        """'CNN.us' and 'Cnn.Us' both in the feed: which one 'cnn.us' means is
        unknown, so the channel goes on to the name pass as before."""
        feed = FEED + [{"id": "Cnn.Us", "names": ["CNN Two"]}]
        r = resolve_guide_ids([_ch(1, "News 24/7", tvg="cnn.us")], feed)
        self.assertEqual((None, None, "tvg-id-not-in-feed"), (r[1]["method"], r[1]["guide_id"], r[1]["reason"]))

    def test_the_feed_spelling_is_stored_like_a_name_match(self):
        r = resolve_guide_ids([_ch(1, "News 24/7", tvg="cnn.us")], FEED)
        self.assertEqual(("tvg-id", "CNN.us", "CNN.us", None),
                         (r[1]["method"], r[1]["guide_id"], r[1]["name_match"], r[1]["reason"]))

    def test_a_feed_without_channel_elements_is_unchanged(self):
        r = resolve_guide_ids([_ch(1, "News", tvg="cnn.us")], [])
        self.assertEqual(("tvg-id", "cnn.us", None), (r[1]["method"], r[1]["guide_id"], r[1]["name_match"]))

    def test_random_ids_and_cases(self):
        """A tvg-id gets the feed id it names: itself when the feed has it,
        else the only feed id equal to it ignoring case; never another one."""
        seeds = int(os.environ.get("EPG_CASE_SEEDS", "1000"))
        for seed in range(seeds):
            rnd = random.Random(seed)
            base = [rnd.choice(["cnn", "amc", "tsn", "bbc", "sky"]) + "." + rnd.choice(["us", "ca", "uk"])
                    for _ in range(rnd.randint(1, 6))]
            flip = lambda s: "".join(c.upper() if rnd.random() < 0.5 else c.lower() for c in s)
            feed = [{"id": flip(b), "names": [f"Feed {i}"]} for i, b in enumerate(base)]
            feed_ids = {f["id"] for f in feed}
            chans = [_ch(i, f"Chan {i}", tvg=flip(rnd.choice(base + ["fox.us"]))) for i in range(rnd.randint(1, 6))]
            r = resolve_guide_ids(chans, feed)
            for ch in chans:
                tvg, got = ch["tvg_id"], r[ch["id"]]
                same_case = {f for f in feed_ids if f.lower() == tvg.lower()}
                if tvg in feed_ids:
                    want = (tvg, None)
                elif len(same_case) == 1:
                    want = (next(iter(same_case)), next(iter(same_case)))
                else:
                    want = (None, None)
                self.assertEqual(want, (got["guide_id"], got["name_match"]), f"seed {seed}: {tvg} in {sorted(feed_ids)}")
                if got["guide_id"]:
                    self.assertEqual("tvg-id", got["method"], f"seed {seed}")


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class EpgSyncIgnoresTvgIdCase(unittest.TestCase):
    """The real sync and the real served guide, with the feed in the disk cache."""

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
        for sid, name, tvg in (("1", "US: CNN HD", "cnn.us"), ("2", "US: CNN FHD", "cnn.us"),
                               ("3", "CA: AMC", "AMC.ca")):
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, epg_channel_id=tvg,
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        start = datetime.utcnow() + timedelta(hours=1)
        progs = "".join(
            f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" channel="{cid}">'
            f'<title>{title}</title></programme>'
            for cid, title in (("CNN.us", "Newsroom"), ("AMC.ca", "A film")))
        feed = ('<tv><channel id="CNN.us"><display-name>CNN</display-name></channel>'
                '<channel id="AMC.ca"><display-name>AMC</display-name></channel>' + progs + '</tv>')
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(feed)

    def _sync(self):
        data = {"id": self.pid, "channels": [], "enabled_count": 3, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            return livetv_router._run_epg_sync_background(data)

    def _client(self):
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        return TestClient(app)

    def test_lower_case_tvg_id_gets_the_feed_channels_programmes(self):
        self.assertTrue(self._sync())
        client = self._client()
        xml = client.get("/hdhr/xmltv.xml").text
        self.assertIn("<title>A film</title>", xml)          # control: exact-case tvg-id
        self.assertIn("<title>Newsroom</title>", xml,
                      "CNN (tvg-id 'cnn.us') has no guide although the feed carries 'CNN.us'")
        rows = {c["stream_id"]: c for c in client.get("/api/live/channels").json()["channels"]}
        self.assertTrue(rows["1"]["has_epg_data"])
        self.assertTrue(rows["2"]["has_epg_data"])
        status = livetv_router._get_sync_status(self.pid)
        self.assertIn("guide for 3 of 3 enabled channels", status["message"])

    def test_the_channel_list_says_tvg_id(self):
        self.assertTrue(self._sync())
        rows = {c["stream_id"]: c for c in self._client().get("/api/live/channels").json()["channels"]}
        self.assertEqual(("CNN.us", "tvg-id"), (rows["1"]["guide_epg_id"], rows["1"]["epg_match"]))
        self.assertEqual(("AMC.ca", "tvg-id"), (rows["3"]["guide_epg_id"], rows["3"]["epg_match"]))

    def test_a_second_sync_keeps_the_guide(self):
        """The rows the first sync stored are replaced, not duplicated (#467)."""
        self.assertTrue(self._sync())
        first = self.db.query(mdb.EPGProgram).count()
        self.assertTrue(self._sync())
        self.db.expire_all()
        self.assertEqual(first, self.db.query(mdb.EPGProgram).count())
        self.assertIn("<title>Newsroom</title>", self._client().get("/hdhr/xmltv.xml").text)


if __name__ == "__main__":
    unittest.main()
