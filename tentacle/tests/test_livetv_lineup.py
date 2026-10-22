"""Live TV: one lineup for the household, set up in Jellyfin by Tentacle;
everyone's favourites are their own.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_lineup

What was proven on a throwaway Jellyfin 10.11.8 (docs/agents/jellyfin-notes.md,
"Live TV per person") and is pinned here:
  - Jellyfin's ids for M3U channels (MD5 of tuner URL + stream URL, UTF-16,
    as a .NET Guid) and for the channel items: Tentacle computes them itself.
    The expected values are ones Jellyfin 10.11.8 gave.
  - The lineup: the pool in its order is the numbers; event channels with
    no event now are left out.
  - The sync against a fake Jellyfin that behaves like the real one where it
    mattered in the end-to-end runs: blocked tags hide channels from a
    person; a favourite on a channel the person can't see is refused (404);
    a guide refresh creates the channels of every tuner (tagged by the
    plugin) and drops those of removed tuners. Moving off per-person tuners,
    the shared tuner and the old HDHomeRun keeps every favourite and
    schedule.
"""
import json
import logging
import os
import unittest
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import livetv_lineups as L
from services import livetv_tuners as T
from tmp_dirs import temp_dir

BASE = "http://tentacle.lan:8888"
ADMIN, ANN, BEN = "a" * 32, "b" * 32, "c" * 32
LINEUP = BASE + L.ALL_TUNER_PATH


class Ids(unittest.TestCase):
    def test_m3u_ids_are_jellyfins(self):
        # Seen in Jellyfin 10.11.8's database for this tuner and stream.
        ext = L.m3u_external_id("http://livetv-test-files:8000/alice.m3u",
                                "http://livetv-test-files:8000/streams/news.ts")
        self.assertEqual("m3u_3f984b526ee00dcbe360111705ad0cb41f2626028381d3496cac17c1dc341d96", ext)
        self.assertEqual("1f42638edad925001dbfcbc3dcba46f8", L.channel_item_id(ext))

    def test_renumbering_keeps_the_id(self):
        tuner = BASE + L.user_tuner_path(ANN)
        one = L.item_ids_for(tuner, [{"key": "c1", "stream_path": "/api/live/stream/1", "number": 1}])
        five = L.item_ids_for(tuner, [{"key": "c1", "stream_path": "/api/live/stream/1", "number": 5}])
        self.assertEqual(one, five)

    def test_each_tuner_has_its_own_channel(self):
        e = [{"key": "c1", "stream_path": "/api/live/stream/1"}]
        self.assertNotEqual(set(L.item_ids_for(BASE + L.ALL_TUNER_PATH, e)),
                            set(L.item_ids_for(BASE + L.user_tuner_path(ANN), e)))

    def test_tags_from_tuner_urls(self):
        self.assertEqual(L.ALL_TAG, L.tag_for_tuner_url(BASE + "/api/live/tuner/all.m3u"))
        self.assertEqual(L.TAG_PREFIX + ANN, L.tag_for_tuner_url(BASE + f"/api/live/users/{ANN.upper()}/tuner.m3u"))
        self.assertIsNone(L.tag_for_tuner_url(BASE + "/api/live/playlist.m3u"))
        self.assertIsNone(L.tag_for_tuner_url("http://other/iptv.m3u"))


class BlockedTags(unittest.TestCase):
    def test_own_tags_kept_kids_block_the_rest_and_old_tuners_blocked(self):
        tags = {L.ALL_TAG, L.KIDS_TAG, L.user_tag(BEN)}
        self.assertEqual(["Horror", L.user_tag(BEN)],
                         T.blocked_tags(["Horror", L.user_tag("d" * 32), L.ALL_TAG], False, tags))
        self.assertEqual([L.ALL_TAG, L.user_tag(BEN)], T.blocked_tags([], True, tags))
        self.assertEqual([L.ALL_TAG], T.blocked_tags([], True, set()))
        self.assertEqual([], T.blocked_tags([], False, {L.ALL_TAG, L.KIDS_TAG}))


class DbCase(unittest.TestCase):
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
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        for i, name in enumerate(["News", "Sport", "Kids", 'Bad "name"\nNext'], 1):
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=str(100 + i),
                                        stream_url=f"http://192.0.2.10/live/{i}.ts", enabled=True,
                                        sort_order=i, logo_url=f"http://img/{i}.png"))
        self.db.add(mdb.LiveChannel(provider_id=p.id, name="Off", stream_id="999",
                                    stream_url="http://192.0.2.10/live/9.ts", enabled=False))
        self.db.commit()
        for k, v in (("jellyfin_url", "http://jf.lan:8096"), ("jellyfin_api_key", "k")):
            mdb.set_setting(self.db, k, v)
        self.db.commit()


