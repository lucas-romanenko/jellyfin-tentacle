"""Tentacle takes over or deletes only playlists it made (#152).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A SmartList with no linked playlist looked for one by exact name in the user's
Jellyfin listing and adopted it. A playlist the user had made by hand
("Christmas") passed, and from then on every refresh replaced its hand-picked
entries with the rule's, and deleting the rule, switching it off or the nightly
clean-up deleted it. Tentacle now marks every playlist it creates (provider id
"Tentacle", kept in the playlist's own playlist.xml, so it survives a restart, a
library scan, a full metadata refresh and a wiped Tentacle data directory), and:
- a same-name playlist is taken over only if it carries the mark, or is empty
  (then it is marked);
- a playlist is deleted only if it carries the mark;
- every sync marks the playlists its configs already link (once, on upgrade),
  unless another Jellyfin user sees them too.
"""
import json
import logging
import unittest
from unittest import mock

import models.database as mdb
import services.jellyfin as jellyfin
import services.smartlists as sl
import test_playlist_cleanup_multiuser as multi

CleanupBase = multi.CleanupBase   # no tests of its own


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class Lookup(CleanupBase):
    _sync = multi.TestLookupDoesNotAdoptAnotherUsersPlaylist._sync

    def test_a_users_own_playlist_with_entries_is_not_taken(self):
        self.config("jf-1", "Christmas", "")
        self.server.add("pl-hand", "Christmas", {"jf-1"}, marked=False, entries=3)

        linked, posted = self._sync("Christmas")

        self.assertNotEqual(linked, "pl-hand", "the user's hand-made playlist was taken over")
        self.assertEqual(1, len(posted), "Tentacle should make a playlist of its own")
        self.assertFalse(self.server.playlists["pl-hand"]["marked"])
        self.assertNotIn("pl-hand", self.server.deleted)

    def test_an_empty_playlist_is_taken_and_marked(self):
        # An interrupted create: the playlist was made, the mark or the config write wasn't.
        self.config("jf-1", "Christmas", "")
        self.server.add("pl-empty", "Christmas", {"jf-1"}, marked=False, entries=0)

        linked, posted = self._sync("Christmas")

        self.assertEqual("pl-empty", linked)
        self.assertEqual([], posted)
        self.assertTrue(self.server.playlists["pl-empty"]["marked"])

    def test_an_unknown_entry_count_is_not_read_as_empty(self):
        self.config("jf-1", "Christmas", "")
        self.server.add("pl-hand", "Christmas", {"jf-1"}, marked=False, entries=None)

        linked, posted = self._sync("Christmas")

        self.assertNotEqual("pl-hand", linked)
        self.assertEqual(1, len(posted))

    def test_a_new_playlist_is_marked(self):
        self.config("jf-1", "Christmas", "")

        linked, posted = self._sync("Christmas")

        self.assertEqual("pl-new-1", linked)
        self.assertTrue(self.server.playlists["pl-new-1"]["marked"])

    def test_a_wiped_data_directory_finds_tentacles_playlists_again(self):
        # No config at all (the data directory was wiped); Tentacle's old
        # playlist is still in Jellyfin, with its entries and its mark.
        self.server.add("pl-old", "Netflix Movies", {"jf-1"}, marked=True, entries=40)

        linked, posted = self._sync("Netflix Movies")

        self.assertEqual("pl-old", linked)
        self.assertEqual([], posted, "a second 'Netflix Movies' was created")


class Cleanup(CleanupBase):
    def test_the_nightly_clean_up_keeps_a_users_own_playlist_of_a_managed_name(self):
        self.config("jf-1", "Christmas", "pl-rule")
        self.server.add("pl-rule", "Christmas", {"jf-1"}, marked=True)
        self.server.add("pl-hand", "Christmas", {"jf-1"}, marked=False, entries=3)

        self.assertEqual(0, self.cleanup(1))
        self.assertEqual([], self.server.deleted)


class MarkPass(CleanupBase):
    def _mark(self):
        with mock.patch.object(jellyfin, "JellyfinService", self.fake_service), \
             mock.patch.object(sl, "_user_smartlists_path", lambda db, uid: self.root / f"jf-{uid}"):
            return sl._mark_linked_playlists(self.db, 1, "jf-1", "http://jellyfin:8096", "k")

    def test_a_linked_playlist_is_marked_once(self):
        """On the first sync after the upgrade, every playlist Tentacle already
        manages gets the mark (a hand-made one taken over by name before this
        can't be told apart and is grandfathered)."""
        self.config("jf-1", "Netflix Movies", "pl-linked")
        self.server.add("pl-linked", "Netflix Movies", {"jf-1"}, marked=False, entries=40)
        self.server.add("pl-other", "Christmas", {"jf-1"}, marked=False, entries=3)

        self.assertEqual(1, self._mark())
        self.assertEqual(["pl-linked"], self.server.marked, "only linked playlists are marked")
        self.assertEqual(0, self._mark(), "a second sync marks nothing")

    def test_a_linked_playlist_another_user_sees_is_not_marked(self):
        self.config("jf-1", "HBO TV", "pl-shared")
        self.server.add("pl-shared", "HBO TV", {"jf-1", "jf-2"}, marked=False)

        self.assertEqual(0, self._mark())
        self.assertEqual([], self.server.marked)

    def test_nothing_is_marked_when_the_listing_fails(self):
        self.config("jf-1", "Netflix Movies", "pl-linked")
        self.server.add("pl-linked", "Netflix Movies", {"jf-1"}, marked=False)
        with mock.patch.object(self.fake_service, "get_playlists_checked", lambda user_id=None: None):
            self.assertEqual(0, self._mark())


