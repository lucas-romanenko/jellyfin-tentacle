"""Entry-point regression cover for the original SmartList / home-row reports.

#7  a Jellyfin timeout on the playlist existence check was read as "deleted" and
    a duplicate playlist was created every time
#8  write_home_config silently dropped a row it could not resolve
#9  the stale-toggle cleanup deleted toggles the user had ENABLED
#12 a series playlist emptied itself: episode ids diffed against series ids

The tests that came with the fix (test_home_config.py, test_smartlists_playlists.py)
import helpers the fix introduced, so they cannot run on the code before it. These
go through the functions that existed before and after -- _process_single_playlist,
write_home_config and sync_smartlists -- so they fail on 98f30e2^ and pass from 98f30e2 on.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db(tmp):
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _stats():
    return {"created": 0, "updated": 0, "processed": 0, "changed": 0, "errors": 0,
            "item_counts": {}, "changed_names": []}


class FakeJellyfin:
    """The JellyfinService surface _process_single_playlist uses.

    `timeout=True` models Jellyfin not answering the existence probe in time:
    get_item_by_id() returns None for that (a real 404 raises), and the tri-state
    item_exists() the fix added returns None.
    """

    def __init__(self, items, playlist_entries, timeout=False):
        self.items = items
        self.entries = playlist_entries
        self.timeout = timeout
        self.created, self.added, self.removed, self.deleted = [], [], [], []

    def query_items(self, **kw):
        return list(self.items)

    def get_item_by_id(self, item_id):
        return None if self.timeout else {"Id": item_id, "Type": "Playlist"}

    def item_exists(self, item_id):
        return None if self.timeout else True

    def get_playlist_items(self, playlist_id, limit=50000):
        return list(self.entries)

    def create_playlist(self, name, item_ids=None, *a, **k):
        self.created.append(name)
        return "new-playlist-id"

    def add_to_playlist(self, playlist_id, ids):
        self.added.append(list(ids))
        return True

    def remove_from_playlist(self, playlist_id, entry_ids):
        self.removed.append(list(entry_ids))
        return True

    def move_playlist_item(self, *a, **k):
        return True

    def delete_item(self, item_id):
        self.deleted.append(item_id)
        return True

    def get_playlists(self, *a, **k):
        return []

    def get_ownerless_playlists(self):
        return []


def _config(name, playlist_id="pl-1", media="Series"):
    return {"Name": name, "Enabled": True, "MediaTypes": [media],
            "ExpressionSets": [{"Expressions": [{"MemberName": "Tags", "Operator": "Contains",
                                                 "TargetValue": name}]}],
            "Order": {"SortOptions": [{"SortBy": "Name", "SortOrder": "Ascending"}]},
            "UserPlaylists": [{"UserId": "u1", "JellyfinPlaylistId": playlist_id}],
            "JellyfinPlaylistId": playlist_id}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = _db(self.tmp)
        self.addCleanup(self.db.close)

    def process(self, jf, config):
        import services.smartlists as sl
        folder = self.tmp / "sl" / config["Name"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.json").write_text(json.dumps(config))
        stats = _stats()
        with mock.patch.object(sl, "bump_playlist_version", create=True):
            sl._process_single_playlist(jf, folder, config, "u1", stats, db=self.db)
        return stats


class TestIssue7TimeoutIsNotDeletion(_Base):
    def test_existence_probe_timeout_does_not_create_a_duplicate(self):
        movies = [{"Id": f"m{i}", "Type": "Movie", "Name": f"M{i}"} for i in range(3)]
        entries = [{"Id": m["Id"], "Type": "Movie", "PlaylistItemId": f"pi{i}"} for i, m in enumerate(movies)]
        jf = FakeJellyfin(movies, entries, timeout=True)
        self.process(jf, _config("Downloaded TV", media="Movie"))
        self.assertEqual(jf.created, [], "#7: a timed-out existence check created a new playlist")


class TestIssue12SeriesPlaylistDoesNotDrain(_Base):
    def test_one_series_without_episodes_does_not_empty_the_playlist(self):
        # Desired: series A, B and C; C has no episodes in Jellyfin yet, so the
        # playlist (stored as expanded episodes) differs from the desired set by one.
        series = [{"Id": s, "Type": "Series", "Name": s} for s in ("A", "B", "C")]
        entries = [{"Id": f"{s}-ep{n}", "Type": "Episode", "SeriesId": s, "PlaylistItemId": f"pi-{s}-{n}"}
                   for s in ("A", "B") for n in range(3)]
        jf = FakeJellyfin(series, entries)
        self.process(jf, _config("Disney+ TV"))
        removed = {e for batch in jf.removed for e in batch}
        still_wanted = {e["PlaylistItemId"] for e in entries} | {e["Id"] for e in entries}
        self.assertFalse(removed & still_wanted,
                         f"#12: episodes of still-desired series were removed: {sorted(removed & still_wanted)}")


class TestIssue8UnresolvableRowIsKept(_Base):
    def _run(self, rows, smartlists):
        import services.smartlists as sl
        path = self.tmp / "user.json"
        existing = {"hero": None, "rows": rows, "toolbar": None}
        path.write_text(json.dumps(existing))
        with mock.patch.object(sl, "_get_smartlists_with_playlist_ids", return_value=smartlists), \
             mock.patch.object(sl, "get_home_config", return_value=existing), \
             mock.patch.object(sl, "_user_home_config_path", return_value=path), \
             mock.patch.object(sl, "get_setting", return_value="20"), \
             mock.patch.object(sl, "bump_playlist_version", create=True):
            return sl.write_home_config(self.db, user_id=1)

    def test_row_with_stale_id_and_ambiguous_name_is_kept(self):
        rows = [{"type": "playlist", "playlist_id": "stale", "display_name": "Downloaded TV", "order": 1},
                {"type": "playlist", "playlist_id": "ok", "display_name": "Netflix TV", "order": 2}]
        config = self._run(rows, [{"playlist_id": "dup1", "name": "Downloaded TV"},
                                  {"playlist_id": "dup2", "name": "Downloaded TV"},
                                  {"playlist_id": "ok", "name": "Netflix TV"}])
        names = [r.get("display_name") for r in config["rows"]]
        self.assertIn("Downloaded TV", names, f"#8: an unresolvable home row was dropped: {names}")


class TestIssue9EnabledToggleSurvivesAbsentContent(_Base):
    def test_enabled_toggle_is_not_deleted_while_its_source_is_empty(self):
        from models.database import AutoPlaylistToggle, TentacleUser, set_setting
        import services.smartlists as sl
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=True))
        self.db.add(AutoPlaylistToggle(user_id=1, key="source:Disney+:series", enabled=True))
        self.db.commit()
        set_setting(self.db, "smartlists_path", str(self.tmp / "smartlists"))
        # No Series row carries the Disney+ source tag right now (a provider hiccup).
        sl.sync_smartlists(self.db, user_id=1)
        left = [t.key for t in self.db.query(AutoPlaylistToggle).filter_by(user_id=1).all()]
        self.assertIn("source:Disney+:series", left, "#9: an ENABLED toggle was deleted")


if __name__ == "__main__":
    unittest.main()