class Lineups(DbCase):
    def test_pool_is_the_enabled_channels_in_order(self):
        self.assertEqual(["c1", "c2", "c3", "c4"], [c["key"] for c in L.pool(self.db)])
        self.assertEqual("101", L.pool(self.db)[0]["xmltv_id"])

    def test_the_lineup_is_numbered_in_the_pools_order(self):
        self.assertEqual([("c1", 1), ("c2", 2), ("c3", 3), ("c4", 4)],
                         [(c["key"], c["number"]) for c in L.lineup(self.db)])

    def test_event_channels_without_an_event_are_left_out(self):
        pool = [{"key": "c1", "event": None}, {"key": "c2", "event": "no_event"},
                {"key": "c3", "event": {"title": "Leafs vs Bruins"}}]
        self.assertEqual([("c1", 1), ("c3", 2)], [(c["key"], c["number"]) for c in L.lineup(self.db, pool)])

    def test_pool_keeps_the_old_tuners_order(self):
        # Jellyfin listed the old tuner's channels by number: 9 before 10,
        # whatever their names, unless the admin set an order.
        self.db.query(mdb.LiveChannel).update({"sort_order": 0})
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 1).update({"stream_id": "1000"})
        self.db.commit()
        self.assertEqual(["c2", "c3", "c4", "c1"], [c["key"] for c in L.pool(self.db)])

    def test_a_channel_switched_off_leaves_the_lineup(self):
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 2).update({"enabled": False})
        self.db.commit()
        self.assertEqual([("c1", 1), ("c3", 2), ("c4", 3)], [(c["key"], c["number"]) for c in L.lineup(self.db)])

    def test_old_keys_follow_their_aliases(self):
        # "c8" became "c9", which became "c2" (folded, then merged); "c7" became
        # a key the pool doesn't have.
        alias = {"c8": "c9", "c9": "c2", "c7": "c99"}
        with mock.patch.object(L, "aliases", lambda db: alias):
            self.assertEqual(["c2", "c1"], L.resolve_keys(self.db, ["c8", "c1", "c9", "c7"],
                                                          {c["key"] for c in L.pool(self.db)}))
            self.assertEqual({"c8": "c2", "c9": "c2"}, L.final_aliases(self.db, L.pool(self.db)))
        self.assertEqual("/api/live/net/7", L.stream_path_of_key("n7"))
        self.assertEqual("/api/live/stream/12", L.stream_path_of_key("c12"))

    def test_a_list_of_before_is_served_until_its_tuner_goes(self):
        self.db.add(mdb.LiveUserLineup(jellyfin_user_id=ANN, own_list=True))
        for pos, k in enumerate(["c3", "c1", "c9"]):
            self.db.add(mdb.LiveUserChannel(jellyfin_user_id=ANN, channel_key=k, position=pos))
        self.db.commit()
        self.assertEqual([("c3", 1), ("c1", 2)], [(c["key"], c["number"]) for c in L.legacy_lineup(self.db, ANN)])

    def test_no_catalog_no_aliases(self):
        with mock.patch.dict("sys.modules", {"services.livetv_catalog": None}):
            self.assertEqual({}, L.aliases(self.db))
        broken = mock.Mock(aliases=mock.Mock(side_effect=RuntimeError("no table")))
        with mock.patch.dict("sys.modules", {"services.livetv_catalog": broken}):
            import services
            with mock.patch.object(services, "livetv_catalog", broken, create=True):
                self.assertEqual({}, L.aliases(self.db))

    def test_m3u(self):
        text = L.m3u(L.lineup(self.db), BASE)
        lines = text.splitlines()
        self.assertEqual("#EXTM3U", lines[0])
        self.assertEqual('#EXTINF:-1 tvg-id="101" tvg-chno="1" tvg-logo="http://img/1.png",News', lines[1])
        self.assertEqual(BASE + "/api/live/stream/1", lines[2])
        self.assertNotIn("group-title", text)
        self.assertEqual(9, len(lines))   # a line break in a name never starts a channel
        self.assertIn(",Bad 'name' Next", text)


