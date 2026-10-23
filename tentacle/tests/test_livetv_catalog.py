"""The channel catalog: networks in the pool, aliases when keys change, types,
has guide, event programmes in the guide, the network stream.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_catalog
"""
import asyncio
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
from tmp_dirs import temp_dir

CHANNELS = [
    # id, name, group, guide id
    (1, "US: NBC (KNBC) LOS ANGELES", "US| NBC", None),
    (2, "NBC WNBC NEW YORK", "US| NBC", None),
    (3, "US: NBC BUFFALO HD", "US| NBC", "WGRZ.us"),
    (4, "US: NBC SPORTS BOSTON", "US| NBC", None),
    (5, "US: NBC NEWS NOW", "US| NBC", None),
    (6, "CA EN: TSN 1", "CA| SPORTS EN", "TSN1.ca"),
    (7, "CA: TSN 1 ⁽ᴮᴷ⁾ ᴿᴬᵂ", "CA| SPORTS EN", None),
    (8, "CA: TSN 2", "CA| SPORTS EN", None),
    (9, "NHL | 02 - 7:30pm Panthers at Senators", "US| NHL PPV", None),
    (10, "NHL | 08 -", "US| NHL PPV", None),
    (11, "CA EN: CARTOON NETWORK HD", "CA| KIDS EN", None),
    (12, "CA EN: CNN HD", "CA| NEWS EN", None),
    (13, "US: HGTV HD", "US| ENTERTAINMENT", None),
    (14, "TV: HGTV WEST", "US| ENTERTAINMENT", None),
    (15, "CA EN: TELETOON AT NIGHT", "CA| KIDS EN", None),
    (16, "CA EN: ADULT SWIM HD", "CA| KIDS EN", None),
    (17, "CA EN: SPORTSNET ONTARIO HD", "CA| SPORTS EN", None),
]


class CatalogCase(unittest.TestCase):
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
        for cid, name, group, gid in CHANNELS:
            self.db.add(mdb.LiveChannel(id=cid, provider_id=p.id, name=name, stream_id=str(100 + cid),
                                        stream_url=f"http://192.0.2.10/live/{cid}.ts", group_title=group,
                                        epg_channel_id=gid, enabled=True, sort_order=cid))
        self.db.commit()
        mdb.set_setting(self.db, "livetv_region", "CA: Toronto, Buffalo, East")
        self.db.commit()
        C._invalidate()
        self.addCleanup(C._invalidate)
        # No tuner sync from these tests.
        patcher = mock.patch("services.livetv_tuners.schedule_sync")
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)

    def pool(self):
        C._invalidate()
        return {e["key"]: e for e in L.pool(self.db)}

    def nid_of(self, cid):
        return self.db.get(mdb.LiveChannelCatalog, cid).network_id


