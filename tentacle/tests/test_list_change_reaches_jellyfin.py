"""A list's Fetch, Refresh all and delete reach Jellyfin and the playlists at once (#679).

Run from the tentacle/ directory:  python -m unittest discover -s tests

apply_list_tags_to_library() only rewrote the rows and the NFOs, but Jellyfin
keeps a tag set through its API (every download's, and the tag push's on
every title), so:
- a title that left a list stayed in the list's playlist and home row until
  the nightly sync or Refresh Tags, and one that joined it did not show;
- a deleted list's tag stayed on its titles (rows and Jellyfin), so another
  user's list that reused the tag (#542) showed the deleted list's titles;
- a deleted list's playlist and home row stayed until the owner's next
  playlist sync, then the row lingered 7 days as unresolvable, pointing at a
  playlist id Jellyfin can give to the next playlist of that name.
Based on therobmilne's report in the issue. The follow-up runs in the
background; here threading.Thread runs it inline, so it is done when the
call returns. Only Jellyfin's HTTP and the list's fetch are faked.
"""
import json
import logging
import shutil
import types
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.lists as lists
import services.jellyfin as jellyfin_mod
import services.smartlists as sl
from models.database import ListItem, ListSubscription, Movie, Setting, TentacleUser
from routers.lists import ListFetch
from test_tag_push_rules import FakeServer
from tmp_dirs import temp_dir

