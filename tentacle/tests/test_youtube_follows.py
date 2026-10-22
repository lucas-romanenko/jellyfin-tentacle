"""YouTube channels per person (services/youtube/follows.py, routers/my_youtube.py).

Everyone adds their own channels; only a channel's followers see its videos
(tag yt:<slug>) and its Live TV channel (tentacle-youtube-<id>), through
Jellyfin's BlockedTags. One copy per channel however many follow it; the
last follower leaving removes it. Existing channels became the admin's only.

Run from the tentacle/ directory:  python -m unittest tests.test_youtube_follows
"""
import sqlite3
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
from routers import my_youtube, youtube as Y
from routers.auth import get_user_from_request
from services import livetv_lineups as L, livetv_tuners as T
from services.youtube import follows
from tests.test_family import ADMIN, ANN, BEN, Case
from tmp_dirs import temp_dir


def _channel(db, title, slug, live=False, **kw):
    ch = mdb.YouTubeChannel(input_url=f"https://www.youtube.com/@{slug}", kind="channel",
                            channel_id="UC" + slug.ljust(22, "x")[:22], title=title, slug=slug,
                            enabled=True, live_enabled=live, extra_tags=[], **kw)
    db.add(ch)
    db.commit()
    return ch


class Follows(Case):
    def setUp(self):
        super().setUp()
        self.trader = _channel(self.db, "TraderTV Live", "tradertv-live", live=True)
        self.cooking = _channel(self.db, "Cooking", "cooking")
        follows.follow(self.db, self.trader, ADMIN)
        follows.follow(self.db, self.cooking, ADMIN)

    def blocked(self, uid):
        return self.jf.users[uid]["Policy"]["BlockedTags"]

    # ── Who sees what ──

    def test_someone_who_follows_nothing_blocks_every_channel_and_keeps_their_own_tags(self):
        self.assertTrue(follows.apply_person(self.db, ANN))
        self.assertEqual(["Horror", L.ALL_TAG, "tentacle-youtube-%d" % self.trader.id,
                          "tentacle-youtube-%d" % self.cooking.id, "yt:cooking", "yt:tradertv-live"],
                         self.blocked(ANN))
        self.assertFalse(follows.apply_person(self.db, ANN))   # nothing to change the second time

    def test_a_follower_sees_their_channel_and_not_the_others(self):
        follows.follow(self.db, self.cooking, ANN)
        follows.apply_person(self.db, ANN)
        self.assertIn("yt:tradertv-live", self.blocked(ANN))
        self.assertNotIn("yt:cooking", self.blocked(ANN))
        self.assertNotIn(f"tentacle-youtube-{self.cooking.id}", self.blocked(ANN))
        follows.apply_person(self.db, ADMIN)
        self.assertFalse([t for t in self.blocked(ADMIN) if follows.is_ours(t)])

    def test_everyone_in_jellyfin_gets_theirs_also_people_tentacle_never_saw(self):
        self.jf.users["e" * 32] = {"Id": "e" * 32, "Name": "Eve", "Policy": {"BlockedTags": []}}
        self.assertEqual(3, follows.apply_everyone(self.db))     # Ann, Ben, Eve; the admin follows both
        self.assertIn("yt:cooking", self.blocked("e" * 32))
        self.assertEqual(0, follows.apply_everyone(self.db))

    def test_the_live_tv_sync_writes_the_same_list_so_the_two_never_undo_each_other(self):
        ann = lambda: [u for u in self.jf.get("/Users") if u["Id"] == ANN]   # noqa: E731
        self.assertEqual(1, T.apply_policies(self.db, self.jf, ann(), {"tentacle-livetv-" + BEN}))
        before = list(self.blocked(ANN))
        self.assertIn("yt:cooking", before)
        self.assertFalse(follows.apply_person(self.db, ANN))
        self.assertEqual(0, T.apply_policies(self.db, self.jf, ann(), {"tentacle-livetv-" + BEN}))
        self.assertEqual(before, self.blocked(ANN))
        # ...and a change in follows reaches the policy through the sync too
        follows.follow(self.db, self.cooking, ANN)
        self.assertEqual(1, T.apply_policies(self.db, self.jf, ann(), {"tentacle-livetv-" + BEN}))
        self.assertNotIn("yt:cooking", self.blocked(ANN))
        self.assertFalse(follows.apply_person(self.db, ANN))

    def test_a_youtube_live_channel_carries_its_tag_in_jellyfin(self):
        url = "http://tentacle.lan:8888/api/live/tuner/all.m3u"
        entries = [{"key": f"y{self.trader.id}", "stream_path": f"/api/youtube/live/{self.trader.id}/stream.ts"}]
        item_id = next(iter(L.item_ids_for(url, entries)))
        posted = []
        jf = mock.Mock()
        jf.get.return_value = {"Items": [{"Id": item_id, "Name": "TraderTV Live", "Tags": [L.ALL_TAG]}]}
        jf.post.side_effect = lambda path, body: posted.append(body)
        T.tag_lineup(jf, ADMIN, url, entries, kids=set())
        self.assertEqual([L.ALL_TAG, f"tentacle-youtube-{self.trader.id}"], posted[0]["Tags"])

    def test_a_channel_a_kid_follows_is_a_kids_channel_for_the_lineup(self):
        self.ben.kids_level = "kids"
        self.db.commit()
        self.assertNotIn(f"y{self.trader.id}", T._kids_keys(self.db))
        follows.follow(self.db, self.trader, BEN, added_by="Lucas")
        self.assertIn(f"y{self.trader.id}", T._kids_keys(self.db))

    # ── My Channels ──

    def test_my_channels_lists_a_youtube_channel_only_for_its_followers(self):
        from routers.my_livetv import my_lineup
        key = f"y{self.trader.id}"
        self.assertIn(key, [c["key"] for c in my_lineup(self.db, self.admin)])
        self.assertNotIn(key, [c["key"] for c in my_lineup(self.db, self.ann)])
        follows.follow(self.db, self.trader, ANN)
        self.assertIn(key, [c["key"] for c in my_lineup(self.db, self.ann)])

    def test_a_kid_sees_the_youtube_channels_added_for_them_besides_the_kids_channels(self):
        from routers.my_livetv import my_lineup
        self.ben.kids_level = "kids"
        self.db.commit()
        self.assertEqual([], my_lineup(self.db, self.ben))
        follows.follow(self.db, self.trader, BEN, added_by="Lucas")
        self.assertEqual([f"y{self.trader.id}"], [c["key"] for c in my_lineup(self.db, self.ben)])


