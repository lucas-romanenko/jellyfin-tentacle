"""A folded network's default variant and its guide follow the household's
country and language: TLC showed a Serbian guide because the provider tags
"CA EN: TLC" with "TLC.rs" while "CA: TLC CANADA" carries "tlc.ca".

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_variant_region
"""
import logging
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import livetv_catalog as C
from services import livetv_lineups as L
from services import livetv_names as N
from tmp_dirs import temp_dir

CHANNELS = [
    # id, name, group, tvg-id, enabled
    (1, "CA EN: TLC", "CA| GENERAL EN", "TLC.rs", True),
    (2, "CA: TLC CANADA", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "tlc.ca", True),
    (3, "US: TLC WEST", "US| ENTERTAINMENT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "tlc.west.us", True),
    (4, "GR: TLC ᵁᴴᴰ ◉", "GR| DOCUMENTARY ⱽᴵᴾ", "TLC.gr", True),
    # Nick Jr.: the only guide is Spanish, no English one exists.
    (10, "TV: NICK JR. ᴿᴬᵂ", "US| TV ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "nickjr.es", True),
    (11, "US: NICK JR HD", "US| KIDS ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None, True),
    # Weather: the variant's own guide id is empty, a switched-off sibling's has programmes.
    (20, "CA EN: WEATHER NETWORK", "CA| GENERAL EN", "EnglishClub.ru", True),
    (21, "CA EN: WEATHER NETWORK HD", "CA| GENERAL EN", "TheWeatherNetwork.ca", False),
    # Discovery: no guide of its own; a Greek sibling's must not become its guide.
    (30, "US: DISCOVERY HD", "US| ENTERTAINMENT", None, True),
    (31, "GR: DISCOVERY", "GR| DOCUMENTARY", "Discovery.gr", False),
]

GUIDES = {"TLC.rs": "Sestre od 500 kilograma", "tlc.ca": "OutDaughtered", "tlc.west.us": "90 Day Fiance",
          "TLC.gr": "Ntokimanter", "nickjr.es": "Patrulla Canina", "TheWeatherNetwork.ca": "Weather Now",
          "EnglishClub.ru": None, "Discovery.gr": "Ellinika"}


class RegionCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        for cid, name, group, gid, on in CHANNELS:
            self.db.add(mdb.LiveChannel(id=cid, provider_id=p.id, name=name, stream_id=str(100 + cid),
                                        stream_url=f"http://192.0.2.10/live/{cid}.ts", group_title=group,
                                        epg_channel_id=gid, enabled=on, sort_order=cid))
        now = datetime.utcnow()
        for gid, title in GUIDES.items():
            if title:
                for h in range(3):
                    self.db.add(mdb.EPGProgram(channel_id=gid, title=title, start=now + timedelta(hours=h),
                                               stop=now + timedelta(hours=h + 1)))
        mdb.set_setting(self.db, "livetv_region", "CA: Toronto, Buffalo, East")
        self.db.commit()
        C._invalidate()
        self.addCleanup(C._invalidate)
        patcher = mock.patch("services.livetv_tuners.schedule_sync")
        patcher.start()
        self.addCleanup(patcher.stop)

    def pool(self):
        C._invalidate()
        return {e["key"]: e for e in L.pool(self.db)}

    def nid_of(self, cid):
        return self.db.get(mdb.LiveChannelCatalog, cid).network_id

    def guide_xml(self):
        from routers.livetv import hdhr_xmltv
        resp = hdhr_xmltv(db=self.db)
        try:
            with open(resp.path, encoding="utf-8") as f:
                return f.read()
        finally:
            os.unlink(resp.path)


class Names(unittest.TestCase):
    def test_feed_country(self):
        self.assertEqual("CA", N.feed_country("CA EN: TLC", "CA| GENERAL EN"))
        self.assertEqual("US", N.feed_country("TV: TLC ᴿᴬᵂ", "US| TV ᴿᴬᵂ"))   # "TV:" is no country
        self.assertEqual("GR", N.feed_country("GR: TLC ᵁᴴᴰ ◉", None))
        self.assertEqual("GB", N.feed_country("UK: SKY NEWS", None))
        self.assertIsNone(N.feed_country("TLC HD", None))
        self.assertIsNone(N.feed_country("NBA: ATLANTA HAWKS", None))

    def test_feed_language(self):
        self.assertEqual("en", N.feed_language("CA EN: TLC", None))
        self.assertEqual("fr", N.feed_language("CA FR: CANAL D", None))
        self.assertEqual("en", N.feed_language("CA: TLC", "CA| GENERAL EN"))
        self.assertIsNone(N.feed_language("US: TLC HD", "US| ENTERTAINMENT"))

    def test_guide_country(self):
        self.assertEqual("RS", N.guide_country("TLC.rs"))
        self.assertEqual("CA", N.guide_country("tlc.ca"))
        self.assertEqual("GB", N.guide_country("CNBC.uk"))
        self.assertEqual("US", N.guide_country("ABC.us@SD"))
        self.assertIsNone(N.guide_country("TS"))
        self.assertIsNone(N.guide_country(None))


class Guide(RegionCase):
    def test_tlc_shows_the_canadian_guide_not_the_serbian_one(self):
        with mock.patch.object(C, "_check_states", return_value={1: "works"}):
            pool = self.pool()
            nid = self.nid_of(1)
            self.assertEqual(nid, self.nid_of(2))
            self.assertEqual(1, C.chosen_variants(self.db)[nid])   # the stream stays the working CA EN one
            self.assertEqual("tlc.ca", C.network_guides(self.db)[nid])
            self.assertTrue(pool[f"n{nid}"]["has_guide"])
            xml = self.guide_xml()
        self.assertIn("<title>OutDaughtered</title>", xml)
        self.assertNotIn("Sestre od 500 kilograma", xml)
        self.assertIn('channel="101"', xml)          # under the variant's own number

    def test_the_variants_own_guide_stays_when_it_is_the_households(self):
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 1).update({"epg_channel_id": "tlc.east.ca"})
        self.db.add(mdb.EPGProgram(channel_id="tlc.east.ca", title="Own", start=datetime.utcnow(),
                                   stop=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        with mock.patch.object(C, "_check_states", return_value={1: "works"}):
            self.pool()
            self.assertEqual("tlc.east.ca", C.network_guides(self.db)[self.nid_of(1)])

    def test_a_guide_the_admin_set_is_kept(self):
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 1).update({"epg_id_override": "TLC.rs"})
        self.db.commit()
        with mock.patch.object(C, "_check_states", return_value={1: "works"}):
            self.pool()
            self.assertEqual("TLC.rs", C.network_guides(self.db)[self.nid_of(1)])

    def test_no_english_guide_keeps_the_variants_own(self):
        with mock.patch.object(C, "_check_states", return_value={10: "works"}):
            self.pool()
            nid = self.nid_of(10)
            self.assertEqual(10, C.chosen_variants(self.db)[nid])
            self.assertEqual("nickjr.es", C.network_guides(self.db)[nid])

    def test_a_foreign_guide_does_not_make_a_variant_preferred(self):
        # Neither works yet: the HD one wins on quality, the Spanish guide
        # counts for nothing, and the network borrows no foreign guide.
        self.pool()
        nid = self.nid_of(10)
        self.assertEqual(11, C.chosen_variants(self.db)[nid])
        self.assertIsNone(C.network_guides(self.db)[nid])

    def test_a_switched_off_siblings_guide_counts(self):
        self.pool()
        self.assertEqual("TheWeatherNetwork.ca", C.network_guides(self.db)[self.nid_of(20)])
        self.assertIn("<title>Weather Now</title>", self.guide_xml())

    def test_no_foreign_guide_for_a_network_without_one(self):
        self.pool()
        self.assertIsNone(C.network_guides(self.db)[self.nid_of(30)])
        self.assertNotIn("Ellinika", self.guide_xml())


class Variant(RegionCase):
    def test_a_foreign_variant_is_not_the_default_even_when_it_works(self):
        with mock.patch.object(C, "_check_states", return_value={4: "works"}):
            C.refresh(self.db)
            self.assertNotEqual(4, C.chosen_variants(self.db)[self.nid_of(1)])

    def test_a_broken_home_variant_gives_way(self):
        states = {1: "broken", 2: "broken", 3: "broken", 4: "works"}
        with mock.patch.object(C, "_check_states", return_value=states):
            C.refresh(self.db)
            self.assertEqual(4, C.chosen_variants(self.db)[self.nid_of(1)])

    def test_people_who_watched_a_foreign_variant_get_the_home_one(self):
        self.db.add(mdb.LiveUserChannel(jellyfin_user_id="a" * 32, channel_key="c4", position=0))
        self.db.commit()
        C.refresh(self.db)
        self.assertNotEqual(4, C.chosen_variants(self.db)[self.nid_of(1)])

    def test_french_is_foreign_to_an_english_household(self):
        self.assertEqual(0, C._variant_rank("CA FR: TLC", None, ("CA", "en")))
        self.assertEqual(3, C._variant_rank("CA FR: TLC", None, ("CA", "fr")))
        self.assertEqual(3, C._variant_rank("US: TLC HD", None, ("CA", "en")))   # the neighbour
        self.assertEqual(1, C._variant_rank("UK: TLC", None, ("CA", "en")))
        self.assertEqual(2, C._variant_rank("TLC HD", None, ("CA", "en")))

    def test_the_admins_choice_stays(self):
        self.pool()
        nid = self.nid_of(1)
        C.set_variant(self.db, nid, 4)
        self.assertEqual(4, C.chosen_variants(self.db)[nid])

    def test_the_new_rules_choose_again_once(self):
        self.pool()
        nid = self.nid_of(1)
        self.db.get(mdb.LiveNetwork, nid).auto_channel_id = 4   # what an older build chose
        mdb.set_setting(self.db, C._FP_KEY, "older")
        self.db.commit()
        self.assertTrue(C.ensure_fresh(self.db))
        self.assertNotEqual(4, self.db.get(mdb.LiveNetwork, nid).auto_channel_id)


if __name__ == "__main__":
    unittest.main()
