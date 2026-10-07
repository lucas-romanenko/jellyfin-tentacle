"""A sort picked for a built-in playlist survives the next playlist sync.

Run from the tentacle/ directory:  python -m unittest discover -s tests

_migrate_builtin_sort_defaults() switches a built-in playlist ("Recently Added
Movies/TV", "Downloaded Movies/TV", "<Name>'s Downloads") that is still sorted
by ReleaseDate to DateCreated Descending: a one-time fix for playlists made
before the built-ins had a default sort of their own, kept to one run by the
config's `_sort_migrated` mark. But sync_smartlists() rebuilds every config and
copies back only PRESERVED_FIELDS, which did not include the mark, and new
configs were written without it. So the migration ran at every full sync, and
a user who picked "Newest First" or "Oldest First" (ReleaseDate) for one of
these playlists got DateCreated Descending back at the next one (the nightly
run, Resync All, a deleted provider, a sign-in after a rename in Jellyfin, the
YouTube check publishing new videos or a removed channel). Every new config now carries the mark and every
rebuild keeps it; a config an older version left without it is still migrated,
once, even when a fast path (a playlist switched on) rebuilt it first.
"""
import contextlib
import json
import os
import random
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

import models.database as mdb
from services import smartlists

JF_USER = "a" * 32
BUILTINS = {
    "builtin:recently_added_movies": "Recently Added Movies",
    "builtin:recently_added_tv": "Recently Added TV",
    "builtin:downloaded_movies": "Downloaded Movies",
    "builtin:downloaded_tv": "Downloaded TV",
    "builtin:my_downloads": "Alex's Downloads",
}
DEFAULT = ("DateCreated", "Descending")
# dashboard value -> what the config stores
SORTS = [("releasedate", "ReleaseDate"), ("name", "SortName"), ("datecreated", "DateCreated"),
         ("communityrating", "CommunityRating"), ("random", "Random")]


class _Playlists(unittest.TestCase):
    def setUp(self):
        root = temp_dir(self)
        engine = create_engine(f"sqlite:///{root}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "smartlists_path", f"{root}/smartlists")
        user = mdb.TentacleUser(jellyfin_user_id=JF_USER, display_name="Alex")
        self.db.add(user)
        self.db.commit()
        self.user_id = user.id
        # "<Name>'s Downloads" is made only while the user has a request.
        self.db.add(mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.user_id))
        for key in BUILTINS:
            self.db.add(mdb.AutoPlaylistToggle(user_id=self.user_id, key=key, enabled=True))
        self.db.commit()
        # No Jellyfin is set up, so a sync reaches nothing; stub what a sort
        # change does after it has saved the config.
        for name in ("refresh_smartlist_playlists", "write_home_config", "_notify_jellyfin_plugin"):
            p = mock.patch.object(smartlists, name, return_value={})
            p.start()
            self.addCleanup(p.stop)
        self.path = Path(root) / "smartlists" / JF_USER

    def sync(self):
        smartlists.sync_smartlists(self.db, user_id=self.user_id)

    def pick(self, name, sort_by, order):
        r = smartlists.update_playlist_sort(name, sort_by, order, self.db, user_id=self.user_id)
        self.assertTrue(r["success"], r)

    def configs(self):
        out = {}
        for folder in self.path.iterdir():
            cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
            out[cfg["Name"]] = cfg
        return out

    def sort(self, name):
        opt = self.configs()[name]["Order"]["SortOptions"][0]
        return opt["SortBy"], opt["SortOrder"]

    @contextlib.contextmanager
    def jellyfin(self):
        """A Jellyfin URL is set (the fast toggle needs one); the calls that
        would reach it are stubbed."""
        mdb.set_setting(self.db, "jellyfin_url", "http://127.0.0.1:9")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        with mock.patch.object(smartlists, "_create_jellyfin_playlist", return_value="pl-1"), \
                mock.patch.object(smartlists, "_process_single_playlist"), \
                mock.patch.object(smartlists, "_mark_linked_playlists", return_value=0), \
                mock.patch("routers.collections.sync_playlist_artwork", return_value={}):
            yield

    def switch_on(self, key):
        """The Playlists page's toggle: POST /api/smartlists/auto-playlists/toggle."""
        r = smartlists.toggle_auto_playlist_fast(self.db, self.user_id, key, True)
        self.assertTrue(r.get("success"), r)


class ReleaseDateChoiceKept(_Playlists):
    def test_a_release_date_pick_survives_the_next_syncs(self):
        self.sync()  # makes the playlists
        ids = {name: cfg["Id"] for name, cfg in self.configs().items()}
        for name in BUILTINS.values():
            # The first pick comes right after the playlist was made, the
            # second after it has been through syncs.
            for order in ("Ascending", "Descending"):
                with self.subTest(playlist=name, order=order):
                    self.pick(name, "releasedate", order)
                    self.assertEqual(("ReleaseDate", order), self.sort(name))
                    self.sync()
                    self.sync()
                    self.assertEqual(("ReleaseDate", order), self.sort(name),
                                     "the user's Release date sort was reverted by a sync")
        self.assertEqual(ids, {name: cfg["Id"] for name, cfg in self.configs().items()},
                         "a sync made a playlist again")

    def test_every_other_sort_was_already_kept(self):
        self.sync()
        for sort_by, stored in SORTS[1:]:
            for order in ("Ascending", "Descending"):
                with self.subTest(sort_by=sort_by, order=order):
                    self.pick("Downloaded Movies", sort_by, order)
                    self.sync()
                    self.assertEqual((stored, order), self.sort("Downloaded Movies"))

    def test_new_playlists_keep_their_default_and_the_mark(self):
        self.sync()
        for _ in range(3):
            for name, cfg in self.configs().items():
                with self.subTest(playlist=name):
                    self.assertEqual(DEFAULT, self.sort(name))
                    self.assertIs(True, cfg.get("_sort_migrated"),
                                  "no mark, so the one-time migration runs again")
            self.sync()

    def test_a_playlist_switched_on_from_the_playlists_page_keeps_a_release_date_pick(self):
        with self.jellyfin():
            self.switch_on("builtin:downloaded_tv")  # makes the config
            self.pick("Downloaded TV", "releasedate", "Descending")
            self.sync()
            self.assertEqual(("ReleaseDate", "Descending"), self.sort("Downloaded TV"))
            self.switch_on("builtin:downloaded_tv")  # rebuilds the config on disk
            self.sync()
        self.assertEqual(("ReleaseDate", "Descending"), self.sort("Downloaded TV"))


