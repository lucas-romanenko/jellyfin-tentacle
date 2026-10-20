"""Live TV per person: everyone's own channels, set up in Jellyfin by Tentacle.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_per_person

What was proven on a throwaway Jellyfin 10.11.8 (docs/agents/jellyfin-notes.md,
"Live TV per person") and is pinned here:
  - Jellyfin's ids for M3U channels (MD5 of tuner URL + stream URL, UTF-16,
    as a .NET Guid) and for the channel items: Tentacle computes them itself.
    The expected values are ones Jellyfin 10.11.8 gave.
  - A person's own list: their order is their numbers; unknown or repeated
    channels are dropped; channels the household no longer has drop out.
  - The sync against a fake Jellyfin that behaves like the real one where it
    mattered in the end-to-end runs: blocked tags hide channels from a
    person; a favourite on a channel the person can't see is refused (404);
    a guide refresh creates the channels of every tuner and drops those of
    removed tuners.
"""
import json
import logging
import os
import shutil
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
    def test_own_tags_kept_and_every_other_tuner_blocked(self):
        tags = {L.ALL_TAG, L.user_tag(ANN), L.user_tag(BEN)}
        self.assertEqual(["Horror", L.ALL_TAG, L.user_tag(BEN)],
                         T.blocked_tags(["Horror", L.user_tag("d" * 32)], L.user_tag(ANN), tags))
        self.assertEqual([L.user_tag(ANN), L.user_tag(BEN)], T.blocked_tags([], L.ALL_TAG, tags))


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

    def test_own_list_order_is_the_numbers(self):
        L.save_own_list(self.db, "b" * 8 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 12,
                        True, ["c3", "c1", "c5", "nope", "c3"])
        self.assertEqual([("c3", 1), ("c1", 2)], [(c["key"], c["number"]) for c in L.lineup_for(self.db, BEN.replace("c", "b"))])

    def test_pool_keeps_the_old_tuners_order(self):
        # Jellyfin listed the old tuner's channels by number: 9 before 10,
        # whatever their names, unless the admin set an order.
        self.db.query(mdb.LiveChannel).update({"sort_order": 0})
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 1).update({"stream_id": "1000"})
        self.db.commit()
        self.assertEqual(["c2", "c3", "c4", "c1"], [c["key"] for c in L.pool(self.db)])

    def test_without_a_list_everything(self):
        self.assertEqual([1, 2, 3, 4], [c["number"] for c in L.lineup_for(self.db, ANN)])
        L.save_own_list(self.db, ANN, True, ["c2"])
        L.save_own_list(self.db, ANN, False, [])
        self.assertEqual(4, len(L.lineup_for(self.db, ANN)))
        self.assertEqual(["c2"], L.own_keys(self.db, ANN))   # kept for switching back

    def test_a_channel_switched_off_leaves_every_list(self):
        L.save_own_list(self.db, ANN, True, ["c2", "c1"])
        self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.id == 2).update({"enabled": False})
        self.db.commit()
        self.assertEqual([("c1", 1)], [(c["key"], c["number"]) for c in L.lineup_for(self.db, ANN)])

    def test_m3u(self):
        text = L.m3u(L.lineup_for(self.db, None), BASE)
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

    def test_a_persons_tuner(self):
        L.save_own_list(self.db, ANN, True, ["c2"])
        row = L.lineup_row(self.db, ANN)
        row.tuner_url = "http://x:1" + L.user_tuner_path(ANN)
        self.db.commit()
        r = self.client.get(f"/api/live/users/{ANN}/tuner.m3u")
        self.assertEqual(['#EXTINF:-1 tvg-id="102" tvg-chno="1" tvg-logo="http://img/2.png",Sport',
                          "http://x:1/api/live/stream/2"], r.text.splitlines()[1:])
        self.assertEqual(404, self.client.get("/api/live/users/..%2Fetc/tuner.m3u").status_code)
        L.save_own_list(self.db, ANN, False, [])      # switching back: still only their list
        self.assertEqual(3, len(self.client.get(f"/api/live/users/{ANN}/tuner.m3u").text.splitlines()))
        self.assertEqual("#EXTM3U\n", self.client.get(f"/api/live/users/{BEN}/tuner.m3u").text)


# ── A fake Jellyfin ─────────────────────────────────────────────────────────