class OrphanedSmartList(CleanupBase):
    """A SmartList that is no longer desired loses its folder; its playlist goes
    only if Tentacle made it."""

    def _sync_with_nothing_desired_but_one(self, keep):
        def setting(db, key, default=""):
            if key == "smartlists_path":
                return str(self.root)
            return {"jellyfin_url": "http://jellyfin:8096", "jellyfin_api_key": "k"}.get(key, default)
        desired = [{"name": keep, "tag": keep, "media_type": ["Movie"], "enabled": True, "source": "auto"}]
        with mock.patch.object(sl, "get_setting", side_effect=setting), \
             mock.patch.object(sl, "get_desired_smartlists", return_value=desired), \
             mock.patch.object(sl, "_enabled_toggle_names", return_value=set()), \
             mock.patch.object(sl, "_mark_linked_playlists", return_value=0), \
             mock.patch.object(jellyfin, "JellyfinService", self.fake_service):
            sl.sync_smartlists(self.db, user_id=1)

    def test_an_unmarked_linked_playlist_is_kept(self):
        self.config("jf-1", "Kept", "pl-kept")
        self.server.add("pl-kept", "Kept", {"jf-1"}, marked=True)
        self.config("jf-1", "Christmas", "pl-hand")
        self.server.add("pl-hand", "Christmas", {"jf-1"}, marked=False, entries=3)

        self._sync_with_nothing_desired_but_one("Kept")

        self.assertNotIn("pl-hand", self.server.deleted)
        self.assertNotIn("Christmas", sl._scan_existing(self.root / "jf-1"), "the SmartList itself goes")

    def test_a_marked_linked_playlist_is_deleted(self):
        self.config("jf-1", "Kept", "pl-kept")
        self.server.add("pl-kept", "Kept", {"jf-1"}, marked=True)
        self.config("jf-1", "Old Rule", "pl-rule")
        self.server.add("pl-rule", "Old Rule", {"jf-1"}, marked=True)

        self._sync_with_nothing_desired_but_one("Kept")

        self.assertEqual(["pl-rule"], self.server.deleted)


class _Resp:
    def __init__(self, status=204):
        self.status_code, self.text = status, ""


class _Session:
    def __init__(self):
        self.posted, self.deleted = [], []

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        return _Resp()

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        return _Resp()


class TheRealService(unittest.TestCase):
    """JellyfinService's mark and delete, with only HTTP faked."""

    def _service(self, item):
        svc = jellyfin.JellyfinService("http://jf:8096", "k", "jf-1")
        svc.session = _Session()
        svc.seen = []

        def get(path, params=None):
            svc.seen.append(path)
            return dict(item) if item else None
        svc._get = get
        return svc

    PLAYLIST = {"Id": "pl-1", "Name": "Christmas", "Type": "Playlist", "LockData": True,
                "ProviderIds": {"Other": "7"}, "Tags": []}

    def test_marking_keeps_the_playlist_and_its_other_ids(self):
        svc = self._service(self.PLAYLIST)
        self.assertTrue(svc.mark_tentacle_playlist("pl-1", "jf-owner"))
        url, payload = svc.session.posted[0]
        self.assertTrue(url.endswith("/Items/pl-1"))
        self.assertEqual({"Other": "7", "Tentacle": "managed"}, payload["ProviderIds"])
        self.assertEqual("Christmas", payload["Name"])
        self.assertIs(True, payload["LockData"])
        self.assertEqual(["/Users/jf-owner/Items/pl-1"], svc.seen, "read as its owner sees it")

    def test_a_marked_playlist_is_not_written_again(self):
        svc = self._service({**self.PLAYLIST, "ProviderIds": {"Tentacle": "managed"}})
        self.assertTrue(svc.mark_tentacle_playlist("pl-1"))
        self.assertEqual([], svc.session.posted)

    def test_only_a_marked_playlist_is_deleted(self):
        svc = self._service(self.PLAYLIST)
        self.assertFalse(svc.delete_tentacle_playlist("pl-1"))
        self.assertEqual([], svc.session.deleted, "the user's own playlist was deleted")

        svc = self._service({**self.PLAYLIST, "ProviderIds": {"tentacle": "managed"}})
        self.assertTrue(svc.delete_tentacle_playlist("pl-1"))
        self.assertEqual(["http://jf:8096/Items/pl-1"], svc.session.deleted)

    def test_a_playlist_that_cant_be_read_is_not_deleted(self):
        svc = self._service(None)
        self.assertFalse(svc.delete_tentacle_playlist("pl-1"))
        self.assertEqual([], svc.session.deleted)

    def test_listings_ask_for_the_mark(self):
        svc = jellyfin.JellyfinService("http://jf:8096", "k", "jf-1")
        asked = []
        svc._get = lambda path, params=None: asked.append(params) or {"Items": []}
        svc.get_playlists("jf-1")
        svc.get_playlists_checked("jf-1")
        self.assertTrue(all("ProviderIds" in p["Fields"] for p in asked))


if __name__ == "__main__":
    unittest.main()
