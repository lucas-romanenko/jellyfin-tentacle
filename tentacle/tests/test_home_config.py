"""Tests for write_home_config's row-preservation rules.

Regression cover for hand-configured home rows being dropped silently when a
playlist was momentarily unresolvable — home rows are never re-added
automatically, so a transient failure removed user configuration permanently.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from services.smartlists import (
    HOME_CONFIG_BACKUPS, UNRESOLVED_ROW_GRACE_DAYS, _backup_home_config,
    _unresolved_for_days,
)


class TestUnresolvedAge(unittest.TestCase):
    def test_recent_timestamp_is_under_the_grace_period(self):
        self.assertLess(_unresolved_for_days(datetime.utcnow().isoformat()),
                        UNRESOLVED_ROW_GRACE_DAYS)

    def test_old_timestamp_is_past_the_grace_period(self):
        old = (datetime.utcnow() - timedelta(days=UNRESOLVED_ROW_GRACE_DAYS + 1)).isoformat()
        self.assertGreater(_unresolved_for_days(old), UNRESOLVED_ROW_GRACE_DAYS)

    def test_unparseable_timestamp_keeps_the_row(self):
        # 0 days elapsed => below the grace period => row is kept.
        self.assertEqual(_unresolved_for_days("not-a-date"), 0.0)
        self.assertEqual(_unresolved_for_days(None), 0.0)


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "user.json"
        self.path.write_text(json.dumps({"rows": [{"display_name": "Disney+ TV"}]}))

    def test_backup_copies_the_outgoing_config(self):
        _backup_home_config(self.path)
        backups = list((self.dir / "backups").glob("user-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("Disney+ TV", backups[0].read_text())

    def test_backups_are_capped(self):
        for i in range(HOME_CONFIG_BACKUPS + 5):
            # Distinct names — the real stamp has 1-second resolution.
            (self.dir / "backups").mkdir(exist_ok=True)
            (self.dir / "backups" / f"user-2026010100{i:04d}.json").write_text("{}")
        _backup_home_config(self.path)
        self.assertLessEqual(len(list((self.dir / "backups").glob("user-*.json"))),
                             HOME_CONFIG_BACKUPS)

    def test_missing_file_is_a_no_op(self):
        _backup_home_config(self.dir / "absent.json")  # must not raise


if __name__ == "__main__":
    unittest.main()


class TestWriteHomeConfigRowPreservation(unittest.TestCase):
    """End-to-end over write_home_config's row loop, with the disk/DB mocked."""

    def setUp(self):
        from unittest import mock
        import models.database as mdb
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        self.mock = mock
        self.tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.config_path = self.tmp / "user.json"

    def _run(self, existing_rows, smartlists):
        """write_home_config with its disk lookups stubbed out."""
        import services.smartlists as sl
        existing = {"hero": None, "rows": existing_rows, "toolbar": None}
        self.config_path.write_text(json.dumps(existing))
        with self.mock.patch.object(sl, "_get_smartlists_with_playlist_ids", return_value=smartlists), \
             self.mock.patch.object(sl, "get_home_config", return_value=existing), \
             self.mock.patch.object(sl, "_user_home_config_path", return_value=self.config_path), \
             self.mock.patch.object(sl, "get_setting", return_value="20"), \
             self.mock.patch.object(sl, "bump_playlist_version"):
            return sl.write_home_config(self.db, user_id=1)

    def test_unresolvable_row_is_kept_not_dropped(self):
        rows = [{"type": "playlist", "playlist_id": "stale", "display_name": "Disney+ TV", "order": 1}]
        # The SmartList exists but under a different name, so the row can't resolve.
        config = self._run(rows, [{"playlist_id": "other", "name": "Netflix TV"}])
        kept = [r for r in config["rows"] if r.get("display_name") == "Disney+ TV"]
        self.assertEqual(len(kept), 1)
        self.assertIn("unresolved_since", kept[0])

    def test_row_resolving_again_clears_the_marker(self):
        rows = [{"type": "playlist", "playlist_id": "stale", "display_name": "Disney+ TV",
                 "order": 1, "unresolved_since": datetime.utcnow().isoformat()}]
        config = self._run(rows, [{"playlist_id": "new-id", "name": "Disney+ TV"}])
        row = config["rows"][0]
        self.assertEqual(row["playlist_id"], "new-id")   # remapped by name
        self.assertNotIn("unresolved_since", row)

    def test_long_unresolvable_row_is_finally_dropped(self):
        old = (datetime.utcnow() - timedelta(days=UNRESOLVED_ROW_GRACE_DAYS + 1)).isoformat()
        rows = [
            {"type": "playlist", "playlist_id": "a", "display_name": "Keep", "order": 1},
            {"type": "playlist", "playlist_id": "gone", "display_name": "Old",
             "order": 2, "unresolved_since": old},
        ]
        config = self._run(rows, [{"playlist_id": "a", "name": "Keep"}])
        self.assertEqual([r["display_name"] for r in config["rows"]], ["Keep"])

    def test_ambiguous_name_keeps_the_row_rather_than_discarding_it(self):
        # Duplicate playlists make the display name ambiguous, so the remap
        # branch can't fire — that used to discard the row.
        rows = [{"type": "playlist", "playlist_id": "stale", "display_name": "Downloaded TV", "order": 1}]
        config = self._run(rows, [
            {"playlist_id": "dup1", "name": "Downloaded TV"},
            {"playlist_id": "dup2", "name": "Downloaded TV"},
        ])
        self.assertEqual(len(config["rows"]), 1)
        self.assertEqual(config["rows"][0]["playlist_id"], "stale")

    def test_builtin_rows_no_longer_pad_the_safety_check(self):
        # 3 playlist rows + 2 built-ins. Losing 2 of 3 playlist rows is more
        # than half of them, so the guard must fire and keep the old config.
        old = (datetime.utcnow() - timedelta(days=UNRESOLVED_ROW_GRACE_DAYS + 1)).isoformat()
        rows = [
            {"type": "builtin", "section_id": "resumevideo", "order": 1},
            {"type": "builtin", "section_id": "nextup", "order": 2},
            {"type": "playlist", "playlist_id": "a", "display_name": "Keep", "order": 3},
            {"type": "playlist", "playlist_id": "b", "display_name": "Lost1", "order": 4,
             "unresolved_since": old},
            {"type": "playlist", "playlist_id": "c", "display_name": "Lost2", "order": 5,
             "unresolved_since": old},
        ]
        config = self._run(rows, [{"playlist_id": "a", "name": "Keep"}])
        self.assertEqual(len(config["rows"]), 5)  # unchanged — guard fired