class FakeJellyfin:
    """Jellyfin's Live TV as far as the sync uses it. Channels appear and go
    only on refresh(); people see only channels whose tags they don't block."""

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
                entries = L.own_lineup(db, m.group(1), pool) if m else L.lineup_for(db, None, pool)
                for i in L.item_ids_for(t["Url"], entries):
                    want[i] = (next(e["name"] for e in entries if L.item_ids_for(t["Url"], [e]).get(i)), t["Url"])
        finally:
            db.close()
        for i in list(self.channels):
            if i not in want:
                del self.channels[i]
        for i, (name, tuner) in want.items():
            self.channels.setdefault(i, {"Id": i, "Name": name, "Tags": [], "tuner": tuner})

    # the Jellyfin wrapper's interface
    def get(self, path, **p):
        self.calls.append(("GET", path, p))
        if path == "/System/Configuration/livetv":
            return {"TunerHosts": [dict(t) for t in self.tuners], "ListingProviders": list(self.listings)}
        if path == "/Users":
            return [json.loads(json.dumps(u)) for u in self.users.values()]
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

    def sync(self):
        return T.sync(self.db)

    def hdhr_id(self, key):
        c = next(c for c in L.pool(self.db) if c["key"] == key)
        return L.channel_item_id(L.hdhr_external_id(c["xmltv_id"]))

    def test_moves_everyone_off_the_old_hdhomerun_tuner(self):
        self.jf.add_hdhr()
        self.jf.favs[ANN] = {self.hdhr_id("c2")}
        self.jf.series.append({"Id": "old", "Name": "Hockey Night", "ChannelId": self.hdhr_id("c2"),
                               "RecordAnyTime": True, "KeepUpTo": 3})
        done = self.sync()
        self.assertEqual(["m3u"], [t["Type"] for t in self.jf.tuners])
        all_url = BASE + L.ALL_TUNER_PATH
        self.assertEqual(all_url, self.jf.tuners[0]["Url"])
        self.assertTrue(self.jf.tuners[0]["ReadAtNativeFramerate"])   # the old tuner's settings
        mine = {k: i for i, k in L.item_ids_for(all_url, L.pool(self.db)).items()}
        self.assertIn(mine["c2"], self.jf.favs[ANN])
        self.assertEqual([mine["c2"]], [s["ChannelId"] for s in self.jf.series])
        self.assertEqual(3, self.jf.series[0]["KeepUpTo"])
        self.assertEqual(1, done["favourites"])
        self.assertEqual(1, done["schedules"])
        self.assertTrue(os.listdir(os.path.join(self.tmp, "backups")))
        backup = json.load(open(os.path.join(self.tmp, "backups", os.listdir(os.path.join(self.tmp, "backups"))[0])))
        self.assertEqual("hdhomerun", backup["tuners"][0]["Type"])
        self.assertEqual(1, len([u for u in backup["users"] if u["favourite_channels"]]))
        # The TV guide was there already: never a second one
        self.assertEqual(4, len(self.jf.seen_by(BEN)))
        self.assertTrue(all(c["Tags"] == [L.ALL_TAG] for c in self.jf.channels.values()))

    def test_adds_the_guide_when_jellyfin_has_none(self):
        self.sync()
        self.assertEqual([BASE + "/api/live/xmltv.xml"], [lp["Path"] for lp in self.jf.listings])
        self.sync()
        self.assertEqual(1, len(self.jf.listings))

    def test_own_list_is_seen_only_by_its_owner_and_keeps_favourites(self):
        self.sync()
        all_ids = {k: i for i, k in L.item_ids_for(BASE + L.ALL_TUNER_PATH, L.pool(self.db)).items()}
        self.jf.favs[ANN] = {all_ids["c3"]}
        L.save_own_list(self.db, ANN, True, ["c3", "c1"])
        self.sync()
        self.assertEqual(["Kids", "News"], self.jf.seen_by(ANN))
        self.assertEqual(4, len(self.jf.seen_by(BEN)))
        self.assertEqual(4, len(self.jf.seen_by(ADMIN)))
        own = {k: i for i, k in L.item_ids_for(BASE + L.user_tuner_path(ANN), L.lineup_for(self.db, ANN)).items()}
        self.assertIn(own["c3"], self.jf.favs[ANN])
        self.assertEqual(["Tentacle · All channels", "Tentacle · ann"], [t["FriendlyName"] for t in self.jf.tuners])
        # Ben (on All channels) blocks Ann's tuner, Ann blocks the shared one
        self.assertEqual([L.user_tag(ANN)], self.jf.users[BEN]["Policy"]["BlockedTags"])
        self.assertEqual([L.ALL_TAG], self.jf.users[ANN]["Policy"]["BlockedTags"])

    def test_back_to_all_channels_removes_the_tuner_and_never_shows_it_to_others(self):
        self.sync()
        L.save_own_list(self.db, ANN, True, ["c2"])
        self.sync()
        own_id = next(iter(L.item_ids_for(BASE + L.user_tuner_path(ANN), L.lineup_for(self.db, ANN))))
        self.jf.favs[ANN].add(own_id)
        L.save_own_list(self.db, ANN, False, [])
        refresh = T.refresh_and_wait
        seen = []

        def watch(db, jf):
            # what Ben sees before the refresh drops Ann's old channels
            seen.append(self.jf.seen_by(BEN))
            return refresh(db, jf)
        with mock.patch.object(T, "refresh_and_wait", watch):
            self.sync()
        self.assertEqual(["Tentacle · All channels"], [t["FriendlyName"] for t in self.jf.tuners])
        self.assertTrue(all(len(s) == 4 for s in seen), seen)
        self.assertEqual(4, len(self.jf.seen_by(ANN)))
        all_sport = {k: i for i, k in L.item_ids_for(BASE + L.ALL_TUNER_PATH, L.pool(self.db)).items()}["c2"]
        self.assertIn(all_sport, self.jf.favs[ANN])

    def test_a_person_with_their_own_blocked_tags_keeps_them(self):
        self.jf.users[BEN]["Policy"]["BlockedTags"] = ["Horror"]
        L.save_own_list(self.db, ANN, True, ["c1"])
        self.sync()
        self.sync()
        self.assertEqual(["Horror", L.user_tag(ANN)], self.jf.users[BEN]["Policy"]["BlockedTags"])

    def test_people_gone_from_jellyfin_lose_their_list(self):
        L.save_own_list(self.db, "d" * 32, True, ["c1"])
        self.sync()
        self.assertIsNone(L.lineup_row(self.db, "d" * 32))

    def test_tuner_address_is_kept_once_chosen(self):
        self.jf.add_hdhr()
        self.sync()
        self.assertEqual(BASE, mdb.get_setting(self.db, T.TUNER_BASE_KEY))
        mdb.set_setting(self.db, "youtube_base_url", "https://public.example")
        self.db.commit()
        L.save_own_list(self.db, BEN, True, ["c1"])
        self.sync()
        self.assertEqual(BASE + L.user_tuner_path(BEN), L.lineup_row(self.db, BEN).tuner_url)

    def test_not_connected_does_nothing(self):
        with mock.patch.object(T, "jellyfin_for", lambda db: None):
            self.assertIn("skipped", T.sync(self.db))

    def test_favourites_wait_for_the_new_channels(self):
        self.sync()
        L.save_own_list(self.db, ANN, True, ["c1"])
        with mock.patch.object(T, "refresh_and_wait", lambda db, jf: True):   # Jellyfin made nothing yet
            self.sync()
        self.assertFalse(L.lineup_row(self.db, ANN).synced_own_list)
        self.assertEqual(4, len(self.jf.seen_by(ANN)))   # still on All channels meanwhile
        self.sync()
        self.assertTrue(L.lineup_row(self.db, ANN).synced_own_list)
        self.assertEqual(["News"], self.jf.seen_by(ANN))


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
        app = FastAPI()
        app.include_router(my_livetv.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        self.client = TestClient(app)
        self.synced = []
        pt = mock.patch.object(T, "schedule_sync", lambda *a, **k: self.synced.append(a))
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch.object(T, "jellyfin_for", lambda db: None)
        pt.start()
        self.addCleanup(pt.stop)

    def test_everyone_starts_on_all_channels(self):
        d = self.client.get("/api/my/channels").json()
        self.assertFalse(d["own_list"])
        self.assertEqual(4, d["count"])
        self.assertEqual([1, 2, 3, 4], [c["number"] for c in d["channels"]])
        self.assertFalse(d["favourites_known"])

    def test_saving_my_list_sets_it_up_in_jellyfin(self):
        d = self.client.put("/api/my/channels", json={"own_list": True, "keys": ["c4", "c2"]}).json()
        self.assertEqual(["c4", "c2"], d["my_list"])
        self.assertEqual({"c4": 1, "c2": 2}, {c["key"]: c["number"] for c in d["channels"] if c["in_list"]})
        self.assertEqual(1, len(self.synced))
        # only for the person signed in
        self.assertEqual([], L.own_keys(self.db, BEN))

    def test_a_star_before_jellyfin_has_the_channel(self):
        r = self.client.put("/api/my/channels/favourite", json={"key": "c1", "favourite": True})
        self.assertEqual(409, r.status_code)
