"""A channel whose tvg-id brings no programmes takes them from the same
channel under the feed's other id.

Run from the tentacle/ directory:  python -m unittest discover -s tests

On a live install 92 enabled channels matched a feed channel that has no
programmes. The provider's feed lists several channels twice: "TNTSport1.uk"
(the tvg-id of 32 TNT Sports channels, no programmes) and "tntsports1.uk"
(the schedule), whose display names mix "UK: TNT SPORTS 1" with
"NOW: TNT SPORT 1". The channels below and their display names are copied
from that feed (ids and names only).
"""
import logging
import os
import shutil
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.xmltv as xmltv
from services.channel_names import channel_country, channel_name_key
from services.epg_match import resolve_guide_ids
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# (feed id, display names, has programmes) -- from the live feed.
FEED = [
    ("TNTSport1.uk", ["UK: TNT SPORTS 1 ᴿᴬᵂ", "UK: TNT SPORTS 1 SD", "UK: TNT SPORTS 1 HEVC HD",
                      "VIP: TNT SPORT 1 SD", "VIP: TNT SPORT 1 ᴴᴰ"], False),
    ("tntsports1.uk", ["UK: TNT SPORTS 1 ᴴᴰ ◉", "NOW: TNT SPORT 1 ᴴᴰ", "NOW: TNT SPORT 1 ᴿᴬᵂ",
                       "UK: TNT SPORT 1 ᴿᴬᵂ"], True),
    ("SkySportsArena.uk", ["UK: SKY SPORTS + SD", "UK: SKY SPORTS + ᴿᴬᵂ", "UK: SKY SPORTS + HEVC HD"], False),
    ("skysportsplus.uk", ["NOW: SKY SPORTS+ ᴴᴰ", "NOW: SKY SPORTS+ ᴿᴬᵂ"], True),
    # Same name, other country: never taken.
    ("SkySportsPremiereLeague.uk", ["UK: SKY SPORTS PREMIER LEAGUE ᴿᴬᵂ", "NZ: SKY SPORTS PREMIER LEAGUE 4K",
                                    "VIP: SKY SPORTS PREMIER LEAGUE ᴴᴰ"], False),
    ("skysportpremierleague.nz", ["NZ: SKY SPORT PREMIER LEAGUE ᴿᴬᵂ"], True),
    ("Eurosport1.uk", ["VIP: EUROSPORT 1 SD", "VIP: EUROSPORT 1 ᴴᴰ"], False),
    ("eurosport1.fr", ["FR: EUROSPORT 1 ᴿᴬᵂ"], True),
    # A tvg-id that has its own programmes keeps them.
    ("AMC.ca", ["CA EN: AMC HD"], True),
    ("amc.ca", ["CA: AMC"], True),
]

# (stream id, our channel name, tvg-id)
CHANNELS = [
    ("1", "VIP: TNT SPORT 1 ᴴᴰ", "TNTSport1.uk"),
    ("2", "UK: TNT SPORTS 1 HEVC HD", "TNTSport1.uk"),
    ("3", "UK: SKY SPORTS + HEVC HD", "SkySportsArena.uk"),
    ("4", "VIP: SKY SPORTS PREMIER LEAGUE ᴴᴰ", "SkySportsPremiereLeague.uk"),
    ("5", "VIP: EUROSPORT 1 ᴴᴰ", "Eurosport1.uk"),
    ("6", "CA EN: AMC HD", "AMC.ca"),
]


class Names(unittest.TestCase):
    def test_tier_tags_are_not_part_of_the_name(self):
        self.assertEqual(channel_name_key("UK: TNT SPORTS 1 SD"), channel_name_key("VIP: TNT SPORT 1 ᴴᴰ"))
        self.assertEqual(channel_name_key("UK: SKY SPORTS + SD"), channel_name_key("NOW: SKY SPORTS+ ᴴᴰ"))

    def test_tier_tags_name_no_country(self):
        self.assertIsNone(channel_country("VIP: TNT SPORT 1 ᴴᴰ"))
        self.assertIsNone(channel_country("NOW: TNT SPORT 1 ᴴᴰ"))

    def test_an_id_suffix_names_a_country(self):
        from services.channel_names import feed_id_country
        self.assertEqual("gb", feed_id_country("TNTSport1.uk"))
        self.assertIsNone(feed_id_country("TS"))


