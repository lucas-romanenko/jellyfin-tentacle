"""A channel playlist is checked as the user who owns it.

Every user gets their own Jellyfin playlist of each channel. The checks that
top a short playlist up (the hourly reconcile, the background refill, "Refill
playlists now") and the one after a publish read each playlist with the
admin's Jellyfin user. Jellyfin 10.11 answers 404 "Playlist not found" when a
user who does not own a playlist asks for its items, and get_playlist_items
returns None for that, which the callers rightly treat as "could not read".
So every other user's channel playlist was never checked: one filled before
Jellyfin had imported the videos stayed short until the nightly rebuild, and
"Refill playlists now" answered that nothing was behind.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_playlists_of_every_user.py
"""
import unittest
from unittest import mock

from models.database import TentacleUser, YouTubeVideo, set_setting
from services import jellyfin as jf_module
from services import smartlists
from services.youtube import sync as ysync
from test_youtube_traffic import _Db

ADMIN, OTHER, KID = "jf-admin", "jf-other", "jf-kid"


class FakeJellyfin:
    """Jellyfin 10.11 as the YouTube checks see it.

    A playlist's items can be read only as its owner (anyone else: 404, which
    get_playlist_items reports as None). Each user sees the channel's videos
    their parental limit allows; the kid sees none of the rated channel.
    """
    playlists = {}      # playlist id -> (owner, number of entries)
    visible = {}        # jellyfin user id (None = library) -> number of videos with the tag

    def __init__(self, url, key, user_id=""):
        self.user_id = user_id

    def get_playlist_items(self, playlist_id, limit=50000):
        owner, n = self.playlists[playlist_id]
        if self.user_id != owner:
            return None
        return [{"Id": f"i{k}"} for k in range(n)]

    queries = []

    def query_items(self, include_types=None, tags=None, user_id=None, **kw):
        FakeJellyfin.queries.append(user_id)
        return [{"Id": f"i{k}"} for k in range(self.visible.get(user_id, 0))]

    def get_libraries(self):
        return []