class Folding(CatalogCase):
    def test_regional_and_quality_duplicates_are_one_entry(self):
        pool = self.pool()
        nbc = self.nid_of(1)
        self.assertEqual(nbc, self.nid_of(2))
        self.assertEqual(nbc, self.nid_of(3))
        self.assertIsNone(self.nid_of(4))      # NBC Sports Boston is its own channel
        self.assertIsNone(self.nid_of(5))      # NBC News Now too
        self.assertIsNone(self.nid_of(9))      # event slots never fold
        e = pool[f"n{nbc}"]
        self.assertEqual("NBC", e["name"])
        self.assertEqual(f"/api/live/net/{nbc}", e["stream_path"])
        self.assertEqual(3, e["variants"])
        # The admin's region: Buffalo.
        self.assertEqual("NBC Buffalo", e["variant_name"])
        self.assertEqual(3, e["channel_id"])
        self.assertEqual("103", e["xmltv_id"])
        for k in ("c1", "c2", "c3", "c6", "c7"):
            self.assertNotIn(k, pool)
        self.assertIn("c8", pool)
        self.assertEqual(17 - 2 - 1 - 1, len(pool), sorted(pool))   # NBC x3, TSN 1 x2, HGTV x2 folded

    def test_entries_are_where_their_first_channel_was(self):
        keys = [e["key"] for e in L.pool(self.db)]
        self.assertTrue(keys[0].startswith("n"))
        self.assertEqual("c4", keys[1])

    def test_switching_a_variant_off_keeps_the_key(self):
        before = set(self.pool())
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 3).update({"enabled": False})
        self.db.commit()
        pool = self.pool()
        self.assertEqual(before, set(pool))
        nbc = f"n{self.nid_of(1)}"
        self.assertEqual(2, pool[nbc]["variants"])
        self.assertNotEqual("NBC Buffalo", pool[nbc]["variant_name"])

    def test_people_who_watched_a_variant_keep_it(self):
        # Someone had New York in their list before folding: it is the default
        # when no variant is the household's place (Toronto: Buffalo is).
        self.db.add(mdb.LiveUserChannel(jellyfin_user_id="a" * 32, channel_key="c2", position=0))
        self.db.commit()
        pool = self.pool()
        self.assertEqual("NBC Buffalo", pool[f"n{self.nid_of(1)}"]["variant_name"])
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 3).update({"enabled": False})
        self.db.commit()
        pool = self.pool()
        self.assertEqual("NBC WNBC New York", pool[f"n{self.nid_of(1)}"]["variant_name"])

    def test_a_working_variant_is_preferred(self):
        with mock.patch.object(C, "_check_states", return_value={3: "broken", 1: "works"}):
            C.refresh(self.db)
        self.assertEqual(1, C.chosen_variants(self.db)[self.nid_of(1)])


class Aliases(CatalogCase):
    def test_folding_leaves_aliases_from_the_old_channel_keys(self):
        self.pool()
        nbc = f"n{self.nid_of(1)}"
        a = C.aliases(self.db)
        self.assertEqual(nbc, a["c1"])
        self.assertEqual(nbc, a["c2"])
        self.assertEqual(nbc, a["c3"])
        self.assertNotIn("c4", a)
        # A person's old list resolves to the network, once.
        self.assertEqual([nbc, "c4"], L.resolve_keys(self.db, ["c1", "c3", "c4"]))

    def test_moved_keys_start_a_tuner_sync_at_once(self):
        self.pool()
        self.assertTrue(self.sync.called)
        self.assertTrue(self.sync.call_args.kwargs.get("refresh"))
        self.sync.reset_mock()
        self.pool()                      # nothing changed: no sync
        self.assertFalse(self.sync.called)

    def test_aliases_are_cached_until_keys_change(self):
        self.pool()
        first = C.aliases(self.db)
        with mock.patch.object(C, "ensure_fresh", side_effect=AssertionError("refolded")):
            self.assertEqual(first, C.aliases(self.db))
        C.merge(self.db, [f"n{self.nid_of(1)}", "c5"])
        self.pool()
        self.assertIn("c5", C.aliases(self.db))

    def test_splitting_one_channel_out(self):
        self.pool()
        nid = self.nid_of(1)
        C.split(self.db, nid, [2])
        pool = self.pool()
        self.assertIn("c2", pool)
        self.assertIn(f"n{nid}", pool)      # the rest keep the network's key
        a = C.aliases(self.db)
        self.assertNotIn("c2", a)           # a pool key again
        self.assertEqual(f"n{nid}", a["c1"])
        # Folding again leaves the admin's split alone.
        C.refresh(self.db)
        self.assertIsNone(self.nid_of(2))

    def test_dissolving_a_network(self):
        self.pool()
        nid = self.nid_of(1)
        variant = C.chosen_variants(self.db)[nid]
        C.split(self.db, nid)
        pool = self.pool()
        self.assertNotIn(f"n{nid}", pool)
        self.assertTrue({"c1", "c2", "c3"} <= set(pool))
        a = C.aliases(self.db)
        self.assertEqual(f"c{variant}", a[f"n{nid}"])
        C.refresh(self.db)
        self.assertIsNone(self.nid_of(1))   # never folded again

    def test_merging_a_missed_one(self):
        self.pool()
        nid = self.nid_of(1)
        res = C.merge(self.db, [f"n{nid}", "c5"])
        self.assertEqual(nid, res["network"])
        pool = self.pool()
        self.assertNotIn("c5", pool)
        self.assertEqual(4, pool[f"n{nid}"]["variants"])
        self.assertEqual(f"n{nid}", C.aliases(self.db)["c5"])

    def test_merging_two_networks(self):
        self.pool()
        a, b = self.nid_of(1), self.nid_of(13)
        C.merge(self.db, [f"n{a}", f"n{b}"], name="Both")
        pool = self.pool()
        self.assertNotIn(f"n{b}", pool)
        self.assertEqual("Both", pool[f"n{a}"]["name"])
        self.assertEqual(f"n{a}", C.aliases(self.db)[f"n{b}"])
        self.assertEqual(f"n{a}", C.aliases(self.db)["c13"])   # chains end at a pool key
        self.assertEqual([f"n{a}"], L.resolve_keys(self.db, ["c13"]))

    def test_choosing_the_variant_keeps_the_stream_address(self):
        pool = self.pool()
        nid = self.nid_of(1)
        before = pool[f"n{nid}"]
        C.set_variant(self.db, nid, 2)
        after = self.pool()[f"n{nid}"]
        self.assertEqual(before["stream_path"], after["stream_path"])
        self.assertEqual("102", after["xmltv_id"])
        self.assertEqual("NBC WNBC New York", after["variant_name"])
        with self.assertRaises(C.CatalogError):
            C.set_variant(self.db, nid, 6)   # not one of its channels

    def test_taking_a_network_out_and_back(self):
        self.pool()
        nid = self.nid_of(1)
        C.set_excluded(self.db, nid, True)
        self.assertNotIn(f"n{nid}", self.pool())
        C.set_excluded(self.db, nid, False)
        self.assertIn(f"n{nid}", self.pool())


