"""#120 (4): a user with no playlists still gets a home config.

write_home_config() returned early for any user without smart playlists, so a
new household member or a fresh install's owner had no home config — and so
no toolbar — until the nightly job. It now writes the same starter config the
first plugin read would (Jellyfin's home sections as built-in rows + the
default toolbar), but still never touches a config that already exists.

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
import routers.smartlists as rsl
import services.jellyfin as jellyfin
import services.smartlists as sl


class FakeJf:
    def __init__(self, sections):
        self.sections = sections
        self.disabled = 0

    def __call__(self, *a, **k):
        return self

    def get_home_sections(self):
        return self.sections

    def disable_home_sections(self):
        self.disabled += 1
        return {}


class StarterConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jfqa2", display_name="qa2"))
        self.db.commit()
        self.home = self.tmp / "home-configs"
        self.home.mkdir()
        self.file = self.home / "jfqa2.json"
        settings = {"jellyfin_url": "http://jf", "jellyfin_api_key": "k",
                    "smartlists_path": str(self.tmp / "smartlists")}
        get = lambda db, k, d="": settings.get(k, d)  # noqa: E731
        self.jf = FakeJf({f"homesection{i}": "" for i in range(10)})
        for p in (
            mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.home)),
            mock.patch.object(sl, "_user_home_config_path", lambda db, uid=None: self.file),
            mock.patch.object(sl, "get_setting", side_effect=get),
            mock.patch.object(rsl, "get_setting", side_effect=get),
            mock.patch.object(jellyfin, "JellyfinService", self.jf),
            mock.patch.object(sl, "bump_playlist_version"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_no_playlists_no_config_writes_a_starter(self):
        config = sl.write_home_config(self.db, user_id=1)
        self.assertTrue(self.file.exists())
        on_disk = json.loads(self.file.read_text())
        self.assertEqual(config["rows"], on_disk["rows"])
        self.assertEqual([r["section_id"] for r in on_disk["rows"]], rsl.DEFAULT_SEED_SECTIONS)
        self.assertTrue(all(r["type"] == "builtin" for r in on_disk["rows"]))
        ids = {b["id"]: b["enabled"] for b in on_disk["toolbar"]}
        self.assertTrue(ids["search"] and ids["libraries"])
        self.assertFalse(on_disk["hero"]["enabled"])
        self.assertEqual(1, self.jf.disabled, "native sections blanked so they don't double-render")

    def test_their_own_jellyfin_sections_are_mirrored(self):
        self.jf.sections = dict(self.jf.sections, homesection0="nextup", homesection1="resume")
        sl.write_home_config(self.db, user_id=1)
        rows = json.loads(self.file.read_text())["rows"]
        self.assertEqual("nextup", rows[0]["section_id"])

    def test_an_existing_config_is_never_rewritten_from_an_empty_lookup(self):
        mine = {"hero": {"enabled": True, "playlist_id": "p"},
                "rows": [{"type": "playlist", "playlist_id": "p", "display_name": "Mine"}]}
        self.file.write_text(json.dumps(mine))
        self.assertEqual({}, sl.write_home_config(self.db, user_id=1))
        self.assertEqual(mine, json.loads(self.file.read_text()))

    def test_jellyfin_unreachable_writes_nothing(self):
        self.jf.sections = {}
        self.assertEqual({}, sl.write_home_config(self.db, user_id=1))
        self.assertFalse(self.file.exists(), "a blank file would stop the plugin's own seeding")

    def test_the_plugin_read_path_seed_carries_the_toolbar_too(self):
        user = self.db.query(mdb.TentacleUser).first()
        config = rsl._seed_home_config_from_jellyfin(self.db, user)
        self.assertEqual(rsl.DEFAULT_TOOLBAR, config["toolbar"])


if __name__ == "__main__":
    unittest.main()
