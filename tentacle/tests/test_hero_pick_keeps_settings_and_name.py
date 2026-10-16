"""Picking a hero playlist keeps the hero's other settings, and names a plain
Jellyfin playlist by its Jellyfin name.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Found in an end-to-end run against a live test install (dashboard API and the
plugin's /TentacleHome/HeroConfig):

1. POST /api/smartlists/hero rebuilds the hero dict from a fixed key list that
   has no "trailer_audio". A user who turned the hero trailer's sound on
   (POST /api/smartlists/hero-sort, trailer_audio=true) and then picks another
   playlist gets trailer_audio dropped -- the plugin then serves
   "trailerAudio": false. Every other setting (sort, logo, trailer, count) is kept.

2. A playlist that is neither a home row nor one of Tentacle's own playlists --
   a playlist the user made in Jellyfin, which the plugin's POST
   /TentacleHome/Hero passes through from clients -- is stored with
   display_name = its id, and the plugin serves the 32-hex id as the hero's
   name ("displayName": "4ac3ad9b..."). Jellyfin knows the name; ask it.
"""
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import routers.smartlists as rsl
except Exception:  # pragma: no cover
    rsl = None


def _db():
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db")
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1")
    db.add(user)
    db.commit()
    return db, user


@unittest.skipIf(rsl is None, "fastapi not installed")
class HeroPick(unittest.TestCase):
    def _pick(self, playlist_id, existing, smartlists=(), jellyfin_name=None):
        db, user = _db()
        written = {}
        with mock.patch.object(rsl, "_read_home_json", lambda u: dict(existing)), \
                mock.patch.object(rsl, "_write_home_json", lambda u, c: written.update(c)), \
                mock.patch.object(rsl, "_notify_jellyfin_plugin", lambda d: None), \
                mock.patch.object(rsl, "bump_playlist_version", lambda: None), \
                mock.patch.object(rsl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: list(smartlists)), \
                mock.patch.object(rsl, "_jellyfin_item_name", lambda db, user, item_id: jellyfin_name, create=True):
            out = rsl.set_hero(rsl.HeroPickRequest(playlist_id=playlist_id), db, user)
        return out, written

    def test_picking_another_playlist_keeps_trailer_audio(self):
        existing = {"rows": [], "hero": {"enabled": True, "playlist_id": "a" * 32, "display_name": "Old",
                                         "sort_by": "random", "sort_order": "Descending", "require_logo": True,
                                         "require_trailer": True, "trailer_audio": True, "item_count": 15}}
        _, written = self._pick("b" * 32, existing, smartlists=[{"playlist_id": "b" * 32, "name": "New"}])
        hero = written["hero"]
        self.assertEqual("New", hero["display_name"])
        self.assertTrue(hero.get("trailer_audio"), f"trailer_audio dropped: {hero}")
        self.assertTrue(hero["require_trailer"])
        self.assertEqual(15, hero["item_count"])

    def test_a_plain_jellyfin_playlist_gets_its_jellyfin_name_not_its_id(self):
        _, written = self._pick("c" * 32, {"rows": []}, smartlists=[], jellyfin_name="Family picks")
        self.assertEqual("Family picks", written["hero"]["display_name"])

    def test_without_a_name_from_jellyfin_the_id_is_still_the_last_resort(self):
        _, written = self._pick("d" * 32, {"rows": []}, smartlists=[], jellyfin_name=None)
        self.assertEqual("d" * 32, written["hero"]["display_name"])

    def test_turning_the_hero_off_still_resets_it(self):
        _, written = self._pick("", {"rows": [], "hero": {"enabled": True, "playlist_id": "a" * 32,
                                                          "trailer_audio": True}})
        self.assertFalse(written["hero"]["enabled"])
        self.assertEqual("", written["hero"]["playlist_id"])


if __name__ == "__main__":
    unittest.main()