class Resolve(unittest.TestCase):
    def resolve(self):
        chans = [{"id": int(sid), "name": name, "tvg_id": tvg, "override": None} for sid, name, tvg in CHANNELS]
        return resolve_guide_ids(chans, [{"id": fid, "names": names} for fid, names, _ in FEED])

    def test_the_same_channel_under_another_id_is_the_fallback(self):
        out = self.resolve()
        self.assertEqual(("TNTSport1.uk", "tntsports1.uk"), (out[1]["guide_id"], out[1].get("fallback")))
        self.assertEqual(("TNTSport1.uk", "tntsports1.uk"), (out[2]["guide_id"], out[2].get("fallback")))
        self.assertEqual("skysportsplus.uk", out[3].get("fallback"))

    def test_another_countrys_namesake_is_never_the_fallback(self):
        out = self.resolve()
        self.assertIsNone(out[4].get("fallback"), "the .uk channel must not take New Zealand's schedule")
        self.assertIsNone(out[5].get("fallback"), "the .uk channel must not take France's schedule")

    def test_an_override_has_no_fallback(self):
        out = resolve_guide_ids([{"id": 1, "name": "UK: TNT SPORTS 1 SD", "tvg_id": None, "override": "x.uk"}],
                                [{"id": fid, "names": names} for fid, names, _ in FEED])
        self.assertIsNone(out[1].get("fallback"))


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class Sync(unittest.TestCase):
    """The real sync, the feed in the disk cache."""

    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        for sid, name, tvg in CHANNELS:
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, epg_channel_id=tvg,
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        start = datetime.utcnow() + timedelta(hours=1)
        channels = "".join(f'<channel id="{fid}">' + "".join(f"<display-name>{n}</display-name>" for n in names)
                           + "</channel>" for fid, names, _ in FEED)
        progs = "".join(f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                        f'channel="{fid}"><title>{fid} show</title></programme>'
                        for fid, _, has in FEED if has)
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(f"<tv>{channels}{progs}</tv>")

    def sync(self):
        data = {"id": self.pid, "channels": [], "enabled_count": len(CHANNELS), "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            self.assertTrue(livetv_router._run_epg_sync_background(data))
        self.db.expire_all()
        return {c.stream_id: c for c in self.db.query(mdb.LiveChannel)}

    def titles(self, channel):
        return [p.title for p in self.db.query(mdb.EPGProgram).filter(
            mdb.EPGProgram.channel_id == channel.guide_epg_id)]

    def test_channels_whose_tvg_id_is_empty_get_the_other_ids_programmes(self):
        by_sid = self.sync()
        for sid in ("1", "2"):
            self.assertEqual("tntsports1.uk", by_sid[sid].guide_epg_id, by_sid[sid].name)
            self.assertEqual(["tntsports1.uk show"], self.titles(by_sid[sid]))
        self.assertEqual("skysportsplus.uk", by_sid["3"].guide_epg_id)

    def test_no_foreign_schedule_and_no_change_where_the_tvg_id_has_programmes(self):
        by_sid = self.sync()
        self.assertEqual([], self.titles(by_sid["4"]))
        self.assertEqual([], self.titles(by_sid["5"]))
        self.assertEqual(("AMC.ca", "tvg-id"), (by_sid["6"].guide_epg_id, by_sid["6"].epg_match))
        stored = {p.channel_id for p in self.db.query(mdb.EPGProgram)}
        self.assertEqual({"tntsports1.uk", "skysportsplus.uk", "AMC.ca"}, stored,
                         "an unused fallback's programmes are not kept")

    def test_the_coverage_report_counts_them_as_having_a_guide(self):
        self.sync()
        import json
        report = json.loads(mdb.get_setting(self.db, f"livetv_epg_coverage_{self.pid}"))
        self.assertEqual(4, report["enabled"]["with_guide"])
        self.assertEqual(2, report["enabled"]["matched_but_no_programmes"])

    def test_a_second_sync_keeps_them(self):
        self.sync()
        by_sid = self.sync()
        self.assertEqual("tntsports1.uk", by_sid["1"].guide_epg_id)
        self.assertEqual(["tntsports1.uk show"], self.titles(by_sid["1"]))


if __name__ == "__main__":
    unittest.main()