class MyYouTube(Case):
    """The page everyone has: paste a link, remove one."""

    def setUp(self):
        super().setUp()
        self.trader = _channel(self.db, "TraderTV Live", "tradertv-live")
        follows.follow(self.db, self.trader, ADMIN)
        for target in ("_start_refresh", "_publish_for", "_cleanup_for_people"):
            p = mock.patch.object(Y, target)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(Y.client, "available", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.resolved = {"kind": "channel", "channel_id": "UC" + "tradertv-live".ljust(22, "x")[:22],
                         "title": "TraderTV Live"}
        p = mock.patch.object(Y.indexer, "resolve_channel", side_effect=lambda url: dict(self.resolved))
        p.start()
        self.addCleanup(p.stop)

    def client(self, user):
        app = FastAPI()
        app.include_router(my_youtube.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        return TestClient(app)

    def test_adding_a_channel_someone_else_has_follows_it_without_a_second_copy(self):
        r = self.client(self.ann).post("/api/my/youtube", json={"url": "https://youtube.com/@tradertv"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["added"]["new"])
        self.assertEqual(1, self.db.query(mdb.YouTubeChannel).count())
        self.assertEqual({ADMIN, ANN}, follows.follower_ids(self.db, self.trader.id))
        self.assertEqual(["TraderTV Live"], [c["title"] for c in r.json()["channels"]])
        self.assertNotIn("yt:tradertv-live", self.jf.users[ANN]["Policy"]["BlockedTags"])
        Y._start_refresh.assert_not_called()      # nothing fetched again
        Y._publish_for.assert_called_once_with([ANN])
        # twice is said, not doubled
        r = self.client(self.ann).post("/api/my/youtube", json={"url": "https://youtube.com/@tradertv"})
        self.assertEqual(409, r.status_code)

    def test_a_new_channel_is_added_followed_and_fetched(self):
        self.resolved = {"kind": "channel", "channel_id": "UC" + "n" * 22, "title": "Numberphile"}
        r = self.client(self.ann).post("/api/my/youtube", json={"url": "https://youtube.com/@numberphile"})
        self.assertEqual(200, r.status_code, r.text)
        ch = self.db.query(mdb.YouTubeChannel).filter_by(title="Numberphile").one()
        self.assertTrue(r.json()["added"]["new"])
        self.assertEqual({ANN}, follows.follower_ids(self.db, ch.id))
        Y._start_refresh.assert_called_once()
        self.assertEqual([ch.id], Y._start_refresh.call_args.kwargs["channel_ids"])
        self.assertEqual(10, ch.keep_count)

    def test_leaving_a_shared_channel_only_takes_it_away_from_you(self):
        follows.follow(self.db, self.trader, ANN)
        r = self.client(self.ann).delete(f"/api/my/youtube/{self.trader.id}")
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["removed"]["gone"])
        self.assertEqual({ADMIN}, follows.follower_ids(self.db, self.trader.id))
        self.assertIn("yt:tradertv-live", self.jf.users[ANN]["Policy"]["BlockedTags"])
        self.assertEqual(1, self.db.query(mdb.YouTubeChannel).count())
        import threading
        for t in threading.enumerate():   # their row and playlist go in the background
            if t.name == "youtube-unfollow-cleanup":
                t.join(5)
        Y._cleanup_for_people.assert_called_once_with("TraderTV Live", "tradertv-live", [self.ann.id])

    def test_the_last_follower_leaving_removes_the_channel_and_its_files(self):
        with mock.patch.object(Y, "remove_channel", return_value={"success": True}) as remove:
            r = self.client(self.admin).delete(f"/api/my/youtube/{self.trader.id}")
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["removed"]["gone"])
        remove.assert_called_once()

    def test_removal_deletes_its_follows_too(self):
        follows.follow(self.db, self.trader, ANN)
        Y.remove_channel(self.db, self.trader)
        self.assertEqual(0, self.db.query(mdb.YouTubeFollow).count())
        self.assertEqual(0, self.db.query(mdb.YouTubeChannel).count())

    def test_someone_elses_channel_cant_be_removed(self):
        r = self.client(self.ann).delete(f"/api/my/youtube/{self.trader.id}")
        self.assertEqual(404, r.status_code)
        self.assertEqual({ADMIN}, follows.follower_ids(self.db, self.trader.id))

    def test_a_kid_sees_theirs_and_cant_change_them(self):
        self.ben.kids_level = "kids"
        self.db.commit()
        follows.follow(self.db, self.trader, BEN, added_by="Lucas")
        c = self.client(self.ben)
        d = c.get("/api/my/youtube").json()
        self.assertFalse(d["can_add"])
        self.assertEqual([("TraderTV Live", "Lucas")], [(x["title"], x["added_by"]) for x in d["channels"]])
        self.assertEqual(403, c.post("/api/my/youtube", json={"url": "https://youtube.com/@x"}).status_code)
        self.assertEqual(403, c.delete(f"/api/my/youtube/{self.trader.id}").status_code)

    def test_an_admin_gives_a_kid_a_channel_and_takes_it_away(self):
        app = FastAPI()
        app.include_router(Y.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.admin
        from routers.auth import require_admin
        app.dependency_overrides[require_admin] = lambda: self.admin
        c = TestClient(app)
        r = c.put(f"/api/youtube/channels/{self.trader.id}/followers", json={"people": [ADMIN, BEN]})
        self.assertEqual(200, r.status_code, r.text)
        row = self.db.query(mdb.YouTubeFollow).filter_by(jellyfin_user_id=BEN).one()
        self.assertEqual("Lucas", row.added_by)
        listed = next(x for x in c.get("/api/youtube/channels").json() if x["id"] == self.trader.id)
        self.assertEqual(["Ben", "Lucas"], [f["name"] for f in listed["followers"]])
        r = c.put(f"/api/youtube/channels/{self.trader.id}/followers", json={"people": [ADMIN]})
        self.assertEqual({ADMIN}, follows.follower_ids(self.db, self.trader.id))
        self.assertEqual(400, c.put(f"/api/youtube/channels/{self.trader.id}/followers",
                                    json={"people": ["f" * 32]}).status_code)


class Forgotten(Case):
    def test_a_person_jellyfin_no_longer_has_takes_their_follows_and_lone_channels_with_them(self):
        from services import gone_people
        shared = _channel(self.db, "Shared", "shared")
        alone = _channel(self.db, "Ann's own", "anns-own")
        follows.follow(self.db, shared, ADMIN)
        follows.follow(self.db, shared, ANN)
        follows.follow(self.db, alone, ANN)
        with mock.patch.object(Y, "_cleanup_after_remove"):
            del self.jf.users[ANN]
            gone_people.forget_gone(self.db, self.jf.get("/Users"))
        self.assertEqual(0, self.db.query(mdb.YouTubeFollow).filter_by(jellyfin_user_id=ANN).count())
        self.assertEqual(["Shared"], [c.title for c in self.db.query(mdb.YouTubeChannel).all()])


class Migration(unittest.TestCase):
    """Every channel there was becomes the admin's only, once."""

    def setUp(self):
        self.path = f"{temp_dir(self)}/t.db"
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME);
            CREATE TABLE tentacle_users (id INTEGER PRIMARY KEY, jellyfin_user_id TEXT, display_name TEXT, is_admin BOOLEAN);
            CREATE TABLE youtube_channels (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE youtube_follows (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_fk INTEGER NOT NULL,
                jellyfin_user_id TEXT NOT NULL, added_by TEXT, created_at DATETIME,
                UNIQUE (channel_fk, jellyfin_user_id));
            INSERT INTO youtube_channels VALUES (1, 'TraderTV Live'), (2, 'Cooking');
        """)
        conn.commit()
        self.conn = conn
        self.addCleanup(conn.close)

    def follows(self):
        return sorted(self.conn.execute("SELECT channel_fk, jellyfin_user_id FROM youtube_follows").fetchall())

    def test_the_owner_admin_follows_everything_and_nobody_else(self):
        self.conn.executescript(f"""
            INSERT INTO tentacle_users VALUES (1, '{ANN}', 'Ann', 0), (2, '{BEN}', 'Ben', 1), (3, '{ADMIN}', 'Lucas', 1);
            INSERT INTO settings (key, value) VALUES ('jellyfin_user_id', '{ADMIN[:8]}-{ADMIN[8:12]}-{ADMIN[12:16]}-{ADMIN[16:20]}-{ADMIN[20:]}');
        """)
        mdb._migrate_youtube_follows(self.conn.cursor(), self.conn)
        self.assertEqual([(1, ADMIN), (2, ADMIN)], self.follows())
        # once: a channel later followed by nobody else stays that way
        self.conn.execute("DELETE FROM youtube_follows WHERE channel_fk = 2")
        mdb._migrate_youtube_follows(self.conn.cursor(), self.conn)
        self.assertEqual([(1, ADMIN)], self.follows())

    def test_without_an_admin_yet_it_waits(self):
        mdb._migrate_youtube_follows(self.conn.cursor(), self.conn)
        self.assertEqual([], self.follows())
        self.assertIsNone(self.conn.execute(
            "SELECT value FROM settings WHERE key = 'youtube_follows_migrated'").fetchone())
        self.conn.execute(f"INSERT INTO tentacle_users VALUES (1, '{BEN}', 'Ben', 1)")
        mdb._migrate_youtube_follows(self.conn.cursor(), self.conn)
        self.assertEqual([(1, BEN), (2, BEN)], self.follows())


class KidsAndStudios(Case):
    def setUp(self):
        super().setUp()
        self.trader = _channel(self.db, "TraderTV Live", "tradertv-live")

    def test_a_channel_added_for_a_kid_is_rated_so_they_can_watch_it(self):
        jf = mock.Mock()
        jf.get.return_value = {"Items": [
            {"Id": "v1", "Name": "Video 1", "Tags": ["youtube", "yt:tradertv-live"], "DateCreated": "2026-10-21T00:00:00Z"},
            {"Id": "v2", "Name": "Video 2", "Tags": ["youtube"], "OfficialRating": "G"}]}
        with mock.patch("services.youtube.sync.base_url", return_value="http://t"):
            self.assertEqual(1, follows.rate_for_kids(self.db, self.trader, jf))
        self.assertEqual("G", self.trader.rating)
        path, body = jf.post.call_args.args
        self.assertEqual("/Items/v1", path)
        self.assertEqual(("G", ["youtube", "yt:tradertv-live"], "2026-10-21T00:00:00Z"),
                         (body["OfficialRating"], body["Tags"], body["DateCreated"]))
        # a rating an admin chose is theirs
        self.trader.rating = "PG-13"
        self.assertEqual(0, follows.rate_for_kids(self.db, self.trader, jf))
        self.assertEqual("PG-13", self.trader.rating)

    def test_the_channels_studio_is_tagged_so_search_hides_it_too(self):
        jf = mock.Mock()
        def get(path, **p):
            if path == "/Studios":
                return {"Items": [{"Id": "s1", "Name": "TraderTV Live"}, {"Id": "s2", "Name": "TraderTV Live 2"}]}
            return {"Items": [{"Id": p["ids"], "Name": "TraderTV Live", "Tags": []}]}
        jf.get.side_effect = get
        self.assertEqual(1, follows.tag_studios(self.db, jf))
        path, body = jf.post.call_args.args
        self.assertEqual(("/Items/s1", ["yt:tradertv-live"]), (path, body["Tags"]))


if __name__ == "__main__":
    unittest.main()