class TunerRoutes(DbCase):
    def setUp(self):
        super().setUp()
        from routers import livetv
        app = FastAPI()
        app.include_router(livetv.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.client = TestClient(app)

    def test_streams_sit_under_the_tuners_own_address(self):
        mdb.set_setting(self.db, T.ALL_TUNER_URL_KEY, "http://saved:8888" + L.ALL_TUNER_PATH)
        self.db.commit()
        r = self.client.get("/api/live/tuner/all.m3u")
        self.assertEqual(200, r.status_code)
        self.assertIn("http://saved:8888/api/live/stream/1", r.text)
        self.assertEqual(9, len(r.text.splitlines()))

    def test_a_persons_tuner_of_before(self):
        self.db.add(mdb.LiveUserLineup(jellyfin_user_id=ANN, own_list=True, synced_own_list=True,
                                       tuner_url="http://x:1" + L.user_tuner_path(ANN)))
        self.db.add(mdb.LiveUserChannel(jellyfin_user_id=ANN, channel_key="c2", position=0))
        self.db.commit()
        r = self.client.get(f"/api/live/users/{ANN}/tuner.m3u")
        self.assertEqual(['#EXTINF:-1 tvg-id="102" tvg-chno="1" tvg-logo="http://img/2.png",Sport',
                          "http://x:1/api/live/stream/2"], r.text.splitlines()[1:])
        self.assertEqual(404, self.client.get("/api/live/users/..%2Fetc/tuner.m3u").status_code)
        self.assertEqual("#EXTM3U\n", self.client.get(f"/api/live/users/{BEN}/tuner.m3u").text)


# ── A fake Jellyfin ─────────────────────────────────────────────────────────

class FakeJellyfin:
    """Jellyfin's Live TV as far as the sync uses it. Channels appear and go
    only on refresh(), tagged as the plugin tags them; people see only
    channels whose tags they don't block."""

    def __init__(self, db_factory, users):
        self.db_factory = db_factory
        self.url = "http://jf.lan:8096"
        self.headers = {"X-Emby-Token": "k"}
        self.users = {u: {"Id": u, "Name": n, "Policy": {"IsAdministrator": u == ADMIN, "EnableLiveTvAccess": True,
                                                         "BlockedTags": []}} for u, n in users}
        self.tuners = []
        self.listings = []
        self.channels = {}      # item id -> {"Id", "Name", "Tags", "tuner"}
        self.favs = {u: set() for u, _ in users}
        self.series = []
        self.timers = []
        self.refreshes = 0
        self.programme_tags = {}   # channel item id -> the tags its programmes got at the last refresh
        self.calls = []

    # helpers
    def add_hdhr(self):
        self.tuners.append({"Id": "hd", "Type": "hdhomerun", "Url": BASE, "DeviceId": "TENTACLE1",
                            "ReadAtNativeFramerate": True, "AllowHWTranscoding": True})
        self.refresh()

    def visible(self, user, item_id):
        ch = self.channels.get(item_id)
        blocked = {t.lower() for t in self.users[user]["Policy"]["BlockedTags"]}
        return ch is not None and not ({t.lower() for t in ch["Tags"]} & blocked)

    def favourites(self, user):
        """A person's favourite channels Jellyfin still has."""
        return self.favs[user] & set(self.channels)

    def seen_by(self, user):
        return sorted(c["Name"] for i, c in self.channels.items() if self.visible(user, i))

    def refresh(self):
        self.refreshes += 1
        db = self.db_factory()
        try:
            pool = L.pool(db)
            want = {}
            for t in self.tuners:
                if t["Type"] == "hdhomerun":
                    for c in pool:
                        want[L.channel_item_id(L.hdhr_external_id(c["xmltv_id"]))] = (c["name"], t["Url"])
                    continue
                m = L.USER_TUNER_RE.search(t["Url"])   # as the tuner routes serve them
                entries = L.legacy_lineup(db, m.group(1), pool) if m else L.lineup(db, pool)
                for e in entries:
                    want[L.m3u_channel_item_id(t["Url"], L.tuner_base(t["Url"]) + e["stream_path"])] = (e["name"], t["Url"])
        finally:
            db.close()
        for i in list(self.channels):
            if i not in want:
                del self.channels[i]
        for i, (name, tuner) in want.items():
            tag = L.tag_for_tuner_url(tuner)    # the plugin, at creation
            self.channels.setdefault(i, {"Id": i, "Name": name, "Tags": [tag] if tag else [], "tuner": tuner})
        # The plugin copies the channel's tag onto its programmes as they are built
        self.programme_tags = {i: list(c["Tags"]) for i, c in self.channels.items()}
        # Jellyfin's RefreshTimers: a one-off whose programme left the guide goes
        self.timers = [t for t in self.timers if t.get("SeriesTimerId") or t.get("ChannelId") in self.channels]

    # the Jellyfin wrapper's interface
    def get(self, path, **p):
        self.calls.append(("GET", path, p))
        if path == "/System/Configuration/livetv":
            return {"TunerHosts": [dict(t) for t in self.tuners], "ListingProviders": list(self.listings)}
        if path == "/Users":
            return [json.loads(json.dumps(u)) for u in self.users.values()]
        if path.startswith("/Users/") and path.count("/") == 2:
            return json.loads(json.dumps(self.users[path.split("/")[2]]))
        if path == "/Items":
            if p.get("includeItemTypes") == "LiveTvChannel":
                return {"Items": [dict(c) for c in self.channels.values()]}
            ids = p["ids"].split(",")
            return {"Items": [dict(self.channels[i]) for i in ids if i in self.channels]}
        if path == "/LiveTv/Channels":
            u = p["userId"]
            items = [c for i, c in self.channels.items() if self.visible(u, i)]
            if p.get("isFavorite") == "true":
                items = [c for c in items if c["Id"] in self.favs[u]]
            return {"Items": [dict(c) for c in items]}
        if path == "/LiveTv/SeriesTimers":
            return {"Items": list(self.series)}
        if path == "/LiveTv/Timers":
            return {"Items": list(self.timers)}
        if path == "/LiveTv/Programs":
            return {"Items": [{"Id": f"p-{p['channelIds']}", "Name": "Hockey Night"}]}
        if path == "/LiveTv/Timers/Defaults":
            return {"ProgramId": p["programId"], "ChannelId": p["programId"][2:]}
        if path.startswith("/Plugins/"):
            return {"TentacleUrl": BASE}
        raise AssertionError(path)

    def post(self, path, body=None, **p):
        self.calls.append(("POST", path, body))
        if path == "/LiveTv/TunerHosts":
            if body.get("Id"):     # Jellyfin saves a tuner with an id in its place
                self.tuners = [dict(body) if t["Id"] == body["Id"] else t for t in self.tuners]
            else:
                self.tuners.append({**body, "Id": f"t{len(self.tuners)}"})
            return {}
        if path == "/LiveTv/ListingProviders":
            self.listings.append(body)
            return {}
        if path.startswith("/Users/") and path.endswith("/Policy"):
            self.users[path.split("/")[2]]["Policy"] = body
            return None
        if path.startswith("/Items/"):
            self.channels[path.split("/")[2]]["Tags"] = list(body["Tags"])
            return None
        if path.startswith("/UserFavoriteItems/"):
            i, u = path.split("/")[2], p["userId"]
            if not self.visible(u, i):
                r = requests.Response()
                r.status_code = 404
                raise requests.HTTPError(response=r)
            self.favs[u].add(i)
            return None
        if path == "/LiveTv/SeriesTimers":
            self.series.append({**body, "Id": f"s{len(self.series)}", "Name": "Hockey Night"})
            return None
        if path == "/LiveTv/Timers":
            self.timers.append({**body, "Id": f"x{len(self.timers)}"})
            return None
        if path.startswith("/ScheduledTasks/Running/"):
            self.refresh()
            return None
        raise AssertionError(path)

    def delete(self, path, **p):
        self.calls.append(("DELETE", path, p))
        if path == "/LiveTv/TunerHosts":
            self.tuners = [t for t in self.tuners if t["Id"] != p["id"]]
        elif path.startswith("/LiveTv/SeriesTimers/"):
            self.series = [s for s in self.series if s["Id"] != path.split("/")[-1]]
        elif path.startswith("/LiveTv/Timers/"):
            self.timers = [t for t in self.timers if t["Id"] != path.split("/")[-1]]
        elif path.startswith("/UserFavoriteItems/"):
            self.favs[p["userId"]].discard(path.split("/")[2])
        else:
            raise AssertionError(path)


class Sync(DbCase):
    def setUp(self):
        super().setUp()
        self.jf = FakeJellyfin(self.Session, [(ADMIN, "admin"), (ANN, "ann"), (BEN, "ben")])
        for target, name, value in (
            (T, "jellyfin_for", lambda db: self.jf),
            (T, "refresh_and_wait", lambda db, jf: jf.refresh() or True),
            (T, "wait_for_guide", lambda jf: True),
            (T, "BACKUP_DIR", os.path.join(self.tmp, "backups")),
        ):
            pt = mock.patch.object(T, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        pt = mock.patch("services.jellyfin_guide.refresh_jellyfin_guide", lambda *a, **k: self.jf.refresh())
        pt.start()
        self.addCleanup(pt.stop)

    def sync(self, refresh=False):
        return T.sync(self.db, refresh=refresh)

    def ids(self):
        """The lineup's Jellyfin ids by key."""
        return {k: i for i, k in L.item_ids_for(LINEUP, L.lineup(self.db)).items()}

    def hdhr_id(self, key):
        c = next(c for c in L.pool(self.db) if c["key"] == key)
        return L.channel_item_id(L.hdhr_external_id(c["xmltv_id"]))

    def first_backup(self):
        d = os.path.join(self.tmp, "backups")
        with open(os.path.join(d, sorted(os.listdir(d))[0])) as f:
            return json.load(f)

    def person(self, uid, name, **kw):
        u = mdb.TentacleUser(jellyfin_user_id=uid, display_name=name, **kw)
        self.db.add(u)
        self.db.commit()
        return u

    def test_a_new_server_gets_one_tuner_and_the_guide(self):
        done = self.sync()
        self.assertEqual([(LINEUP, "Tentacle · Live TV")], [(t["Url"], t["FriendlyName"]) for t in self.jf.tuners])
        self.assertEqual([BASE + "/api/live/xmltv.xml"], [lp["Path"] for lp in self.jf.listings])
        for u in (ADMIN, ANN, BEN):
            self.assertEqual(4, len(self.jf.seen_by(u)))
            self.assertEqual([], self.jf.users[u]["Policy"]["BlockedTags"])
        self.assertEqual(1, done["added"])
        again = self.sync()
        self.assertEqual((0, 0, False), (again["added"], again["removed"], again["refreshed"]))
        self.assertEqual(1, len(self.jf.listings))

    def test_read_at_native_frame_rate_is_switched_off_once(self):
        # A lineup tuner made by v1.13 or earlier carried it over from the old tuner:
        # live channels then took 3-11 s to their first HLS segment.
        self.sync()
        self.jf.tuners[0]["ReadAtNativeFramerate"] = True
        mdb.set_setting(self.db, T.NATIVE_RATE_KEY, "")
        self.db.commit()
        done = self.sync()
        self.assertTrue(done.get("native_rate_off"))
        self.assertEqual([(LINEUP, False)], [(t["Url"], t["ReadAtNativeFramerate"]) for t in self.jf.tuners])
        self.assertEqual("Tentacle · Live TV", self.jf.tuners[0]["FriendlyName"])   # the rest kept
        # Switched back on in Jellyfin afterwards: the admin's call, left alone.
        self.jf.tuners[0]["ReadAtNativeFramerate"] = True
        self.assertFalse(self.sync().get("native_rate_off"))
        self.assertTrue(self.jf.tuners[0]["ReadAtNativeFramerate"])

    def test_moves_everyone_off_the_old_hdhomerun_tuner(self):
        self.jf.add_hdhr()
        self.jf.favs[ANN] = {self.hdhr_id("c2")}
        self.jf.series.append({"Id": "old", "Name": "Hockey Night", "ChannelId": self.hdhr_id("c2"),
                               "RecordAnyTime": True, "KeepUpTo": 3})
        done = self.sync()
        self.assertEqual([LINEUP], [t["Url"] for t in self.jf.tuners])
        self.assertTrue(self.jf.tuners[0]["AllowHWTranscoding"])        # the old tuner's settings,
        self.assertFalse(self.jf.tuners[0]["ReadAtNativeFramerate"])    # but channels start fast
        self.assertEqual({self.ids()["c2"]}, self.jf.favourites(ANN))
        self.assertEqual([self.ids()["c2"]], [s["ChannelId"] for s in self.jf.series])
        self.assertEqual(3, self.jf.series[0]["KeepUpTo"])
        self.assertEqual((1, 1, 1), (done["favourites"], done["schedules"], done["removed"]))
        backup = self.first_backup()
        self.assertEqual("hdhomerun", backup["tuners"][0]["Type"])
        self.assertEqual(1, len([u for u in backup["users"] if u["favourite_channels"]]))
        self.assertEqual(4, len(self.jf.channels))
        for u in (ADMIN, ANN, BEN):
            self.assertEqual(4, len(self.jf.seen_by(u)))

    def test_a_person_with_their_own_blocked_tags_keeps_them(self):
        self.jf.users[BEN]["Policy"]["BlockedTags"] = ["Horror", L.user_tag(ANN)]
        self.sync()
        self.assertEqual(["Horror"], self.jf.users[BEN]["Policy"]["BlockedTags"])

    def test_people_gone_from_jellyfin_lose_their_list_of_before(self):
        self.db.add(mdb.LiveUserLineup(jellyfin_user_id="d" * 32, own_list=True))
        self.db.commit()
        self.sync()
        self.assertIsNone(L.lineup_row(self.db, "d" * 32))

    def test_tuner_address_is_kept_once_chosen(self):
        self.jf.add_hdhr()
        self.sync()
        self.assertEqual(BASE, mdb.get_setting(self.db, T.TUNER_BASE_KEY))
        self.assertEqual(LINEUP, mdb.get_setting(self.db, T.ALL_TUNER_URL_KEY))

    def test_not_connected_does_nothing(self):
        with mock.patch.object(T, "jellyfin_for", lambda db: None):
            self.assertIn("skipped", T.sync(self.db))

    def test_old_tuners_wait_for_the_lineups_channels(self):
        self.jf.add_hdhr()
        self.jf.favs[ANN] = {self.hdhr_id("c1")}
        with mock.patch.object(T, "refresh_and_wait", lambda db, jf: True):   # Jellyfin made nothing yet
            done = self.sync()
        self.assertEqual(0, done["removed"])
        self.assertIn("hd", [t["Id"] for t in self.jf.tuners])
        done = self.sync()
        self.assertEqual(["m3u"], [t["Type"] for t in self.jf.tuners])
        self.assertEqual({self.ids()["c1"]}, self.jf.favourites(ANN))

    # ── Moving off v1.11-1.12's shared tuner and the per-person tuners ─────

    def on_per_person_tuners(self):
        """Jellyfin as v1.11-1.12 (or the own-tuner work) left it: the shared
        "All channels" tuner (Ann and the admin on it), Ben on a tuner of his
        own with one channel. Returns (shared ids by key, Ben's ids by key)."""
        for k, v in ((T.ALL_TUNER_URL_KEY, LINEUP), (T.TUNER_BASE_KEY, BASE)):
            mdb.set_setting(self.db, k, v)
        ben_url = BASE + L.user_tuner_path(BEN)
        self.db.add(mdb.LiveUserLineup(jellyfin_user_id=BEN, own_list=True, synced_own_list=True, tuner_url=ben_url))
        self.db.add(mdb.LiveUserChannel(jellyfin_user_id=BEN, channel_key="c1", position=0))
        self.db.commit()
        self.jf.tuners += [{"Id": "all", "Type": "m3u", "Url": LINEUP, "FriendlyName": "Tentacle · All channels",
                            "ReadAtNativeFramerate": True},
                           {"Id": "ben", "Type": "m3u", "Url": ben_url, "FriendlyName": "Tentacle · ben"}]
        self.jf.listings.append({"Type": "xmltv", "Path": BASE + "/api/live/xmltv.xml"})
        self.jf.refresh()
        for u in (ADMIN, ANN):
            self.jf.users[u]["Policy"]["BlockedTags"] = [L.user_tag(BEN)]
        self.jf.users[BEN]["Policy"]["BlockedTags"] = [L.ALL_TAG]
        self.assertEqual(4, len(self.jf.seen_by(ANN)))
        self.assertEqual(["News"], self.jf.seen_by(BEN))
        ben = {k: i for i, k in L.item_ids_for(ben_url, L.legacy_lineup(self.db, BEN)).items()}
        return self.ids(), ben

    def test_moving_onto_the_lineup_keeps_channels_favourites_and_schedules(self):
        shared, ben = self.on_per_person_tuners()
        self.jf.favs[ANN] = {shared["c3"]}
        self.jf.favs[ADMIN] = {shared["c1"], shared["c4"]}
        self.jf.favs[BEN] = {ben["c1"]}
        self.jf.series.append({"Id": "old", "Name": "Hockey Night", "ChannelId": ben["c1"], "KeepUpTo": 3})
        done = self.sync()
        # One tuner, the shared one's address: its favourites never moved
        self.assertEqual([("all", "Tentacle · All channels")], [(t["Id"], t["FriendlyName"]) for t in self.jf.tuners])
        for u in (ADMIN, ANN, BEN):
            self.assertEqual(4, len(self.jf.seen_by(u)))
            self.assertEqual([], self.jf.users[u]["Policy"]["BlockedTags"])
        self.assertEqual({shared["c3"]}, self.jf.favs[ANN])
        self.assertEqual({shared["c1"], shared["c4"]}, self.jf.favs[ADMIN])
        self.assertEqual({shared["c1"]}, self.jf.favs[BEN] & set(shared.values()))
        self.assertEqual([shared["c1"]], [s["ChannelId"] for s in self.jf.series])
        self.assertEqual(3, self.jf.series[0]["KeepUpTo"])
        self.assertEqual((1, 1, 1), (done["favourites"], done["schedules"], done["removed"]))
        # Saved first; Ben's tuner and its channels gone
        backup = self.first_backup()
        self.assertEqual({"all", "ben"}, {t["Id"] for t in backup["tuners"]})
        self.assertEqual(["c1"], backup["lists"][BEN]["keys"])
        self.assertFalse(any(c["tuner"] != LINEUP for c in self.jf.channels.values()))
        again = self.sync()
        self.assertEqual((0, 0, 0), (again["added"], again["removed"], again["favourites"]))

    def test_a_per_person_tuner_stays_while_a_schedule_cant_move(self):
        shared, ben = self.on_per_person_tuners()
        self.jf.series.append({"Id": "old", "Name": "Not in the guide", "ChannelId": ben["c1"]})
        self.sync()
        self.assertIn("ben", [t["Id"] for t in self.jf.tuners])
        # Ben already sees the lineup, not his tuner of before twice
        self.assertEqual(4, len(self.jf.seen_by(BEN)))
        self.assertEqual([L.user_tag(BEN)], self.jf.users[BEN]["Policy"]["BlockedTags"])

    # ── Keys that change (a channel folded into a network) ─────────────────

    def test_a_changed_key_carries_favourites_and_schedules(self):
        self.sync()
        # Ann's "Sport East" (c7) became c2: Jellyfin still has it under the
        # old stream address until its next guide refresh.
        old_id = L.m3u_channel_item_id(LINEUP, BASE + "/api/live/stream/7")
        self.jf.channels[old_id] = {"Id": old_id, "Name": "Sport East", "Tags": [L.ALL_TAG], "tuner": LINEUP}
        self.jf.favs[ANN].add(old_id)
        self.jf.series.append({"Id": "old", "Name": "Hockey Night", "ChannelId": old_id, "KeepUpTo": 2})
        with mock.patch.object(L, "aliases", lambda db: {"c7": "c2"}):
            done = self.sync()
        self.assertNotIn(old_id, self.jf.channels)
        self.assertEqual({self.ids()["c2"]}, self.jf.favourites(ANN))
        self.assertEqual([self.ids()["c2"]], [s["ChannelId"] for s in self.jf.series])
        self.assertEqual((1, 1), (done["favourites"], done["schedules"]))
        self.assertTrue(done["backup"])

    def test_a_changed_key_carries_a_one_off_recording_the_refresh_drops(self):
        self.sync()
        old_id = L.m3u_channel_item_id(LINEUP, BASE + "/api/live/stream/7")
        self.jf.channels[old_id] = {"Id": old_id, "Name": "Sport East", "Tags": [L.ALL_TAG], "tuner": LINEUP}
        self.jf.timers.append({"Id": "once", "Name": "Hockey Night", "ChannelId": old_id,
                               "StartDate": "2026-10-21T20:00:00Z", "PrePaddingSeconds": 60})
        with mock.patch.object(L, "aliases", lambda db: {"c7": "c2"}):
            done = self.sync()
        self.assertEqual([self.ids()["c2"]], [t["ChannelId"] for t in self.jf.timers])
        self.assertEqual(60, self.jf.timers[0]["PrePaddingSeconds"])
        self.assertEqual(1, done["schedules"])

    def test_nothing_to_carry_without_aliases(self):
        self.sync()
        calls = len(self.jf.calls)
        done = self.sync()
        self.assertFalse(done["refreshed"])
        self.assertFalse(done["backup"])
        self.assertFalse([c for c in self.jf.calls[calls:] if c[1] == "/LiveTv/Channels"])

    # ── Kids ────────────────────────────────────────────────────────────────

    def test_a_kid_is_blocked_before_the_refresh(self):
        from services import family
        self.sync()
        self.person(BEN, "ben", kids_level="young")
        mdb.set_setting(self.db, family.KIDS_CHANNELS_KEY, json.dumps(["c3"]))
        self.db.commit()
        seen = []
        with mock.patch.object(T, "refresh_and_wait", lambda db, jf: seen.append(self.jf.seen_by(BEN)) or jf.refresh()):
            self.sync(refresh=True)
        self.assertEqual([["Kids"]], seen)

    def test_kids_see_only_the_kids_channels(self):
        from services import family
        self.person(BEN, "ben", kids_level="young")
        mdb.set_setting(self.db, family.KIDS_CHANNELS_KEY, json.dumps(["c3"]))
        self.db.commit()
        self.sync()
        self.assertEqual(["Kids"], self.jf.seen_by(BEN))
        self.assertEqual([L.ALL_TAG], self.jf.users[BEN]["Policy"]["BlockedTags"])
        self.assertEqual(4, len(self.jf.seen_by(ANN)))
        self.assertEqual([L.KIDS_TAG], self.jf.channels[self.ids()["c3"]]["Tags"])
        # A channel made a kids channel, another taken off
        mdb.set_setting(self.db, family.KIDS_CHANNELS_KEY, json.dumps(["c1"]))
        self.db.commit()
        self.sync(refresh=True)
        self.assertEqual(["News"], self.jf.seen_by(BEN))
        # ...and the refresh gave their programmes the new tags
        self.assertEqual([L.KIDS_TAG], self.jf.programme_tags[self.ids()["c1"]])
        self.assertEqual([L.ALL_TAG], self.jf.programme_tags[self.ids()["c3"]])
        # A channel new to Jellyfin is no kids channel until an admin says so
        self.db.add(mdb.LiveChannel(provider_id=1, name="Late Movie", stream_id="200",
                                    stream_url="http://192.0.2.10/live/20.ts", enabled=True, sort_order=9))
        self.db.commit()
        self.jf.refresh()
        self.assertEqual(["News"], self.jf.seen_by(BEN))
        self.assertIn("Late Movie", self.jf.seen_by(ANN))


class GuideWait(unittest.TestCase):
    def test_a_cancelled_or_earlier_run_is_not_done(self):
        from datetime import datetime
        since = datetime(2026, 10, 20, 18, 30, 0)
        task = lambda status, end: {"LastExecutionResult": {"Status": status, "EndTimeUtc": end}}
        self.assertEqual("Completed", T._finished_after(task("Completed", "2026-10-20T18:30:05.123Z"), since))
        self.assertIsNone(T._finished_after(task("Completed", "2026-10-20T18:29:59.9Z"), since))
        self.assertEqual("Cancelled", T._finished_after(task("Cancelled", "2026-10-20T18:31:00Z"), since))
        self.assertIsNone(T._finished_after({}, since))


class MyChannelsRoutes(DbCase):
    def setUp(self):
        super().setUp()
        from routers import my_livetv
        from routers.auth import get_user_from_request
        self.ann = mdb.TentacleUser(jellyfin_user_id=ANN, display_name="ann")
        self.db.add(self.ann)
        self.db.commit()
        mdb.set_setting(self.db, T.ALL_TUNER_URL_KEY, LINEUP)
        self.db.commit()
        app = FastAPI()
        app.include_router(my_livetv.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        self.client = TestClient(app)
        self.jf = FakeJellyfin(self.Session, [(ADMIN, "admin"), (ANN, "ann"), (BEN, "ben")])
        self.jf.tuners.append({"Id": "all", "Type": "m3u", "Url": LINEUP})
        self.jf.refresh()
        self.ids = {k: i for i, k in L.item_ids_for(LINEUP, L.lineup(self.db)).items()}
        pt = mock.patch.object(T, "jellyfin_for", lambda db: self.jf)
        pt.start()
        self.addCleanup(pt.stop)

    def test_the_lineup_with_my_stars(self):
        self.jf.favs[ANN] = {self.ids["c3"]}
        self.jf.favs[BEN] = {self.ids["c1"]}
        d = self.client.get("/api/my/channels").json()
        self.assertEqual(["c1", "c2", "c3", "c4"], [c["key"] for c in d["channels"]])
        self.assertEqual([1, 2, 3, 4], [c["number"] for c in d["channels"]])
        self.assertEqual(["c3"], [c["key"] for c in d["mine"]])
        self.assertEqual((4, 1, True), (d["total"], d["count"], d["favourites_known"]))
        self.assertNotIn("my_list", d)

    def test_a_quick_pick_stars_and_unstars_several(self):
        d = self.client.put("/api/my/channels/favourites", json={"keys": ["c4", "c2", "c9"], "favourite": True}).json()
        self.assertEqual(["c2", "c4"], [c["key"] for c in d["mine"]])
        self.assertEqual(2, d["changed"])
        self.assertEqual({self.ids["c2"], self.ids["c4"]}, self.jf.favs[ANN])
        self.assertEqual(set(), self.jf.favs[BEN])     # only for the person signed in
        d = self.client.put("/api/my/channels/favourites", json={"keys": ["c4"], "favourite": False}).json()
        self.assertEqual(["c2"], [c["key"] for c in d["mine"]])
        self.assertEqual(404, self.client.put("/api/my/channels/favourites",
                                              json={"keys": ["c9"], "favourite": True}).status_code)

    def test_one_star(self):
        self.assertEqual(200, self.client.put("/api/my/channels/favourite",
                                              json={"key": "c1", "favourite": True}).status_code)
        self.assertEqual({self.ids["c1"]}, self.jf.favs[ANN])

    def test_a_kids_profile_sees_and_stars_only_kids_channels(self):
        self.ann.kids_level = "young"
        self.db.commit()
        mdb.set_setting(self.db, "livetv_kids_channels", json.dumps(["c3", "c1"]))
        self.db.commit()
        d = self.client.get("/api/my/channels").json()
        self.assertEqual(["c1", "c3"], [c["key"] for c in d["channels"]])
        self.assertEqual(2, d["total"])
        self.assertEqual(404, self.client.put("/api/my/channels/favourite",
                                              json={"key": "c2", "favourite": True}).status_code)

    def test_admins_see_each_persons_favourites(self):
        from routers.auth import require_admin
        self.client.app.dependency_overrides[require_admin] = lambda: self.ann
        self.db.add(mdb.TentacleUser(jellyfin_user_id=BEN, display_name="ben"))
        self.db.commit()
        self.jf.favs[ANN] = {self.ids["c2"], self.ids["c3"]}
        d = self.client.get("/api/live/people").json()
        self.assertEqual({"ann": 2, "ben": 0}, {p["name"]: p["favourites"] for p in d["people"]})
        self.assertEqual({"ann": 4, "ben": 4}, {p["name"]: p["channels"] for p in d["people"]})
        self.assertEqual(4, d["total"])

    def test_before_jellyfin_is_set_up(self):
        with mock.patch.object(T, "jellyfin_for", lambda db: None):
            d = self.client.get("/api/my/channels").json()
            self.assertFalse(d["favourites_known"])
            self.assertTrue(d["setting_up"])
            r = self.client.put("/api/my/channels/favourite", json={"key": "c1", "favourite": True})
            self.assertEqual(409, r.status_code)