class Facts(CatalogCase):
    def test_types(self):
        pool = self.pool()
        self.assertIn("Sports", pool["c8"]["types"])
        self.assertIn("News", pool["c12"]["types"])
        self.assertIn("Kids", pool["c11"]["types"])
        self.assertIn("Local", pool[f"n{self.nid_of(1)}"]["types"])
        self.assertIn("Lifestyle", pool[f"n{self.nid_of(13)}"]["types"])
        self.assertEqual(["Sports", "Event/PPV"], pool["c9"]["types"])

    def test_adult_animation_is_not_kids(self):
        pool = self.pool()
        self.assertIn("Kids", pool["c11"]["types"])
        self.assertNotIn("Kids", pool["c15"]["types"])
        self.assertNotIn("Kids", pool["c16"]["types"])

    def test_people_see_clean_names_the_admin_the_providers(self):
        pool = self.pool()
        self.assertEqual("Sportsnet Ontario", pool["c17"]["name"])
        self.assertEqual("CA EN: SPORTSNET ONTARIO HD", pool["c17"]["provider_name"])
        self.assertEqual("Panthers at Senators", pool["c9"]["name"])     # an event channel: the event
        self.assertEqual("NBC Buffalo", pool[f"n{self.nid_of(1)}"]["variant_name"])
        # The admin's own name wins.
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 17).update({"custom_name": "SN Ontario"})
        self.db.commit()
        self.assertEqual("SN Ontario", self.pool()["c17"]["name"])

    def test_main_is_a_short_list_per_type(self):
        pool = self.pool()
        nbc = pool[f"n{self.nid_of(1)}"]
        self.assertTrue(nbc["main"])
        self.assertIn("Local", nbc["main_types"])
        self.assertFalse(pool["c9"]["main"])          # event slots: never a quick pick
        self.assertFalse(pool["c8"]["main"])          # one copy, no guide: only in Add channels
        self.assertTrue(pool[f"n{self.nid_of(6)}"]["main"])   # TSN 1: a network
        with mock.patch.object(C, "MAIN_PER_TYPE", 1):
            pool = self.pool()
        sports = [e for e in pool.values() if "Sports" in e["main_types"]]
        self.assertEqual(1, len(sports))

    def test_the_admins_types_win(self):
        self.pool()
        C.set_types(self.db, "c12", ["Entertainment"])
        pool = self.pool()
        self.assertEqual(["Entertainment"], pool["c12"]["types"])
        nid = self.nid_of(1)
        C.set_types(self.db, f"n{nid}", ["News", "Local"])
        self.assertEqual(["News", "Local"], self.pool()[f"n{nid}"]["types"])
        C.set_types(self.db, "c12", [])
        self.assertIn("News", self.pool()["c12"]["types"])
        with self.assertRaises(C.CatalogError):
            C.set_types(self.db, "c12", ["Cooking"])

    def test_types_from_the_guide(self):
        now = datetime.utcnow()
        for h in range(4):
            self.db.add(mdb.EPGProgram(channel_id="WGRZ.us", title=f"Show {h}", category="News",
                                       start=now + timedelta(hours=h), stop=now + timedelta(hours=h + 1)))
        self.db.commit()
        self.assertIn("News", self.pool()[f"n{self.nid_of(1)}"]["types"])

    def test_has_guide_is_the_next_24_hours(self):
        now = datetime.utcnow()
        self.db.add(mdb.EPGProgram(channel_id="TSN1.ca", title="SportsCentre",
                                   start=now + timedelta(hours=2), stop=now + timedelta(hours=3)))
        self.db.add(mdb.EPGProgram(channel_id="WGRZ.us", title="Old news",
                                   start=now - timedelta(hours=5), stop=now - timedelta(hours=4)))
        self.db.commit()
        pool = self.pool()
        self.assertTrue(pool[f"n{self.nid_of(6)}"]["has_guide"])     # TSN 1's variant has a guide
        self.assertFalse(pool[f"n{self.nid_of(1)}"]["has_guide"])    # only a past programme
        self.assertFalse(pool["c8"]["has_guide"])

    def test_events(self):
        pool = self.pool()
        self.assertEqual("no_event", pool["c10"]["event"])
        ev = pool["c9"]["event"]
        self.assertEqual("Panthers at Senators", ev["title"])
        self.assertTrue(ev["start"].endswith("Z"))
        self.assertIsNone(pool["c8"]["event"])

    def test_event_programmes_go_in_the_guide(self):
        from routers.livetv import hdhr_xmltv
        resp = hdhr_xmltv(db=self.db)
        try:
            with open(resp.path, encoding="utf-8") as f:
                xml = f.read()
        finally:
            os.unlink(resp.path)
        self.assertIn("<title>Panthers at Senators</title>", xml)
        self.assertIn('channel="109"', xml)
        self.assertNotIn('channel="110"', xml.split("</channel>")[-1])   # "no event": no programme


