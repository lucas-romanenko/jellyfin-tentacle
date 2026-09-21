"""Cross-user safety of the playlist cleanup and of the name-based lookup.

Jellyfin keeps every playlist in one flat store and every Tentacle user gets
the same generated playlist names ("Netflix Movies", "HBO TV", ...). A playlist
that is shared, public or ownerless is listed for other users too, so:

* cleanup_orphaned_playlists() — which deletes any visible playlist whose name
  matches one it manages and whose id is not its own — can delete a playlist
  that belongs to a different user, using an admin key that no permission check
  stops; and
* _find_jellyfin_playlist() — exact name match inside one user's item listing —
  can adopt another user's playlist, after which both users' refreshes fight
  over its contents.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.jellyfin as jellyfin
import services.smartlists as sl


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class FakeServer:
    """Jellyfin's playlist store, with per-user visibility.

    `visible_to` is the set of Jellyfin user ids that see the playlist. A
    Tentacle-created playlist is private (one owner); a shared, public or
    ownerless playlist is visible to several users.
    """

    def __init__(self):
        self.playlists = {}   # id -> {"Name": str, "visible_to": set}
        self.deleted = []
        self.created = []

    def add(self, pid, name, visible_to):
        self.playlists[pid] = {"Name": name, "visible_to": set(visible_to)}


class FakeJellyfinService:
    def __init__(self, server):
        self.server = server
        self.jellyfin_users = ["jf-1", "jf-2"]

    def get_user_ids(self):
        return self.jellyfin_users

    def __call__(self, url, api_key, user_id="", **kw):
        self.user_id = user_id
        return self

    def get_playlists(self, user_id=None):
        uid = user_id or self.user_id
        return [
            {"Id": pid, "Name": pl["Name"], "ChildCount": 1}
            for pid, pl in self.server.playlists.items()
            if uid in pl["visible_to"]
        ]

    def delete_item(self, item_id):
        self.server.deleted.append(item_id)
        self.server.playlists.pop(item_id, None)
        return True


def _settings(db, key, default=""):
    return {"jellyfin_url": "http://jellyfin:8096",
            "jellyfin_api_key": "k"}.get(key, default)


class CleanupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = _session(self.tmp)
        self.db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1"))
        self.db.add(mdb.TentacleUser(id=2, jellyfin_user_id="jf-2", display_name="User 2"))
        self.db.commit()
        self.root = self.tmp / "smartlists"
        self.server = FakeServer()
        self.fake_service = FakeJellyfinService(self.server)

    def config(self, user_jf_id, name, playlist_id):
        folder = self.root / user_jf_id / f"f-{name}-{playlist_id}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.json").write_text(json.dumps({
            "Name": name, "Type": "Playlist", "Id": folder.name,
            "UserPlaylists": [{"UserId": user_jf_id, "JellyfinPlaylistId": playlist_id}],
        }), encoding="utf-8")

    def cleanup(self, user_id):
        def setting(db, key, default=""):
            if key == "smartlists_path":
                return str(self.root)
            return _settings(db, key, default)
        with mock.patch.object(sl, "get_setting", side_effect=setting), \
             mock.patch.object(jellyfin, "JellyfinService", self.fake_service):
            return sl.cleanup_orphaned_playlists(self.db, user_id)


class TestCleanupIsOwnerAware(CleanupBase):

    def test_another_users_playlist_is_not_deleted_as_a_duplicate(self):
        """Both users have an "HBO TV" SmartList, with their own playlist.
        User 2 shares theirs (Jellyfin's playlist sharing / a public playlist),
        so it appears in user 1's listing under a name user 1 also manages —
        and the nightly cleanup for user 1 deletes user 2's row."""
        self.config("jf-1", "HBO TV", "pl-user1")
        self.config("jf-2", "HBO TV", "pl-user2")
        self.server.add("pl-user1", "HBO TV", {"jf-1"})
        self.server.add("pl-user2", "HBO TV", {"jf-2", "jf-1"})

        deleted = self.cleanup(1)

        self.assertEqual(self.server.deleted, [],
                         "cleanup deleted a playlist belonging to another user")
        self.assertEqual(deleted, 0)

    def test_a_playlist_visible_to_another_user_is_left_alone(self):
        """An ownerless / public playlist that carries a managed name is visible
        to every user. It is not this user's duplicate to reap: deleting it
        removes it from everyone."""
        self.config("jf-1", "Recently Added Movies", "pl-user1")
        self.server.add("pl-user1", "Recently Added Movies", {"jf-1"})
        self.server.add("pl-public", "Recently Added Movies", {"jf-1", "jf-2"})

        self.cleanup(1)

        self.assertEqual(self.server.deleted, [],
                         "cleanup deleted a playlist other users can see")

    def test_a_jellyfin_only_users_public_playlist_is_left_alone(self):
        """Found live (E2E-DATA): one Tentacle admin, plus a family member who
        only uses a Jellyfin client and so has no TentacleUser row. Their public
        playlist carries a managed name, the admin can see it, and nobody with a
        Tentacle login vouches for it — the cleanup deleted it."""
        self.db.query(mdb.TentacleUser).filter(mdb.TentacleUser.id == 2).delete()
        self.db.commit()
        self.fake_service.jellyfin_users = ["jf-1", "jf-mom"]
        self.config("jf-1", "Downloaded Movies", "pl-user1")
        self.server.add("pl-user1", "Downloaded Movies", {"jf-1"})
        self.server.add("pl-mom", "Downloaded Movies", {"jf-mom", "jf-1"})
        self.server.add("pl-dupe", "Downloaded Movies", {"jf-1"})

        deleted = self.cleanup(1)

        self.assertEqual(self.server.deleted, ["pl-dupe"],
                         "only the admin's own private duplicate may go")
        self.assertEqual(deleted, 1)

    def test_nothing_is_deleted_when_jellyfin_will_not_list_its_users(self):
        self.config("jf-1", "Netflix Movies", "pl-user1")
        self.server.add("pl-user1", "Netflix Movies", {"jf-1"})
        self.server.add("pl-dupe", "Netflix Movies", {"jf-1"})
        self.fake_service.jellyfin_users = None

        self.assertEqual(self.cleanup(1), 0)
        self.assertEqual(self.server.deleted, [])

    def test_a_private_duplicate_is_still_deleted(self):
        """The case the reaper exists for: a second playlist with a managed
        name, visible only to this user (an old recreate-on-timeout duplicate)."""
        self.config("jf-1", "Netflix Movies", "pl-user1")
        self.config("jf-2", "Netflix Movies", "pl-user2")
        self.server.add("pl-user1", "Netflix Movies", {"jf-1"})
        self.server.add("pl-user2", "Netflix Movies", {"jf-2"})
        self.server.add("pl-dupe", "Netflix Movies", {"jf-1"})

        deleted = self.cleanup(1)

        self.assertEqual(self.server.deleted, ["pl-dupe"])
        self.assertEqual(deleted, 1)

    def test_nothing_is_deleted_when_another_users_listing_fails(self):
        """If Jellyfin won't say what the other user can see, a shared playlist
        can't be ruled out — so nothing may be deleted this run."""
        self.config("jf-1", "Netflix Movies", "pl-user1")
        self.server.add("pl-user1", "Netflix Movies", {"jf-1"})
        self.server.add("pl-dupe", "Netflix Movies", {"jf-1"})

        real_get = self.fake_service.get_playlists

        def flaky(user_id=None):
            if user_id == "jf-2":
                raise RuntimeError("read timeout")
            return real_get(user_id)

        with mock.patch.object(self.fake_service, "get_playlists", flaky):
            deleted = self.cleanup(1)

        self.assertEqual(self.server.deleted, [])
        self.assertEqual(deleted, 0)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestLookupDoesNotAdoptAnotherUsersPlaylist(CleanupBase):
    """sync_smartlists() links a config that has no playlist id yet by searching
    Jellyfin for the exact name inside that user's own item listing. A shared or
    public playlist of another user matches that search."""

    def _sync(self, desired_name):
        posted = []

        def fake_get(url, headers=None, params=None, timeout=None):
            uid = url.rstrip("/").split("/")[-2]
            items = [{"Id": pid, "Name": pl["Name"]}
                     for pid, pl in self.server.playlists.items()
                     if uid in pl["visible_to"]]
            return FakeResponse({"Items": items})

        def fake_post(url, headers=None, json=None, timeout=None):
            posted.append(json)
            pid = f"pl-new-{len(posted)}"
            self.server.add(pid, json["Name"], {json["UserId"]})
            return FakeResponse({"Id": pid})

        def setting(db, key, default=""):
            if key == "smartlists_path":
                return str(self.root)
            return _settings(db, key, default)

        desired = [{"name": desired_name, "tag": desired_name, "media_type": ["Movie"],
                    "enabled": True, "source": "auto"}]
        with mock.patch.object(sl, "get_setting", side_effect=setting), \
             mock.patch.object(sl, "get_desired_smartlists", return_value=desired), \
             mock.patch.object(sl.requests, "get", fake_get), \
             mock.patch.object(sl.requests, "post", fake_post), \
             mock.patch.object(jellyfin, "JellyfinService", self.fake_service):
            sl.sync_smartlists(self.db, user_id=1)

        existing = sl._scan_existing(self.root / "jf-1")
        _folder, cfg = existing[desired_name]
        return cfg["UserPlaylists"][0]["JellyfinPlaylistId"], posted

    def test_a_shared_playlist_of_another_user_is_not_linked(self):
        # User 2 owns "HBO TV" and has shared it; user 1's config has no id yet.
        self.config("jf-2", "HBO TV", "pl-user2")
        self.config("jf-1", "HBO TV", "")
        self.server.add("pl-user2", "HBO TV", {"jf-2", "jf-1"})

        linked, posted = self._sync("HBO TV")

        self.assertNotEqual(linked, "pl-user2",
                            "user 1's SmartList adopted user 2's Jellyfin playlist")
        self.assertEqual(len(posted), 1, "a playlist of its own should have been created")

    def test_the_users_own_playlist_is_still_reused(self):
        # The reason the lookup exists: don't create a second playlist when one
        # with that name is already there for this user.
        self.config("jf-1", "HBO TV", "")
        self.server.add("pl-mine", "HBO TV", {"jf-1"})

        linked, posted = self._sync("HBO TV")

        self.assertEqual(linked, "pl-mine")
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