class Upgrade(_Playlists):
    def rewrite(self, name, sort_by):
        """The config as an older version left it: no mark, the given sort."""
        for folder in self.path.iterdir():
            f = folder / "config.json"
            cfg = json.loads(f.read_text(encoding="utf-8"))
            if cfg["Name"] == name:
                cfg.pop("_sort_migrated", None)
                cfg["Order"] = {"SortOptions": [{"SortBy": sort_by, "SortOrder": "Descending"}]}
                f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def test_an_old_release_date_default_is_still_switched_once(self):
        self.sync()
        for name in BUILTINS.values():
            self.rewrite(name, "ReleaseDate")
        self.rewrite("Downloaded TV", "SortName")  # a pick the user had made
        self.sync()
        for name in BUILTINS.values():
            with self.subTest(playlist=name):
                want = ("SortName", "Descending") if name == "Downloaded TV" else DEFAULT
                self.assertEqual(want, self.sort(name))
                self.assertIs(True, self.configs()[name].get("_sort_migrated"))
        # From then on a Release date pick holds.
        self.pick("Downloaded Movies", "releasedate", "Ascending")
        self.sync()
        self.assertEqual(("ReleaseDate", "Ascending"), self.sort("Downloaded Movies"))

    def test_an_old_config_switched_on_before_the_first_sync_is_still_switched_once(self):
        """The fast toggle rebuilds a config that is still on disk without
        running the migration. It must not mark an old config as migrated, or
        its old ReleaseDate default would stay for good."""
        self.sync()
        self.rewrite("Downloaded Movies", "ReleaseDate")
        with self.jellyfin():
            self.switch_on("builtin:downloaded_movies")
            self.assertNotIn("_sort_migrated", self.configs()["Downloaded Movies"])
            self.sync()
            self.assertEqual(DEFAULT, self.sort("Downloaded Movies"))
            self.assertIs(True, self.configs()["Downloaded Movies"].get("_sort_migrated"))
            self.pick("Downloaded Movies", "releasedate", "Ascending")
            self.switch_on("builtin:downloaded_movies")
            self.sync()
        self.assertEqual(("ReleaseDate", "Ascending"), self.sort("Downloaded Movies"))


class Property(_Playlists):
    def test_property_the_last_pick_always_holds(self):
        """Random sort picks, syncs and switching playlists off and on, by a
        full sync or by the Playlists page's fast toggle (200 seeds here,
        SORT_PROPERTY_SEEDS=1000 for more). After every step each playlist has
        the user's last pick, or its default if there was none since it was
        made."""
        toggles = {t.key: t for t in self.db.query(mdb.AutoPlaylistToggle).all()}
        with self.jellyfin():
            for seed in range(1, int(os.environ.get("SORT_PROPERTY_SEEDS", "200")) + 1):
                rnd = random.Random(seed)
                shutil.rmtree(self.path, ignore_errors=True)
                for t in toggles.values():
                    t.enabled = True
                self.db.commit()
                self.sync()
                expected = {name: DEFAULT for name in BUILTINS.values()}
                steps = []
                for _ in range(6):
                    op = rnd.random()
                    if op < 0.5:
                        name = rnd.choice(sorted(expected))
                        sort_by, stored = rnd.choice(SORTS)
                        order = rnd.choice(("Ascending", "Descending"))
                        self.pick(name, sort_by, order)
                        expected[name] = (stored, order)
                        steps.append(f"pick {name} {sort_by} {order}")
                    elif op < 0.8 or len(expected) < 2:
                        self.sync()
                        steps.append("sync")
                    else:
                        key = rnd.choice(sorted(toggles))
                        name = BUILTINS[key]
                        # on again while on: the fast toggle rebuilds the config
                        on = name not in expected or rnd.random() < 0.25
                        toggles[key].enabled = on
                        self.db.commit()
                        if rnd.random() < 0.5:
                            self.sync()
                            how = "sync"
                        else:
                            r = smartlists.toggle_auto_playlist_fast(self.db, self.user_id, key, on)
                            self.assertTrue(r.get("success"), r)
                            how = "toggle"
                        if not on:
                            expected.pop(name)
                        elif name not in expected:
                            expected[name] = DEFAULT  # made again
                        steps.append(f"{'on' if on else 'off'} {name} ({how})")
                    for name, want in expected.items():
                        self.assertEqual(want, self.sort(name), f"seed {seed}: {steps}")


if __name__ == "__main__":
    unittest.main()