class ChannelPlaylistsOfEveryUser(_Db):
    def setUp(self):
        super().setUp()
        set_setting(self.db, "jellyfin_url", "http://jf.invalid")
        set_setting(self.db, "jellyfin_api_key", "k")
        set_setting(self.db, "jellyfin_user_id", ADMIN)
        self.users = {}
        for jid, name in ((ADMIN, "admin"), (OTHER, "other"), (KID, "kid")):
            u = TentacleUser(jellyfin_user_id=jid, display_name=name, is_admin=(jid == ADMIN))
            self.db.add(u)
            self.db.commit()
            self.users[jid] = u.id
        self.ch = self.channel(title="Chan", slug="chan", keep_count=5)
        for k in range(5):
            self.db.add(YouTubeVideo(channel_fk=self.ch.id, video_id=f"vid{k:08d}", title=f"V{k}"))
        self.db.commit()
        FakeJellyfin.playlists = {"p-admin": (ADMIN, 5), "p-other": (OTHER, 0), "p-kid": (KID, 0)}
        FakeJellyfin.queries = []
        # Jellyfin has imported all five; the kid may see none of them (a rated channel).
        FakeJellyfin.visible = {None: 5, ADMIN: 5, OTHER: 5, KID: 0}
        owners = {self.users[ADMIN]: "p-admin", self.users[OTHER]: "p-other", self.users[KID]: "p-kid"}

        def lists(db, user_id=None):
            return [{"name": "Chan", "playlist_id": owners[user_id], "is_youtube": True,
                     "yt_tags": ["yt:chan"]}]

        self.refreshed = []
        for name, value in (
            ("JellyfinService", FakeJellyfin),
        ):
            p = mock.patch.object(jf_module, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (
            ("_get_smartlists_with_playlist_ids", lists),
            ("refresh_smartlist_playlists",
             lambda db, user_id=None, only_names=None, **kw: self.refreshed.append((user_id, tuple(only_names or ())))),
            ("sync_smartlists", lambda db, user_id=None: {}),
            ("write_home_config", lambda db, user_id=None: {}),
            ("bump_playlist_version", lambda: None),
            ("_notify_jellyfin_plugin", lambda db: {}),
            ("prune_dead_entries", lambda db, **kw: 0),
        ):
            p = mock.patch.object(smartlists, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_another_users_short_playlist_is_topped_up(self):
        fixed, behind = ysync.reconcile_playlists(self.db, report=True)
        self.assertIn((self.users[OTHER], ("Chan",)), self.refreshed)
        self.assertEqual(1, fixed)
        self.assertFalse(behind)

    def test_a_playlist_holding_all_its_owner_may_see_is_left_alone(self):
        ysync.reconcile_playlists(self.db, report=True)
        self.assertNotIn(self.users[KID], [u for u, _ in self.refreshed])
        self.assertNotIn(self.users[ADMIN], [u for u, _ in self.refreshed])

    def test_nothing_is_behind_once_every_playlist_is_full(self):
        FakeJellyfin.playlists["p-other"] = (OTHER, 5)
        fixed, behind = ysync.reconcile_playlists(self.db, report=True)
        self.assertEqual((0, False), (fixed, behind))
        self.assertEqual([], self.refreshed)

    def test_videos_not_imported_yet_still_count_as_behind(self):
        FakeJellyfin.visible = {None: 2, ADMIN: 2, OTHER: 2, KID: 0}
        FakeJellyfin.playlists = {"p-admin": (ADMIN, 2), "p-other": (OTHER, 2), "p-kid": (KID, 0)}
        fixed, behind = ysync.reconcile_playlists(self.db, report=True)
        self.assertEqual(0, fixed)
        self.assertTrue(behind)

    def test_the_check_after_a_publish_sees_another_users_short_playlist(self):
        with mock.patch.object(ysync, "youtube_library", return_value=(None, None)), \
             mock.patch.object(ysync, "_warm_streams", return_value=0), \
             mock.patch.object(ysync, "_wait_for_channel_items", return_value=5), \
             mock.patch.object(ysync, "start_background_refill") as refill, \
             mock.patch.object(FakeJellyfin, "trigger_library_scan", create=True, return_value=True):
            result = ysync.publish_to_jellyfin(self.db, [self.ch])
        self.assertTrue(result["short"])
        refill.assert_called_once()

    def test_after_a_publish_a_restricted_users_playlist_is_not_short(self):
        FakeJellyfin.playlists["p-other"] = (OTHER, 5)
        with mock.patch.object(ysync, "youtube_library", return_value=(None, None)), \
             mock.patch.object(ysync, "_warm_streams", return_value=0), \
             mock.patch.object(ysync, "_wait_for_channel_items", return_value=5), \
             mock.patch.object(ysync, "start_background_refill") as refill, \
             mock.patch.object(FakeJellyfin, "trigger_library_scan", create=True, return_value=True):
            result = ysync.publish_to_jellyfin(self.db, [self.ch])
        self.assertFalse(result["short"])
        refill.assert_not_called()

    def test_a_full_playlist_costs_no_count_query_after_a_publish(self):
        FakeJellyfin.playlists = {"p-admin": (ADMIN, 5), "p-other": (OTHER, 5), "p-kid": (KID, 5)}
        FakeJellyfin.visible = {None: 5, ADMIN: 5, OTHER: 5, KID: 5}
        with mock.patch.object(ysync, "youtube_library", return_value=(None, None)), \
             mock.patch.object(ysync, "_warm_streams", return_value=0), \
             mock.patch.object(ysync, "_wait_for_channel_items", return_value=5), \
             mock.patch.object(ysync, "start_background_refill"), \
             mock.patch.object(FakeJellyfin, "trigger_library_scan", create=True, return_value=True):
            result = ysync.publish_to_jellyfin(self.db, [self.ch])
        self.assertFalse(result["short"])
        self.assertEqual([], FakeJellyfin.queries)

    def test_only_a_short_playlist_is_counted_after_a_publish(self):
        # Admin and other full; the restricted user's playlist is short of the
        # library, so only it is counted (library, then as that user).
        FakeJellyfin.playlists["p-other"] = (OTHER, 5)
        with mock.patch.object(ysync, "youtube_library", return_value=(None, None)), \
             mock.patch.object(ysync, "_warm_streams", return_value=0), \
             mock.patch.object(ysync, "_wait_for_channel_items", return_value=5), \
             mock.patch.object(ysync, "start_background_refill"), \
             mock.patch.object(FakeJellyfin, "trigger_library_scan", create=True, return_value=True):
            ysync.publish_to_jellyfin(self.db, [self.ch])
        self.assertEqual([None, KID], FakeJellyfin.queries)


if __name__ == "__main__":
    unittest.main()