ADMIN, BOB = "a" * 32, "b" * 32


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Inline:
    """threading.Thread whose start() runs the target there and then."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jellyfin.invalid:8096"), ("jellyfin_api_key", "k" * 32),
                     ("jellyfin_user_id", ADMIN), ("smartlists_path", str(self.tmp / "smartlists"))):
            self.db.add(Setting(key=k, value=v))
        self.admin = TentacleUser(jellyfin_user_id=ADMIN, display_name="Admin", is_admin=True)
        self.bob = TentacleUser(jellyfin_user_id=BOB, display_name="Bob", is_admin=False)
        self.db.add_all([self.admin, self.bob])
        self.db.commit()
        self.fetched = {}
        for p in (mock.patch.object(lists, "threading", types.SimpleNamespace(Thread=_Inline)),
                  mock.patch.object(lists, "SessionLocal", self.Session),
                  mock.patch.object(lists, "_get_tmdb_service", lambda db: None),
                  mock.patch.object(lists, "fetch_list_tmdb_ids",
                                    lambda lst, **kw: ListFetch(self.fetched[lst.id], source="test"))):
            p.start()
            self.addCleanup(p.stop)

    def movie(self, tmdb_id, tags=(), source="provider_1"):
        self.db.add(Movie(tmdb_id=tmdb_id, title=f"Film {tmdb_id}", year="2000", source=source,
                          tags=list(tags)))
        self.db.commit()

    def lst(self, user, tag, tmdb_ids, playlist=True):
        l = ListSubscription(user_id=user.id, name=tag, type="trakt", url="https://trakt.tv/x", tag=tag,
                             active=True, playlist_enabled=playlist)
        self.db.add(l)
        self.db.commit()
        for t in tmdb_ids:
            self.db.add(ListItem(list_id=l.id, tmdb_id=t, media_type="movie", title=f"Film {t}"))
        self.db.commit()
        return l

    def will_fetch(self, lst, tmdb_ids):
        self.fetched[lst.id] = [{"tmdb_id": t, "media_type": "movie", "title": f"Film {t}"} for t in tmdb_ids]

    def row_tags(self, tmdb_id):
        self.db.expire_all()
        return self.db.query(Movie).filter_by(tmdb_id=tmdb_id).one().tags


class TagsAndPlaylistsFollowTheList(_Db):
    """The tag push is the real sync_owned_tags against a fake Jellyfin; the
    playlist functions are recorded, with how many tag writes came before."""

    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        jf = self.server.service()
        self.calls = []

        def record(name):
            def call(db=None, user_id=None, **kw):
                self.calls.append((name, user_id, len(self.server.posts)))
                return {}
            return call

        for p in (mock.patch.object(jellyfin_mod, "JellyfinService", lambda *a, **k: jf),
                  mock.patch.object(sl, "sync_smartlists", record("sync")),
                  mock.patch.object(sl, "refresh_smartlist_playlists", record("refresh")),
                  mock.patch.object(sl, "write_home_config", record("home")),
                  mock.patch.object(sl, "bump_playlist_version", lambda: None),
                  mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db, user=None: {})):
            p.start()
            self.addCleanup(p.stop)

    def jf_tags(self, item_id):
        return self.server.tags(item_id)

    def assert_ran_after_the_push(self, name, user):
        self.assertTrue(self.server.posts, "no tag reached Jellyfin")
        runs = [c for c in self.calls if c[0] == name and c[1] == user.id]
        self.assertTrue(runs, f"no {name} for {user.display_name}'s playlists: {self.calls}")
        self.assertEqual(len(self.server.posts), runs[-1][2],
                         f"the {name} ran before the tags reached Jellyfin: {self.calls}")

    def _bobs_list_changes(self):
        """Film 9 leaves Bob's list, Film 2 (a download) joins it."""
        self.movie(9, ["Weekend"])
        self.movie(4, ["Weekend"])
        self.movie(2, ["Downloaded Movies"], source="radarr")
        self.server.add("Movie", "m9", 9, "Film 9", 2000, ["Weekend"])
        self.server.add("Movie", "m4", 4, "Film 4", 2000, ["Weekend"])
        self.server.add("Movie", "m2", 2, "Film 2", 2000, ["Downloaded Movies"])
        lst = self.lst(self.bob, "Weekend", [9, 4])
        self.will_fetch(lst, [4, 2])
        return lst

    def assert_list_followed(self):
        self.assertNotIn("Weekend", self.row_tags(9))
        self.assertNotIn("Weekend", self.jf_tags("m9"), "the title that left the list is still in its playlist")
        self.assertIn("Weekend", self.jf_tags("m2"), "the title that joined the list is not in its playlist")
        self.assertIn("Downloaded Movies", self.jf_tags("m2"))
        self.assertEqual(["Weekend"], self.jf_tags("m4"))
        self.assert_ran_after_the_push("refresh", self.bob)

    def test_fetch_takes_a_title_that_left_the_list_out_of_its_playlist(self):
        lst = self._bobs_list_changes()
        answer = lists.fetch_list(lst.id, db=self.db, user=self.bob)
        self.assertTrue(answer["success"])
        self.assert_list_followed()

    def test_refresh_all_takes_a_title_that_left_the_list_out_of_its_playlist(self):
        self._bobs_list_changes()
        answer = lists.refresh_all_lists(db=self.db, user=self.bob)
        self.assertEqual(1, answer["refreshed"])
        self.assert_list_followed()

    def test_only_the_changed_titles_are_written(self):
        lst = self._bobs_list_changes()
        self.movie(7, ["Netflix Movies"])
        self.server.add("Movie", "m7", 7, "Film 7", 2000, ["Netflix Movies", "Recently Added Movies"])
        lists.fetch_list(lst.id, db=self.db, user=self.bob)
        self.assertEqual({"m9", "m2"}, {item_id for item_id, _ in self.server.posts})

    def test_delete_takes_the_tag_off_the_lists_titles(self):
        self.movie(9, ["Weekend", "Netflix Movies"])
        self.server.add("Movie", "m9", 9, "Film 9", 2000, ["Weekend", "Netflix Movies", "by hand"])
        lst = self.lst(self.bob, "Weekend", [9])
        lists.delete_list(lst.id, db=self.db, user=self.bob)
        self.assertEqual(["Netflix Movies"], self.row_tags(9))
        self.assertEqual(["Netflix Movies", "by hand"], sorted(self.jf_tags("m9")))
        self.assert_ran_after_the_push("sync", self.bob)

    def test_delete_keeps_a_tag_another_list_still_gives(self):
        self.movie(9, ["Weekend"])
        self.server.add("Movie", "m9", 9, "Film 9", 2000, ["Weekend"])
        mine = self.lst(self.bob, "Weekend", [9])
        self.lst(self.bob, "Weekend", [9], playlist=False)   # the same user's second list on that tag
        lists.delete_list(mine.id, db=self.db, user=self.bob)
        self.assertEqual(["Weekend"], self.row_tags(9))
        self.assertEqual(["Weekend"], self.jf_tags("m9"))

    def test_a_reused_tag_does_not_show_the_deleted_lists_titles(self):
        """#542 lets the admin take Bob's freed tag. Bob's list was deleted
        before this fix, so his films still carry it, in the rows and in
        Jellyfin; the admin's first Fetch must take it off them."""
        for t in (1, 3, 5):
            self.movie(t, ["Weekend"])
            self.server.add("Movie", f"m{t}", t, f"Film {t}", 2000, ["Weekend"])
        self.movie(4)
        self.server.add("Movie", "m4", 4, "Film 4", 2000, [])
        from services.tagger import retire_tag
        retire_tag(self.db, "Weekend")    # Bob's deleted list
        self.db.commit()
        mine = self.lst(self.admin, "Weekend", [])
        self.will_fetch(mine, [4])
        lists.fetch_list(mine.id, db=self.db, user=self.admin)
        for t in (1, 3, 5):
            self.assertNotIn("Weekend", self.jf_tags(f"m{t}"), f"Bob's Film {t} is in the admin's playlist")
        self.assertEqual(["Weekend"], self.jf_tags("m4"))
        self.assert_ran_after_the_push("refresh", self.admin)

    def test_a_jellyfin_that_fails_leaves_the_fetch_answered(self):
        lst = self._bobs_list_changes()
        with mock.patch.object(jellyfin_mod, "sync_owned_tags",
                               mock.Mock(side_effect=ConnectionError("Jellyfin is down"))):
            answer = lists.fetch_list(lst.id, db=self.db, user=self.bob)
        self.assertTrue(answer["success"])
        self.assertNotIn("Weekend", self.row_tags(9))


