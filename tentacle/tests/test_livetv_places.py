"""Live TV: each person's place for local and regional channels.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_places

An admin sets where each person watches from (default: the household's, the
first admin's); networks whose variant there isn't the default get a
channel of their own in the one lineup ("CTV · Vancouver", key
"n<id>@vancouver", stream /api/live/net/<id>/at/vancouver), hidden from
everyone elsewhere by a blocked tag; favourites follow the person's place.
The channel names are the provider's own (production, Oct 2026).
"""
import json
import logging
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import family as F
from services import livetv_catalog as C
from services import livetv_lineups as L
from services import livetv_locations as P
from services import livetv_names as N
from services import livetv_tuners as T
from test_livetv_lineup import ADMIN, ANN, BEN, BASE, LINEUP, FakeJellyfin
from tmp_dirs import temp_dir

CHANNELS = [
    # id, provider name, group, guide id
    (1, "CA EN: CTV TORONTO HD (CFTO)", "CA| GENERAL EN", "CTV.Toronto.ca"),
    (2, "CA EN: CTV VANCOUVER HD", "CA| GENERAL EN", "CTV.Vancouver.ca"),
    (3, "CA EN: CTV CALGARY", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (4, "CA: CTV MONTREAL HD", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (5, "CA EN: CTV OTTAWA HD", "CA| GENERAL EN", None),
    (10, "CA EN: GLOBAL TORONTO HD", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (11, "CA: GLOBAL VANCOUVER", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (12, "CA: GLOBAL CALGARY", "CA| CANADA ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (20, "CA EN: CBC TORONTO HD", "CA| GENERAL EN", None),
    (21, "CA EN: CBC VANCOUVER HD", "CA| GENERAL EN", None),
    (22, "CA EN: CBC CALGARY HD", "CA| GENERAL EN", None),
    (30, "US: NBC 2 (WGRZ) BUFFALO (H) ᴿᴬᵂ", "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (31, "US: NBC 5 (KING) SEATTLE (A) ᴿᴬᵂ", "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (32, "US: NBC 17 (KMOL) VICTORIA (D) ᴿᴬᵂ", "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (33, "US: NBC (KNBC) LOS ANGELES", "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (34, "US: NBC 4 (WDIV) DETROIT", "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (40, "TV: SPRINGFIELD, MA WGBY PBS 57 A3 HD DETROIT ᶜᶦᵗʸ ᴿᴬᵂ", "US| TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (41, "TV: BINGHAMTON, NY WSKG PBS 46 A3 HD CAMBRIDGE ᶜᶦᵗʸ ᴿᴬᵂ", "US| TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (42, "TV: SEATTLE, WA KBTC PBS 28 A3 HD SEATTLE ᶜᶦᵗʸ ᴿᴬᵂ", "US| TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ", None),
    (50, "US: DISCOVERY HD", "US| ENTERTAINMENT", "Discovery.us"),
    (51, "US: DISCOVERY WEST HD", "US| ENTERTAINMENT", "DiscoveryWest.us"),
    (60, "CA EN: CNN HD", "CA| NEWS EN", "CNN.us"),
    (70, "US: CBS (WGME) PORTLAND MAINE", "US| CBS", None),
    (71, "US: CBS 2 (WIVB) BUFFALO", "US| CBS", None),
]


class Places(unittest.TestCase):
    def test_a_place_is_its_city_then_province_then_nearby_then_its_feed(self):
        v = P.get("vancouver")["words"]
        self.assertEqual(["VANCOUVER", "BRITISH COLUMBIA"], v[:2])
        self.assertLess(v.index("VICTORIA"), v.index("SEATTLE"))
        self.assertLess(v.index("SEATTLE"), v.index("PACIFIC"))
        self.assertEqual("Vancouver, BC", P.get("vancouver")["label"])
        self.assertEqual("British Columbia", P.get("vancouver")["region"])
        t = P.get("toronto")["words"]
        self.assertLess(t.index("ONTARIO"), t.index("BUFFALO"))
        self.assertLess(t.index("BUFFALO"), t.index("DETROIT"))   # Buffalo for US networks
        self.assertEqual(["EAST", "EASTERN"], t[-2:])

    def test_a_tv_market_name_counts_only_its_market(self):
        # The place after "A3 HD" is often another city altogether.
        words = N.region_words("TV: SPRINGFIELD, MA WGBY PBS 57 A3 HD DETROIT ᶜᶦᵗʸ ᴿᴬᵂ")
        self.assertIn("SPRINGFIELD", words)
        self.assertIn("MASSACHUSETTS", words)
        self.assertNotIn("DETROIT", words)

    def test_a_border_stations_call_sign_is_its_city(self):
        self.assertEqual({"US", "BUFFALO"}, N.region_words("US: PBS 17 (WNED) ᴿᴬᵂ"))
        self.assertIn("BUFFALO", N.region_words("US: CW 23 HD (WNLO-TV)"))
        self.assertIn("DETROIT", N.region_words("US: ABC 7 (WXYZ)"))
        self.assertIn("SEATTLE", N.region_words("US: ABC 4 (KOMO)"))
        self.assertNotIn("SEATTLE", N.region_words("US: KING OF THE HILL"))

    def test_a_city_named_west_is_no_west_feed(self):
        self.assertNotIn("WEST", N.region_words("US: ABC 25 (WPBF) West Palm Beach HD"))
        self.assertNotIn("WEST", N.region_words("US: ABC 5 WEST DES MOINES IA (WOI)"))
        self.assertIn("WEST", N.region_words("US: DISCOVERY WEST HD"))

    def test_a_city_of_another_country_or_state_is_not_the_place(self):
        tiers = P.get("vancouver")["tiers"]
        self.assertEqual(0, C._place_rank(N.region_words("US: NBC 17 (KMOL) VICTORIA (D)"), tiers, "US")[0])
        self.assertEqual(3, C._place_rank(N.region_words("CA: CTV VICTORIA"), tiers, "CA")[0])
        portland = P.get("portland")["tiers"]
        self.assertEqual(0, C._place_rank(N.region_words("US: CBS (WGME) PORTLAND MAINE"), portland, "US")[0])
        self.assertEqual(4, C._place_rank(N.region_words("US: FOX 12 (KPTV) PORTLAND HD"), portland, "US")[0])


class Case(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        for cid, name, group, gid in CHANNELS:
            self.db.add(mdb.LiveChannel(id=cid, provider_id=p.id, name=name, stream_id=str(100 + cid),
                                        stream_url=f"http://192.0.2.10/live/{cid}.ts", group_title=group,
                                        epg_channel_id=gid, enabled=True, sort_order=cid))
        now = datetime.utcnow()
        for gid in ("CTV.Toronto.ca", "CTV.Vancouver.ca", "Discovery.us", "DiscoveryWest.us", "CNN.us"):
            self.db.add(mdb.EPGProgram(channel_id=gid, title=f"On {gid}", start=now - timedelta(minutes=5),
                                       stop=now + timedelta(hours=2)))
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.ann = mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann")
        self.ben = mdb.TentacleUser(jellyfin_user_id=BEN, display_name="Ben")
        self.db.add_all([self.admin, self.ann, self.ben])
        for k, v in (("livetv_region", "CA: Toronto, Ontario, Buffalo, East"), ("jellyfin_url", "http://jf.lan:8096"),
                     ("jellyfin_api_key", "k"), (T.ALL_TUNER_URL_KEY, LINEUP), (T.TUNER_BASE_KEY, BASE)):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        C._invalidate()
        self.addCleanup(C._invalidate)
        pt = mock.patch("services.livetv_tuners.schedule_sync")
        self.sync_calls = pt.start()
        self.addCleanup(pt.stop)

    def net(self, cid):
        C.ensure_fresh(self.db)
        return self.db.get(mdb.LiveChannelCatalog, cid).network_id

    def lineup(self):
        C._invalidate()
        return L.lineup(self.db)

    def move(self, user, slug):
        user.livetv_location = slug
        self.db.commit()


class Variants(Case):
    def test_the_household_is_the_first_admins_place(self):
        self.assertEqual("toronto", P.household(self.db))      # from the region setting
        self.move(self.admin, "calgary")
        self.assertEqual("calgary", P.household(self.db))
        self.assertEqual("calgary", P.of_person(self.db, self.ann))

    def test_defaults_are_the_households(self):
        L.pool(self.db)
        chosen = C.chosen_variants(self.db)
        self.assertEqual(1, chosen[self.net(1)])     # CTV Toronto
        self.assertEqual(10, chosen[self.net(10)])   # Global Toronto
        self.assertEqual(20, chosen[self.net(20)])   # CBC Toronto
        self.assertEqual(30, chosen[self.net(30)])   # NBC: Buffalo for US networks
        self.assertEqual(71, chosen[self.net(70)])   # CBS Buffalo
        # PBS: not "Springfield, MA ... DETROIT" (the Detroit is another city's tag)
        self.assertEqual(41, chosen[self.net(40)])
        self.assertEqual(50, chosen[self.net(50)])   # Discovery's East feed

    def test_toronto_has_buffalos_station_of_every_us_network(self):
        # Named by call sign alone, the Buffalo stations still win; without
        # one, the nearest market there is (PBS: Binghamton).
        for cid, name in ((43, "US: PBS 17 (WNED) ᴿᴬᵂ"), (35, "US: ABC 4 (KOMO) SEATTLE"),
                          (36, "US: ABC 7 HD (WKBW)"), (37, "US: FOX 11 (KTTV) LOS ANGELES"),
                          (38, "US: FOX 29 (WUTV)"), (72, "US: CW 11 (WPIX) NEW YORK"), (73, "US: CW 23 (WNLO)")):
            self.db.add(mdb.LiveChannel(id=cid, provider_id=1, name=name, stream_id=str(100 + cid),
                                        stream_url=f"http://192.0.2.10/live/{cid}.ts", group_title="US| LOCALS",
                                        enabled=True, sort_order=cid))
        self.db.commit()
        L.pool(self.db)
        chosen = C.chosen_variants(self.db)
        self.assertEqual(43, chosen[self.net(41)])   # PBS: WNED
        self.assertEqual(36, chosen[self.net(35)])   # ABC: WKBW
        self.assertEqual(38, chosen[self.net(37)])   # FOX: WUTV
        self.assertEqual(73, chosen[self.net(72)])   # CW: WNLO
        self.assertEqual(30, chosen[self.net(30)])   # NBC: WGRZ
        self.assertEqual(71, chosen[self.net(70)])   # CBS: WIVB
        got = C.place_variants(self.db, ["vancouver"])["vancouver"]
        self.assertEqual(42, got[self.net(41)])      # Vancouver: Seattle's
        self.assertEqual(35, got[self.net(35)])
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 43).update({"enabled": False})
        self.db.commit()
        L.pool(self.db)
        self.assertEqual(41, C.chosen_variants(self.db)[self.net(41)])

    def test_vancouver_gets_its_local_stations_and_seattle_for_us_networks(self):
        L.pool(self.db)
        got = C.place_variants(self.db, ["vancouver"])["vancouver"]
        self.assertEqual({self.net(1): 2, self.net(10): 11, self.net(20): 21, self.net(30): 31,
                          self.net(40): 42, self.net(50): 51}, got)
        self.assertNotIn(32, got.values())          # Victoria, Texas is not Victoria BC
        self.assertIsNone(self.net(60))             # CNN is CNN everywhere

    def test_calgary_and_montreal(self):
        L.pool(self.db)
        got = C.place_variants(self.db, ["calgary", "montreal"])
        self.assertEqual(3, got["calgary"][self.net(1)])
        self.assertEqual(12, got["calgary"][self.net(10)])
        self.assertEqual(22, got["calgary"][self.net(20)])
        self.assertEqual(4, got["montreal"][self.net(1)])
        self.assertNotIn(self.net(10), got["montreal"])   # no Global Montreal: Toronto's it is

    def test_the_households_place_has_no_variants_of_its_own(self):
        self.assertEqual({}, C.place_variants(self.db, ["toronto"]))

    def test_a_broken_variant_is_never_a_places(self):
        with mock.patch.object(C, "_check_states", return_value={2: "broken"}):
            L.pool(self.db)
            self.assertNotIn(self.net(1), C.place_variants(self.db, ["vancouver"])["vancouver"])


class Lineup(Case):
    def test_nobody_elsewhere_no_extra_channels(self):
        self.assertFalse([e for e in self.lineup() if e.get("place")])

    def test_a_places_channel_sits_after_the_networks_with_its_number(self):
        before = self.lineup()
        self.move(self.ann, "vancouver")
        after = self.lineup()
        keys = [e["key"] for e in after]
        ctv = f"n{self.net(1)}"
        i = keys.index(ctv)
        self.assertEqual(f"{ctv}@vancouver", keys[i + 1])
        e = after[i + 1]
        self.assertEqual("CTV · Vancouver", e["name"])
        self.assertEqual(after[i]["number"], e["number"])
        self.assertEqual(f"/api/live/net/{self.net(1)}/at/vancouver", e["stream_path"])
        self.assertEqual(2, e["channel_id"])
        nbc = next(x for x in after if x["key"] == f"n{self.net(30)}@vancouver")
        self.assertEqual("NBC · Seattle", nbc["name"])
        # Every channel of before keeps its Jellyfin id and number
        old = L.item_ids_for(LINEUP, before)
        new = L.item_ids_for(LINEUP, [x for x in after if not x.get("place")])
        self.assertEqual(old, new)
        self.assertEqual([x["number"] for x in before], [x["number"] for x in after if not x.get("place")])

    def test_a_places_channel_keeps_its_id_whichever_variant_it_streams(self):
        self.move(self.ann, "vancouver")
        first = L.item_ids_for(LINEUP, [e for e in self.lineup() if e.get("place")])
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 31).update({"enabled": False})
        self.db.commit()
        after = {k: i for i, k in L.item_ids_for(LINEUP, [e for e in self.lineup() if e.get("place")]).items()}
        for i, k in first.items():
            if k in after:
                self.assertEqual(i, after[k])
        self.assertNotIn(f"n{self.net(30)}@vancouver", after)   # no other Seattle-ish NBC: the default

    def test_each_person_sees_their_places_channel(self):
        self.move(self.ann, "vancouver")
        entries = self.lineup()
        ctv = f"n{self.net(1)}"
        mine = {e["key"] for e in L.view_for(entries, "vancouver")}
        self.assertIn(f"{ctv}@vancouver", mine)
        self.assertNotIn(ctv, mine)
        home = {e["key"] for e in L.view_for(entries, "toronto")}
        self.assertIn(ctv, home)
        self.assertFalse([k for k in home if "@" in k])
        tags = L.place_tags(entries, {"toronto", "vancouver"})
        self.assertEqual({"tentacle-notlocal-toronto"}, tags[f"{ctv}@vancouver"])
        self.assertEqual({"tentacle-notlocal-vancouver"}, tags[ctv])
        self.assertNotIn("c60", tags)   # CNN: everyone

    def test_the_guide_follows_the_places_variant(self):
        from routers.livetv import hdhr_xmltv, live_tuner_all
        self.move(self.ann, "vancouver")
        entries = self.lineup()
        e = next(x for x in entries if x["key"] == f"n{self.net(1)}@vancouver")
        self.assertTrue(e["has_guide"])
        req = mock.Mock(base_url="http://tentacle.lan:8888/", headers={}, url=mock.Mock(path=L.ALL_TUNER_PATH))
        with mock.patch("routers.livetv._tuner_m3u", lambda request, ents, url: L.m3u(ents, BASE)):
            m3u = live_tuner_all(req, self.db)
        self.assertIn(f'tvg-id="{e["xmltv_id"]}" tvg-chno="{e["number"]}"', m3u)
        self.assertIn(f"{BASE}/api/live/net/{self.net(1)}/at/vancouver", m3u)
        resp = hdhr_xmltv(self.db)
        with open(resp.path) as f:
            xml = f.read()
        os.unlink(resp.path)
        self.assertIn(f'<programme start=', xml)
        self.assertRegex(xml, rf'channel="{e["xmltv_id"]}"[^>]*>\s*<title>On CTV.Vancouver.ca</title>')

    def test_the_stream_is_the_places_variant(self):
        self.assertEqual(2, C.place_channel(self.db, self.net(1), "vancouver"))
        self.assertEqual(1, C.place_channel(self.db, self.net(1), "toronto"))
        self.assertEqual("/api/live/net/7/at/vancouver", L.stream_path_of_key("n7@vancouver"))
        self.assertEqual("n7", L.base_key("n7@vancouver"))


class Sync(Case):
    def setUp(self):
        super().setUp()
        self.jf = FakeJellyfin(self.Session, [(ADMIN, "lucas"), (ANN, "ann"), (BEN, "ben")])
        for target, name, value in (
            (T, "jellyfin_for", lambda db: self.jf),
            (T, "refresh_and_wait", lambda db, jf: jf.refresh() or True),
            (T, "wait_for_guide", lambda jf: True),
            (T, "BACKUP_DIR", os.path.join(self.tmp, "backups")),
        ):
            pt = mock.patch.object(T, name, value)
            pt.start()
            self.addCleanup(pt.stop)

    def sync(self):
        C._invalidate()
        return T.sync(self.db)

    def ids(self):
        return {k: i for i, k in L.item_ids_for(LINEUP, self.lineup()).items()}

    def names(self, uid):
        return self.jf.seen_by(uid)

    def fav_names(self, uid):
        return sorted(self.jf.channels[i]["Name"] for i in self.jf.favourites(uid))

    def test_two_people_in_different_cities_each_see_their_own(self):
        self.sync()
        lucas_before = self.names(ADMIN)
        self.assertEqual([], self.jf.users[ADMIN]["Policy"]["BlockedTags"])
        self.move(self.ann, "vancouver")
        self.sync()
        self.assertIn("CTV · Vancouver", self.names(ANN))
        self.assertNotIn("CTV", self.names(ANN))
        self.assertIn("NBC · Seattle", self.names(ANN))
        self.assertIn("CNN", self.names(ANN))
        # Nothing changed for Lucas (Toronto): same channels, same ids
        self.assertEqual(lucas_before, self.names(ADMIN))
        self.assertEqual(["tentacle-notlocal-toronto"], self.jf.users[ADMIN]["Policy"]["BlockedTags"])
        self.assertEqual(["tentacle-notlocal-vancouver"], self.jf.users[ANN]["Policy"]["BlockedTags"])
        self.assertEqual(lucas_before, self.names(BEN))
        # Programmes carry the place tags too (the plugin copies the channel's)
        ctv_van = self.ids()[f"n{self.net(1)}@vancouver"]
        self.assertIn("tentacle-notlocal-toronto", self.jf.channels[ctv_van]["Tags"])
        self.assertIn("tentacle-notlocal-toronto", self.jf.programme_tags[ctv_van])
        self.assertIn("tentacle-notlocal-vancouver", self.jf.programme_tags[self.ids()[f"n{self.net(1)}"]])

    def test_favourites_follow_a_change_of_place(self):
        self.move(self.ann, "vancouver")
        self.sync()
        ids = self.ids()
        ctv = f"n{self.net(1)}"
        self.jf.favs[ANN] = {ids[f"{ctv}@vancouver"], ids["c60"], ids[f"n{self.net(30)}@vancouver"]}
        self.assertEqual(["CNN", "CTV · Vancouver", "NBC · Seattle"], self.fav_names(ANN))
        self.move(self.ann, "calgary")
        self.sync()
        self.assertEqual(["CNN", "CTV · Calgary", "NBC"], self.fav_names(ANN))
        self.assertNotIn(f"{ctv}@vancouver", self.ids())                 # nobody in Vancouver now
        self.assertEqual(ids["c60"], self.ids()["c60"])   # CNN: same channel
        self.assertIn("CTV · Calgary", self.names(ANN))
        self.assertNotIn("CTV", self.names(ANN))
        # Back to the household's place: the defaults, and no place tags left
        self.move(self.ann, None)
        self.sync()
        self.assertEqual(["CNN", "CTV", "NBC"], self.fav_names(ANN))
        self.assertEqual([], self.jf.users[ANN]["Policy"]["BlockedTags"])
        self.assertEqual([], self.jf.users[ADMIN]["Policy"]["BlockedTags"])
        self.assertFalse([e for e in self.lineup() if e.get("place")])

    def test_a_new_person_elsewhere_starts_with_someones_favourites_in_their_place(self):
        self.sync()
        ids = self.ids()
        self.jf.favs[ADMIN] = {ids[f"n{self.net(1)}"], ids[f"n{self.net(10)}"]}
        self.jf.favs[BEN] = set(self.jf.favs[ADMIN])      # family.copy_channels
        self.move(self.ben, "vancouver")
        self.sync()
        self.assertEqual(["CTV · Vancouver", "Global · Vancouver"], self.fav_names(BEN))
        self.assertEqual(["CTV", "Global"], self.fav_names(ADMIN))

    def test_a_kid_elsewhere_keeps_kids_channels_only(self):
        self.ann.kids_level = "young"
        self.db.commit()
        mdb.set_setting(self.db, F.KIDS_CHANNELS_KEY, json.dumps([f"n{self.net(50)}"]))   # Discovery
        self.db.commit()
        self.move(self.ann, "vancouver")
        self.sync()
        self.assertEqual(["Discovery · West"], self.names(ANN))
        self.assertEqual(sorted([L.ALL_TAG, "tentacle-notlocal-vancouver"]),
                         sorted(self.jf.users[ANN]["Policy"]["BlockedTags"]))


class MyChannels(Case):
    def setUp(self):
        super().setUp()
        from routers import my_livetv
        from routers.auth import get_user_from_request
        self.jf = FakeJellyfin(self.Session, [(ADMIN, "lucas"), (ANN, "ann"), (BEN, "ben")])
        self.jf.tuners.append({"Id": "all", "Type": "m3u", "Url": LINEUP})
        self.move(self.ann, "vancouver")
        self.jf.refresh()
        pt = mock.patch.object(T, "jellyfin_for", lambda db: self.jf)
        pt.start()
        self.addCleanup(pt.stop)
        app = FastAPI()
        app.include_router(my_livetv.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        self.client = TestClient(app)

    def test_my_channels_are_my_places(self):
        d = self.client.get("/api/my/channels").json()
        names = [c["name"] for c in d["channels"]]
        self.assertIn("CTV · Vancouver", names)
        self.assertNotIn("CTV", names)
        self.assertEqual({"slug": "vancouver", "own": True}, {k: d["location"][k] for k in ("slug", "own")})

    def test_a_quick_pick_stars_my_places_channel(self):
        ctv = f"n{self.net(1)}"
        d = self.client.put("/api/my/channels/favourites", json={"keys": [ctv], "favourite": True}).json()
        self.assertEqual([f"{ctv}@vancouver"], [c["key"] for c in d["mine"]])


class People(Case):
    def test_an_invite_can_set_the_place(self):
        from test_family import FakeJellyfin as FamilyJellyfin
        jf = FamilyJellyfin([(ADMIN, "Lucas", True)])
        with mock.patch("services.family.start_new_person"), mock.patch("services.family.jellyfin", return_value=jf):
            F.invite(self.db, self.admin, "Grandma", location="vancouver")
        row = self.db.query(mdb.TentacleUser).filter(mdb.TentacleUser.display_name == "Grandma").one()
        self.assertEqual("vancouver", row.livetv_location)
        with self.assertRaises(F.FamilyError):
            F.invite(self.db, self.admin, "Bob", location="atlantis")

    def test_an_invite_at_the_households_place_follows_it(self):
        self.assertIsNone(F._own_place(self.db, "toronto"))
        self.assertIsNone(F._own_place(self.db, None))
        self.assertEqual("calgary", F._own_place(self.db, "calgary"))

    def test_changing_a_place_syncs_and_the_owners_is_the_households(self):
        out = F.set_location(self.db, self.ann, "vancouver")
        self.assertEqual("vancouver", out["location"]["slug"])
        self.assertTrue(out["location"]["own"])
        self.sync_calls.assert_called_once()
        F.set_location(self.db, self.ann, "toronto")        # the household's: follows it
        self.assertIsNone(self.ann.livetv_location)
        F.set_location(self.db, self.admin, "calgary")
        self.assertEqual("calgary", P.household(self.db))
        self.assertEqual("calgary", F.person(self.db, self.ann)["location"]["slug"])
        F.set_location(self.db, self.admin, "")              # the owner always has one
        self.assertEqual("calgary", self.admin.livetv_location)

    def test_the_people_route_lists_the_places(self):
        from routers import family
        from routers.auth import require_admin
        from test_family import FakeJellyfin as FamilyJellyfin
        jf = FamilyJellyfin([(ADMIN, "Lucas", True), (ANN, "Ann", False), (BEN, "Ben", False)])
        app = FastAPI()
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: self.admin
        client = TestClient(app)
        with mock.patch("services.family.jellyfin", return_value=jf), \
                mock.patch("services.livetv_tuners.jellyfin_for", return_value=None), \
                mock.patch("services.tv_app.latest", return_value=None):
            self.assertEqual(200, client.put(f"/api/family/people/{self.ann.id}", json={"location": "vancouver"}).status_code)
            self.assertEqual(400, client.put(f"/api/family/people/{self.ann.id}", json={"location": "mars"}).status_code)
            d = client.get("/api/family/people").json()
        slugs = [x["slug"] for x in d["locations"]]
        self.assertIn("vancouver", slugs)
        self.assertIn("toronto", slugs)
        self.assertIn("seattle", slugs)                     # US places the channels name
        self.assertNotIn("anchorage", slugs)                # none of the channels is from there
        self.assertEqual("toronto", d["household_location"]["slug"])
        ann = next(p for p in d["people"] if p["name"] == "Ann")
        self.assertEqual("vancouver", ann["location"]["slug"])


if __name__ == "__main__":
    unittest.main()