class NetworkStream(CatalogCase):
    def test_the_network_streams_its_variant(self):
        from routers import livetv_catalog as R
        self.pool()
        nid = self.nid_of(1)
        C.set_variant(self.db, nid, 2)
        seen = []

        async def fake_proxy(cid, db):
            seen.append(cid)
            return "streaming"
        with mock.patch("routers.livetv.stream_proxy", fake_proxy):
            self.assertEqual("streaming", asyncio.run(R.network_stream(nid, db=self.db)))
        self.assertEqual([2], seen)

    def test_a_network_out_of_the_pool_is_not_found(self):
        from fastapi import HTTPException
        from routers import livetv_catalog as R
        self.pool()
        nid = self.nid_of(1)
        C.set_excluded(self.db, nid, True)
        with self.assertRaises(HTTPException) as e:
            asyncio.run(R.network_stream(nid, db=self.db))
        self.assertEqual(404, e.exception.status_code)


class Logos(CatalogCase):
    def setUp(self):
        super().setUp()
        self.pool()   # folded

    def logo(self, cid, url):
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == cid).update({"logo_url": url})
        self.db.commit()

    def test_a_network_shows_the_logo_most_versions_share(self):
        # The provider gave the Buffalo version (the variant) another
        # channel's logo ("CTV Calary News" had RMC WOW's): outvoted.
        self.logo(1, "http://l/nbc.png?ver=1")
        self.logo(2, "http://l/nbc.png?ver=2")
        self.logo(3, "http://l/rmc-wow.jpg")
        nid = self.nid_of(1)
        self.assertEqual(3, self.pool()[f"n{nid}"]["channel_id"])
        self.assertEqual("http://l/nbc.png?ver=1", self.pool()[f"n{nid}"]["logo_url"])
        # A tie: the variant's own (TSN 1, two versions).
        self.logo(6, "http://l/tsn-a.png")
        self.logo(7, "http://l/tsn-b.png")
        tsn = self.pool()[f"n{self.nid_of(6)}"]
        self.assertEqual(f"http://l/tsn-{'a' if tsn['channel_id'] == 6 else 'b'}.png", tsn["logo_url"])

    def test_the_admins_logo_wins(self):
        self.logo(1, "http://l/nbc.png")
        nid = self.nid_of(1)
        C.set_logo(self.db, f"n{nid}", "https://logos.example/nbc.svg")
        C.set_logo(self.db, "c12", "https://logos.example/cnn.png")
        pool = self.pool()
        self.assertEqual("https://logos.example/nbc.svg", pool[f"n{nid}"]["logo_url"])
        self.assertEqual("https://logos.example/cnn.png", pool["c12"]["logo_url"])
        C.set_logo(self.db, "c12", "")
        self.assertIsNone(self.pool()["c12"]["logo_url"])
        self.assertIsNone(self.db.get(mdb.LiveChannel, 12).custom_logo_url)

    def test_only_web_addresses(self):
        for bad in ("javascript:alert(1)", "ftp://x/y.png", "https://x/a b.png", "https://x/" + "a" * 600):
            with self.assertRaises(C.CatalogError, msg=bad):
                C.set_logo(self.db, "c12", bad)
        with self.assertRaises(C.CatalogError):
            C.set_logo(self.db, "c999", "https://x/y.png")
        with self.assertRaises(C.CatalogError):
            C.set_logo(self.db, "y1", "https://x/y.png")

    def test_choices_the_provider_the_versions_the_guide_and_similar_names(self):
        self.logo(1, "http://l/nbc.png?ver=1")
        self.logo(2, "http://l/nbc.png?ver=2")    # the same picture: offered once
        self.logo(3, "http://l/rmc-wow.jpg")
        self.logo(4, "http://l/nbc-sports.png")
        nid = self.nid_of(1)
        urls = [c["url"] for c in C.logo_choices(self.db, f"n{nid}")["choices"]]
        self.assertEqual("http://l/nbc.png?ver=1", urls[0])
        self.assertIn("http://l/rmc-wow.jpg", urls)
        self.assertIn("http://l/nbc-sports.png", urls)     # "NBC Sports Boston": a similar name
        self.assertEqual(len(urls), len({u.split("?")[0] for u in urls}))
        # A single channel: its own, the same guide's, similar names.
        self.db.add(mdb.LiveChannel(id=30, provider_id=1, name="CA: CNN INTERNATIONAL", stream_id="130",
                                    stream_url="x", logo_url="http://l/cnn-i.png"))
        self.db.add(mdb.LiveChannel(id=31, provider_id=1, name="UK: SOMETHING ELSE", stream_id="131",
                                    stream_url="y", logo_url="http://l/cnn.png", epg_channel_id="cnn.ca"))
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 12).update(
            {"logo_url": "http://l/rmc-wow.jpg", "epg_channel_id": "cnn.ca"})
        self.db.commit()
        got = C.logo_choices(self.db, "c12")
        self.assertEqual(["http://l/rmc-wow.jpg", "http://l/cnn.png", "http://l/cnn-i.png"],
                         [c["url"] for c in got["choices"]])
        self.assertIsNone(got["custom_logo_url"])

    def test_the_route_tells_the_tuner(self):
        from routers import livetv_catalog as R
        r = R.catalog_set_logo(R.LogoBody(key="c12", url="https://logos.example/cnn.png"), db=self.db)
        self.assertEqual("https://logos.example/cnn.png", r["custom_logo_url"])
        self.sync.assert_called()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as e:
            R.catalog_set_logo(R.LogoBody(key="c12", url="file:///etc/passwd"), db=self.db)
        self.assertEqual(400, e.exception.status_code)


if __name__ == "__main__":
    unittest.main()
