"""A Jellyfin user who is renamed keeps their "<name>'s Downloads" (#454).

The sign-in that brings the new name used to leave the old playlist to the
orphan sweep: it was deleted with its home row and hero, an empty one was
made under the new name, and the old tag stayed on every requested title.
Based on therobmilne's test in the issue.
"""
import html
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

import services.jellyfin as jellyfin_mod
import services.smartlists as sl
from models.database import (AutoPlaylistToggle, Base, DownloadRequest, Movie, Setting,
                             TentacleUser)
from routers import auth as auth_router
from services.nfo import update_nfo_tags
from services.tagger import dynamic_tags, retired_tags, tentacle_owned_tags
from tmp_dirs import temp_dir

OWNER, BOB = "a" * 32, "b" * 32


def _nfo_tags(path: Path) -> list:
    return [html.unescape(t) for t in re.findall(r"<tag>(.*?)</tag>", path.read_text())]


class _FakeJellyfin:
    deleted = []
    renamed = []

    def __init__(self, *a, **k):
        pass

    def delete_tentacle_playlist(self, playlist_id, user_id):
        _FakeJellyfin.deleted.append(playlist_id)
        return True

    def rename_tentacle_playlist(self, playlist_id, name, user_id=None):
        _FakeJellyfin.renamed.append((playlist_id, name))
        return True


