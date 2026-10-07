"""A sort the user picks on a built-in playlist stays across full syncs.

Built-ins get their default sort (DateCreated) only when they are created;
a sync never rewrites the `Order` a user picked. A one-time migration that
turned ReleaseDate sorts back into DateCreated ran at every full sync
(nightly, Resync All, the YouTube check...), because its mark was not in
PRESERVED_FIELDS, so "Newest First" / "Oldest First" on Downloaded Movies,
Downloaded TV and <Name>'s Downloads went back to "Recently Added".
Issue #534.

Run from the tentacle/ directory:  python tests/hermetic.py discover -s tests
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import smartlists
from tmp_dirs import temp_dir

JF_USER = "a" * 32


class BuiltinSortKeptAcrossSyncs(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.root}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "smartlists_path", f"{self.root}/smartlists")
        self.user = mdb.TentacleUser(jellyfin_user_id=JF_USER, display_name="Alex")
        self.db.add(self.user)
        self.db.commit()
        for key in ("builtin:downloaded_movies", "builtin:downloaded_tv", "builtin:my_downloads"):
            self.db.add(mdb.AutoPlaylistToggle(user_id=self.user.id, key=key, enabled=True))
        # "<Name>'s Downloads" exists once the user has requested something.
        self.db.add(mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.user.id))
        self.db.commit()
        # No Jellyfin here: stub what runs after a config is saved.
        for name in ("refresh_smartlist_playlists", "write_home_config", "_notify_jellyfin_plugin"):
            p = mock.patch.object(smartlists, name, return_value={})
            p.start()
            self.addCleanup(p.stop)

    def sort_of(self, playlist):
        for cfg_file in (Path(self.root) / "smartlists" / JF_USER).glob("*/config.json"):
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            if cfg["Name"] == playlist:
                opt = cfg["Order"]["SortOptions"][0]
                return opt["SortBy"], opt["SortOrder"]
        self.fail(f"no config for {playlist!r}")

    def sync(self):
        smartlists.sync_smartlists(self.db, user_id=self.user.id)

    def pick(self, playlist, sort_by, sort_order):
        r = smartlists.update_playlist_sort(playlist, sort_by, sort_order, self.db, user_id=self.user.id)
        self.assertTrue(r["success"], r)

    def test_release_date_picks_survive_full_syncs(self):
        self.sync()  # makes the playlists with their default sort
        self.assertEqual(("DateCreated", "Descending"), self.sort_of("Downloaded Movies"))
        picks = {
            "Downloaded Movies": ("releasedate", "Descending"),   # "Newest First"
            "Downloaded TV": ("releasedate", "Ascending"),        # "Oldest First"
            "Alex's Downloads": ("releasedate", "Descending"),
        }
        for playlist, (sort_by, order) in picks.items():
            self.pick(playlist, sort_by, order)
        for _ in range(2):  # two nightly syncs
            self.sync()
            for playlist, (_, order) in picks.items():
                self.assertEqual(("ReleaseDate", order), self.sort_of(playlist),
                                 f"the sort picked on {playlist!r} was put back")


if __name__ == "__main__":
    unittest.main()
