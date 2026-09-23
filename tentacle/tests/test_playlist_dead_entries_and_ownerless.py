"""#120 (1, 2): dead playlist entries are pruned; empty ownerless duplicates are removed.

Both were verified end-to-end against a real Jellyfin 10.11.11 with the plugin
loaded; these tests pin the backend's side of the contract.

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


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="Rob"))
        self.db.add(mdb.TentacleUser(id=2, jellyfin_user_id="jf-2", display_name="Mom"))
        self.db.commit()
        self.root = self.tmp / "smartlists"
        self.settings = {"jellyfin_url": "http://jellyfin:8096", "jellyfin_api_key": "k",
                         "smartlists_path": str(self.root)}
        p = mock.patch.object(sl, "get_setting",
                              side_effect=lambda db, k, d="": self.settings.get(k, d))
        p.start()
        self.addCleanup(p.stop)

    def config(self, jf_user, name, playlist_id):
        folder = self.root / jf_user / f"f-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.json").write_text(json.dumps({
            "Name": name, "Type": "Playlist", "Id": folder.name, "Enabled": True,
            "UserPlaylists": [{"UserId": jf_user, "JellyfinPlaylistId": playlist_id}],
        }), encoding="utf-8")


# ── Dead entries ────────────────────────────────────────────────────────────

class TestPruneDeadEntries(_Base):
    def setUp(self):
        super().setUp()
        self.config("jf-1", "Toronto Maple Leafs", "pl-rob")
        self.config("jf-2", "Toronto Maple Leafs", "pl-mom")
        self.config("jf-1", "Universal Movies", "pl-uni")
        self.calls = []

        def prune(svc, ids):
            self.calls.append(sorted(ids))
            return self.answer
        self.answer = {"checkedPlaylists": 3, "prunedPlaylists": 2, "removed": 3,
                       "refused": False, "skipped": []}
        p = mock.patch.object(jellyfin.JellyfinService, "prune_dead_playlist_entries", prune)
        p.start()
        self.addCleanup(p.stop)

    def test_every_users_managed_playlists_are_sent_in_one_call(self):
        self.assertEqual(3, sl.prune_dead_entries(self.db))
        self.assertEqual([["pl-mom", "pl-rob", "pl-uni"]], self.calls)

    def test_one_user_and_named_playlists_only(self):
        sl.prune_dead_entries(self.db, user_id=1, only_names=["Toronto Maple Leafs"])
        self.assertEqual([["pl-rob"]], self.calls)

    def test_no_plugin_route_is_zero_not_an_error(self):
        self.answer = None
        self.assertEqual(0, sl.prune_dead_entries(self.db))

    def test_a_refusal_removes_nothing_and_is_logged(self):
        self.answer = {"refused": True, "dead": 900, "removed": 0}
        with self.assertLogs("services.smartlists", "WARNING") as logs:
            self.assertEqual(0, sl.prune_dead_entries(self.db))
        self.assertIn("refused", " ".join(logs.output))

    def test_not_configured_is_a_no_op(self):
        self.settings["jellyfin_api_key"] = ""
        self.assertEqual(0, sl.prune_dead_entries(self.db))
        self.assertEqual([], self.calls)

    def test_runs_after_every_playlist_refresh(self):
        with mock.patch.object(sl, "_refresh_smartlist_playlists_inner",
                               return_value={"processed": 1, "changed": 0, "created": 0}), \
             mock.patch.object(sl, "bump_playlist_version") as bump:
            result = sl.refresh_smartlist_playlists(self.db, user_id=2, only_names=["Toronto Maple Leafs"])
        self.assertEqual([["pl-mom"]], self.calls)
        self.assertEqual(3, result["dead_removed"])
        bump.assert_called_once()   # clients re-read the rows that changed

    def test_not_after_a_refresh_that_could_not_reach_jellyfin(self):
        with mock.patch.object(sl, "_refresh_smartlist_playlists_inner",
                               return_value={"error": "Jellyfin connection failed", "processed": 0}):
            sl.refresh_smartlist_playlists(self.db, user_id=1)
        self.assertEqual([], self.calls)


class TestJellyfinServicePrune(unittest.TestCase):
    def svc(self, status, body=None):
        s = jellyfin.JellyfinService("http://jf", "k")
        resp = mock.Mock(status_code=status)
        resp.json.return_value = body
        s.session = mock.Mock()
        s.session.post.return_value = resp
        return s

    def test_posts_the_ids(self):
        s = self.svc(200, {"removed": 2})
        self.assertEqual({"removed": 2}, s.prune_dead_playlist_entries(["a", "b"]))
        url = s.session.post.call_args.args[0]
        self.assertTrue(url.endswith("/Tentacle/Playlists/PruneDead"))
        self.assertEqual({"Ids": ["a", "b"]}, s.session.post.call_args.kwargs["json"])

    def test_old_plugin_is_none(self):
        self.assertIsNone(self.svc(404).prune_dead_playlist_entries(["a"]))
        self.assertIsNone(self.svc(500).prune_dead_playlist_entries(["a"]))

    def test_nothing_to_check_makes_no_call(self):
        s = self.svc(200)
        s.prune_dead_playlist_entries([])
        s.session.post.assert_not_called()


# ── Ownerless empty duplicates ──────────────────────────────────────────────

class TestManagedNameVariant(unittest.TestCase):
    managed = {"recently added movies", "toronto maple leafs", "top 250"}

    def test_matches(self):
        for n in ("Recently Added Movies", "Recently Added Movies1", "recently added movies12",
                  "Toronto Maple Leafs1", "Top 2501"):
            self.assertTrue(sl._is_managed_name_variant(n, self.managed), n)

    def test_does_not_match(self):
        for n in ("Recently Added Movies Extra", "My Movies1", "Top 25", "Top 250 x1",
                  "Recently Added Movies 1a", "", "12"):
            self.assertFalse(sl._is_managed_name_variant(n, self.managed), n)


class FakeJf:
    def __init__(self, ownerless):
        self.ownerless = ownerless
        self.deleted = []
        self.playlists = {}

    def __call__(self, *a, **k):
        return self

    def get_ownerless_playlists(self):
        return self.ownerless

    def delete_item(self, pid):
        self.deleted.append(pid)
        return True

    # What the name-based duplicate clean-up reads.
    def get_playlists(self, user_id=None):
        return [{"Id": "pl-canon", "Name": "Recently Added Movies"}]

    def get_playlists_checked(self, user_id=None):
        return self.get_playlists(user_id)

    def get_user_ids(self):
        return ["jf-1", "jf-2"]


class TestOwnerlessCleanup(_Base):
    def run_cleanup(self, ownerless):
        self.config("jf-1", "Recently Added Movies", "pl-canon")
        fake = FakeJf(ownerless)
        with mock.patch.object(jellyfin, "JellyfinService", fake):
            n = sl.cleanup_orphaned_playlists(self.db, 1)
        return n, fake.deleted

    def test_empty_ownerless_duplicate_is_deleted(self):
        n, deleted = self.run_cleanup([
            {"id": "pl-ghost", "name": "Recently Added Movies1", "entries": 0}])
        self.assertEqual(["pl-ghost"], deleted)
        self.assertEqual(1, n)

    def test_one_with_entries_is_kept(self):
        _, deleted = self.run_cleanup([
            {"id": "pl-full", "name": "Recently Added Movies1", "entries": 4}])
        self.assertEqual([], deleted)

    def test_an_unmanaged_name_is_kept(self):
        _, deleted = self.run_cleanup([
            {"id": "pl-mine", "name": "Road Trip1", "entries": 0}])
        self.assertEqual([], deleted)

    def test_a_managed_name_of_another_user_counts(self):
        self.config("jf-2", "Toronto Maple Leafs", "pl-mom")
        _, deleted = self.run_cleanup([
            {"id": "pl-ghost2", "name": "Toronto Maple Leafs1", "entries": 0}])
        self.assertEqual(["pl-ghost2"], deleted)

    def test_plugin_without_the_route_changes_nothing(self):
        n, deleted = self.run_cleanup(None)
        self.assertEqual((0, []), (n, deleted))


if __name__ == "__main__":
    unittest.main()