class _FakeJellyfin:
    deleted = []

    def __init__(self, *a, **k):
        pass

    def delete_tentacle_playlist(self, playlist_id, user_id):
        _FakeJellyfin.deleted.append(playlist_id)
        return True


class DeleteRemovesThePlaylistAndRow(_Db):
    """The real playlist sync and home config, Jellyfin's playlist calls faked."""

    def setUp(self):
        super().setUp()
        _FakeJellyfin.deleted = []
        self.deleted = _FakeJellyfin.deleted
        created = []

        def create(name, *a, **k):
            created.append(name)
            return f"pl-{len(created)}"

        home_dir = self.tmp / "home-configs"
        home_dir.mkdir()
        self.home_path = home_dir / f"{BOB}.json"
        for p in (mock.patch.object(sl, "_create_jellyfin_playlist", create),
                  mock.patch.object(sl, "_mark_linked_playlists", lambda *a, **k: 0),
                  mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db, user=None: {}),
                  mock.patch.object(sl, "refresh_smartlist_playlists", lambda *a, **k: {}),
                  mock.patch.object(sl, "_user_home_config_path", lambda db, user_id=None: self.home_path),
                  mock.patch.object(jellyfin_mod, "JellyfinService", _FakeJellyfin),
                  mock.patch.object(jellyfin_mod, "sync_owned_tags",
                                    lambda *a, **k: {"written": 0, "unchanged": 0, "not_found": 0, "errors": 0})):
            p.start()
            self.addCleanup(p.stop)

        self.movie(9, ["Weekend"])
        self.movie(8, ["Classics"])
        self.weekend = self.lst(self.bob, "Weekend", [9])
        self.lst(self.bob, "Classics", [8])
        sl.sync_smartlists(self.db, user_id=self.bob.id)
        self.smartlists = self.tmp / "smartlists" / BOB
        existing = sl._scan_existing(self.smartlists)
        self.weekend_pid = existing["Weekend"][1]["UserPlaylists"][0]["JellyfinPlaylistId"]
        self.classics_pid = existing["Classics"][1]["UserPlaylists"][0]["JellyfinPlaylistId"]
        self.home_path.write_text(json.dumps({
            "hero": {"enabled": True, "playlist_id": self.classics_pid, "display_name": "Classics"},
            "rows": [{"type": "playlist", "playlist_id": self.weekend_pid, "display_name": "Weekend", "order": 1},
                     {"type": "playlist", "playlist_id": self.classics_pid, "display_name": "Classics",
                      "order": 2}],
            "toolbar": [{"id": "search", "enabled": True}],
        }))

    def test_the_deleted_lists_playlist_and_home_row_go_at_once(self):
        lists.delete_list(self.weekend.id, db=self.db, user=self.bob)
        self.assertEqual([self.weekend_pid], self.deleted, "the list's Jellyfin playlist was kept")
        self.assertEqual(["Classics"], list(sl._scan_existing(self.smartlists)))
        home = json.loads(self.home_path.read_text())
        self.assertEqual([self.classics_pid], [r["playlist_id"] for r in home["rows"]],
                         f"the deleted list's row was kept: {home['rows']}")
        self.assertEqual(self.classics_pid, home["hero"]["playlist_id"])


if __name__ == "__main__":
    unittest.main()