class RenamedUserKeepsMyDownloads(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jellyfin.invalid:8096"), ("jellyfin_api_key", "k" * 32),
                     ("jellyfin_user_id", OWNER), ("jellyfin_user_name", "Owner"),
                     ("session_secret", "s" * 64),
                     ("smartlists_path", str(self.tmp / "smartlists"))):
            self.db.add(Setting(key=k, value=v))
        self.db.add(TentacleUser(jellyfin_user_id=OWNER, display_name="Owner", is_admin=True))
        bob = TentacleUser(jellyfin_user_id=BOB, display_name="Bob", is_admin=False)
        self.db.add(bob)
        self.db.flush()
        self.bob_id = bob.id
        self.nfo = self.tmp / "The Matrix (1999).nfo"
        self.nfo.write_text("<movie>\n  <title>The Matrix</title>\n  <tag>Downloaded Movies</tag>\n"
                            "  <tag>Bob's Downloads</tag>\n  <tag>by hand</tag>\n</movie>\n")
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="radarr", nfo_path=str(self.nfo),
                          tags=["Downloaded Movies", "Bob's Downloads"]))
        # Someone else's request keeps its own tag.
        self.db.add(Movie(tmdb_id=604, title="The Matrix Reloaded", source="radarr",
                          tags=["Downloaded Movies", "Owner's Downloads"]))
        self.db.add(DownloadRequest(tmdb_id=603, media_type="movie", user_id=bob.id))
        self.db.add(AutoPlaylistToggle(user_id=bob.id, key="builtin:my_downloads", enabled=True))
        self.db.commit()

        self.created = []
        _FakeJellyfin.deleted = []
        _FakeJellyfin.renamed = []
        self.deleted = _FakeJellyfin.deleted
        self.renamed = _FakeJellyfin.renamed
        self.followed = []

        def create(name, *a, **k):
            self.created.append(name)
            return f"pl-{len(self.created)}"

        home_dir = self.tmp / "home-configs"
        home_dir.mkdir()
        for p in (mock.patch.object(sl, "_create_jellyfin_playlist", create),
                  mock.patch.object(sl, "_mark_linked_playlists", lambda *a, **k: 0),
                  mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db: {}),
                  mock.patch.object(jellyfin_mod, "JellyfinService", _FakeJellyfin),
                  mock.patch.object(sl, "_user_home_config_path",
                                    lambda db, user_id=None: home_dir / f"{BOB}.json"),
                  mock.patch.object(auth_router, "_build_playlists_for_new_user", lambda uid: None),
                  mock.patch.object(auth_router, "_follow_user_rename",
                                    lambda *a: self.followed.append(a))):
            p.start()
            self.addCleanup(p.stop)
        self.home_path = home_dir / f"{BOB}.json"

        # Bob's playlist exists, with a home row and the hero on it.
        sl.sync_smartlists(self.db, user_id=self.bob_id)
        self.smartlists = self.tmp / "smartlists" / BOB
        folder, cfg = sl._scan_existing(self.smartlists)["Bob's Downloads"]
        self.old_folder = folder
        self.old_pid = cfg["UserPlaylists"][0]["JellyfinPlaylistId"]
        self.home_path.write_text(json.dumps({
            "hero": {"enabled": True, "playlist_id": self.old_pid, "display_name": "Bob's Downloads",
                     "sort_by": "random", "sort_order": "Descending", "require_logo": True,
                     "require_trailer": False, "trailer_audio": False, "item_count": 10},
            "rows": [{"type": "playlist", "playlist_id": self.old_pid,
                      "display_name": "Bob's Downloads", "order": 1}],
            "toolbar": [{"id": "search", "enabled": True}],
        }))
        self.created.clear()

    def _login(self, name):
        def post(*args, **kwargs):
            r = mock.Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = {"User": {"Id": BOB, "Name": kwargs["json"]["Username"],
                                            "Policy": {"IsAdministrator": False}}}
            return r
        request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        with mock.patch.object(auth_router.requests, "post", post):
            auth_router.login(auth_router.LoginRequest(username=name, password="x"),
                              Response(), request, self.db)
        self.db.expire_all()

    def test_sync_after_rename_renames_the_playlist_in_place(self):
        self._login("Robert")
        sl.sync_smartlists(self.db, user_id=self.bob_id)
        self.assertEqual([], self.deleted, f"playlist deleted: {self.deleted}; created: {self.created}")
        self.assertEqual([], self.created)
        existing = sl._scan_existing(self.smartlists)
        self.assertEqual(["Robert's Downloads"], list(existing))
        folder, cfg = existing["Robert's Downloads"]
        self.assertEqual(self.old_folder, folder)
        self.assertEqual(self.old_pid, cfg["UserPlaylists"][0]["JellyfinPlaylistId"])
        self.assertEqual("Robert's Downloads", cfg["ExpressionSets"][0]["Expressions"][0]["TargetValue"])
        self.assertEqual([(self.old_pid, "Robert's Downloads")], self.renamed)

    def test_home_row_and_hero_follow_the_rename(self):
        self._login("Robert")
        sl.sync_smartlists(self.db, user_id=self.bob_id)
        sl.write_home_config(self.db, user_id=self.bob_id)
        home = json.loads(self.home_path.read_text())
        self.assertTrue(home["hero"].get("enabled"), f"hero switched off by the rename: {home['hero']}")
        self.assertEqual((self.old_pid, "Robert's Downloads"),
                         (home["hero"]["playlist_id"], home["hero"]["display_name"]))
        row = [r for r in home["rows"] if r.get("type") == "playlist"][0]
        self.assertNotIn("unresolved_since", row, f"home row left pointing at a deleted playlist: {row}")
        self.assertEqual((self.old_pid, "Robert's Downloads"), (row["playlist_id"], row["display_name"]))

    def test_a_session_loaded_before_the_sign_in_reads_the_new_name(self):
        # The nightly loads every user, then syncs them one by one.
        nightly = self.Session()
        self.addCleanup(nightly.close)
        nightly.query(TentacleUser).all()
        self._login("Robert")
        sl.sync_smartlists(nightly, user_id=self.bob_id)
        self.assertEqual([], self.deleted)
        self.assertEqual(["Robert's Downloads"], list(sl._scan_existing(self.smartlists)))

    def test_old_downloads_tag_is_still_tentacles_and_comes_off(self):
        self._login("Robert")
        owned = tentacle_owned_tags(self.db)
        self.assertIn("Bob's Downloads", owned, "the pre-rename tag now reads as somebody else's")
        nfo = self.tmp / "movie.nfo"
        nfo.write_text("<movie>\n  <title>The Matrix</title>\n  <tag>Bob's Downloads</tag>\n</movie>\n")
        update_nfo_tags(nfo, ["Robert's Downloads"], owned=owned)
        self.assertNotIn("Bob's Downloads", _nfo_tags(nfo))

    def test_sign_in_follows_the_rename_in_the_background(self):
        self._login("Robert")
        self.assertEqual([(self.bob_id, "Bob", "Robert")], self.followed)

    def test_same_name_sign_in_changes_nothing(self):
        self._login("Bob")
        self.assertEqual([], self.followed)
        self.assertEqual(set(), retired_tags(self.db))

    def test_follow_up_moves_the_tag_on_the_requested_titles(self):
        self._login("Robert")
        pushed = []
        with mock.patch.object(jellyfin_mod, "sync_owned_tags",
                               lambda db, jf, prefix, only=None: pushed.append(only) or
                               {"written": 1, "unchanged": 0, "not_found": 0, "errors": 0}):
            auth_router.follow_user_rename(self.db, self.bob_id, "Bob", "Robert")
        self.db.expire_all()
        mine = self.db.query(Movie).filter(Movie.tmdb_id == 603).one()
        theirs = self.db.query(Movie).filter(Movie.tmdb_id == 604).one()
        self.assertEqual(["Downloaded Movies", "Robert's Downloads"], mine.tags)
        self.assertEqual(["Downloaded Movies", "Owner's Downloads"], theirs.tags)
        self.assertEqual({"by hand", "Downloaded Movies", "Robert's Downloads"}, set(_nfo_tags(self.nfo)))
        self.assertEqual([{"Movie": {603}, "Series": set()}], pushed)
        # ...and the playlist followed, home config included.
        self.assertEqual(["Robert's Downloads"], list(sl._scan_existing(self.smartlists)))
        self.assertEqual([], self.deleted)
        home = json.loads(self.home_path.read_text())
        self.assertEqual("Robert's Downloads", home["hero"]["display_name"])

    def test_a_later_user_given_the_old_name_keeps_their_tag(self):
        self._login("Robert")
        self.assertIn("Bob's Downloads", dynamic_tags(self.db))
        self.db.add(TentacleUser(jellyfin_user_id="c" * 32, display_name="Bob", is_admin=False))
        self.db.commit()
        self.assertNotIn("Bob's Downloads", dynamic_tags(self.db))


if __name__ == "__main__":
    unittest.main()
