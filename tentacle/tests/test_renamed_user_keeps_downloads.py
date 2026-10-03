"""A user renamed in Jellyfin keeps their "<name>'s Downloads".

Run from the tentacle/ directory:  python -m unittest discover -s tests

The per-user downloads playlist and its tag are named after the user's display
name, and every sign-in copies the Jellyfin name into it. When an admin renamed
Bob to Robert and he signed in again, the next sync no longer wanted
"Bob's Downloads": it deleted that playlist (in Jellyfin and on disk) and made
an empty "Robert's Downloads" with a new id. The home row on the old playlist
no longer resolved (and was dropped after the grace period), and the hero on
it was switched off at once. "Bob's Downloads" also stayed on every title he
had requested: with the name gone nothing counted it as Tentacle's, so no scan
took it off.

Now the sign-in that brings the new name retires the old tag, and the playlist
is renamed in place: same folder, same Jellyfin playlist (renamed there too),
so the row, the hero and the sort stay. A sync whose session loaded the users
before the sign-in (the nightly) reads the new name too. Someone who signs in
under the old name later keeps their own tag; the retired one doesn't take it
off.
"""
import html
import json
import re
import threading
import unittest
from datetime import datetime
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
from services.tagger import merge_owned_tags, refresh_recently_added_tags, tentacle_owned_tags
from tmp_dirs import temp_dir

OWNER, BOB, LATER = "a" * 32, "b" * 32, "c" * 32


class _FakeJellyfin:
    deleted, renamed = [], []

    def __init__(self, *a, **k):
        pass

    def delete_tentacle_playlist(self, playlist_id, user_id=None):
        _FakeJellyfin.deleted.append(playlist_id)
        return True

    def rename_tentacle_playlist(self, playlist_id, name, user_id=None):
        _FakeJellyfin.renamed.append((playlist_id, name, user_id))
        return True


class RenamedUser(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
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
        self.db.add(DownloadRequest(tmdb_id=603, media_type="movie", user_id=bob.id))
        self.db.add(AutoPlaylistToggle(user_id=bob.id, key="builtin:my_downloads", enabled=True))
        self.db.commit()

        # No network: Jellyfin playlist create/delete/rename/mark are recorded.
        self.created = []
        _FakeJellyfin.deleted, _FakeJellyfin.renamed = [], []
        self.notified = []

        def create(name, *a, **k):
            self.created.append(name)
            return f"pl-{len(self.created)}"

        home_dir = self.tmp / "home-configs"
        home_dir.mkdir()
        for p in (mock.patch.object(sl, "_create_jellyfin_playlist", create),
                  mock.patch.object(sl, "_mark_linked_playlists", lambda *a, **k: 0),
                  mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db: self.notified.append(1)),
                  mock.patch.object(jellyfin_mod, "JellyfinService", _FakeJellyfin),
                  mock.patch.object(sl, "_user_home_config_path",
                                    lambda db, user_id=None: home_dir / f"{user_id}.json"),
                  mock.patch.object(auth_router, "_build_playlists_for_new_user", lambda uid: None)):
            p.start()
            self.addCleanup(p.stop)
        self.home_path = home_dir / f"{self.bob_id}.json"

        # Bob's downloads playlist exists, with a home row and the hero on it.
        sl.sync_smartlists(self.db, user_id=self.bob_id)
        folder, cfg = self.configs()["Bob's Downloads"]
        self.folder, self.pid = folder, cfg["UserPlaylists"][0]["JellyfinPlaylistId"]
        self.assertEqual("pl-1", self.pid)
        self.home_path.write_text(json.dumps({
            "hero": {"enabled": True, "playlist_id": self.pid, "display_name": "Bob's Downloads",
                     "sort_by": "random", "sort_order": "Descending", "require_logo": True,
                     "require_trailer": False, "trailer_audio": False, "item_count": 10},
            "rows": [{"type": "playlist", "playlist_id": self.pid,
                      "display_name": "Bob's Downloads", "order": 1}],
            "toolbar": [{"id": "search", "enabled": True}],
        }))

    def configs(self):
        return sl._scan_existing(self.tmp / "smartlists" / BOB)

    def login(self, jf_id, name):
        def post(*args, **kwargs):
            r = mock.Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = {"User": {"Id": jf_id, "Name": name,
                                            "Policy": {"IsAdministrator": False}}}
            return r
        request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        with mock.patch.object(auth_router.requests, "post", post):
            auth_router.login(auth_router.LoginRequest(username=name, password="x"),
                              Response(), request, self.db)
        self.db.expire_all()

    def test_the_playlist_is_renamed_not_deleted(self):
        self.login(BOB, "Robert")
        self.assertEqual("Robert", self.db.get(TentacleUser, self.bob_id).display_name)
        configs = self.configs()
        self.assertEqual(["Robert's Downloads"], list(configs),
                         "the sign-in left the playlist under the old name, for the next sync to delete")
        folder, cfg = configs["Robert's Downloads"]
        self.assertEqual(self.folder, folder, "a new SmartList folder instead of the old one")
        self.assertEqual(self.pid, cfg["UserPlaylists"][0]["JellyfinPlaylistId"])
        self.assertEqual("Robert's Downloads", cfg["ExpressionSets"][0]["Expressions"][0]["TargetValue"])
        self.assertEqual([(self.pid, "Robert's Downloads", BOB)], _FakeJellyfin.renamed)

        result = sl.sync_smartlists(self.db, user_id=self.bob_id)
        self.assertEqual([], _FakeJellyfin.deleted, "the renamed user's Jellyfin playlist was deleted")
        self.assertEqual(["Bob's Downloads"], self.created, "a second playlist was made")
        self.assertEqual((0, 0), (result["created"], result["removed"]))
        self.assertTrue(self.folder.exists())

    def test_the_home_row_and_the_hero_stay_on_it(self):
        self.login(BOB, "Robert")
        sl.sync_smartlists(self.db, user_id=self.bob_id)
        sl.write_home_config(self.db, user_id=self.bob_id)
        home = json.loads(self.home_path.read_text())
        self.assertTrue(home["hero"]["enabled"], f"hero switched off by the rename: {home['hero']}")
        self.assertEqual((self.pid, "Robert's Downloads"),
                         (home["hero"]["playlist_id"], home["hero"]["display_name"]))
        row = [r for r in home["rows"] if r.get("type") == "playlist"]
        self.assertEqual(1, len(row))
        self.assertNotIn("unresolved_since", row[0], f"home row left on a deleted playlist: {row[0]}")
        self.assertEqual((self.pid, "Robert's Downloads"), (row[0]["playlist_id"], row[0]["display_name"]))
        self.assertTrue(self.notified, "the clients were not told")

    def nightly(self):
        # The nightly loads every user, then syncs them one by one with no
        # commit in between: Bob signs in under his new name while it works
        # on the owner.
        nightly = sessionmaker(bind=self.db.get_bind())()
        self.addCleanup(nightly.close)
        for user in nightly.query(TentacleUser).order_by(TentacleUser.id).all():
            if user.id == self.bob_id:
                self.login(BOB, "Robert")
            sl.sync_smartlists(nightly, user_id=user.id)
            sl.write_home_config(nightly, user_id=user.id)

    def test_a_nightly_that_loaded_the_users_earlier_keeps_it(self):
        self.nightly()
        self.assertEqual([], _FakeJellyfin.deleted, "the nightly still wanted the old name")
        self.assertEqual(["Robert's Downloads"], list(self.configs()))
        hero = json.loads(self.home_path.read_text())["hero"]
        self.assertEqual((True, self.pid), (hero["enabled"], hero["playlist_id"]))

    def test_a_nightly_keeps_it_while_the_user_has_no_requests(self):
        # With no request left only the switched-on toggle keeps the playlist
        # (another playlist is wanted, so the all-orphans guard doesn't).
        self.db.query(DownloadRequest).delete()
        self.db.add(AutoPlaylistToggle(user_id=self.bob_id, key="builtin:recently_added_movies",
                                       enabled=True))
        self.db.commit()
        self.nightly()
        self.assertEqual([], _FakeJellyfin.deleted, "the nightly still protected the old name")
        self.assertIn("Robert's Downloads", self.configs())

    def test_the_old_tag_comes_off_the_titles(self):
        self.login(BOB, "Robert")
        owned = tentacle_owned_tags(self.db)
        self.assertIn("Robert's Downloads", owned)
        self.assertIn("Bob's Downloads", owned, "the old tag reads as somebody else's and stays for ever")

        # What the Radarr scan writes (.strm NFO) and pushes (Jellyfin API) now.
        nfo = self.tmp / "movie.nfo"
        nfo.write_text("<movie>\n  <title>The Matrix</title>\n  <tag>Bob's Downloads</tag>\n"
                       "  <tag>date-night</tag>\n</movie>\n", encoding="utf-8")
        update_nfo_tags(nfo, ["Robert's Downloads"], owned=owned)
        tags = [html.unescape(t) for t in re.findall(r"<tag>(.*?)</tag>", nfo.read_text(encoding="utf-8"))]
        self.assertEqual({"date-night", "Robert's Downloads"}, set(tags))
        self.assertEqual(["date-night", "Robert's Downloads"],
                         merge_owned_tags(["Bob's Downloads", "date-night"], ["Robert's Downloads"], owned))

        # And the tag refresh takes it off a title no scan has rewritten yet.
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="radarr", date_added=datetime.utcnow(),
                          tags=["Downloaded Movies", "Bob's Downloads"]))
        self.db.commit()
        refresh_recently_added_tags(self.db)
        self.assertNotIn("Bob's Downloads", self.db.query(Movie).one().tags)

    def test_someone_signing_in_under_the_old_name_later_keeps_their_tag(self):
        self.login(BOB, "Robert")
        self.login(LATER, "Bob")   # a new Jellyfin account, given the freed name
        later = self.db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == LATER).one()
        self.db.add(DownloadRequest(tmdb_id=604, media_type="movie", user_id=later.id))
        self.db.add(Movie(tmdb_id=604, title="The Matrix Reloaded", source="radarr",
                          date_added=datetime.utcnow(), tags=["Downloaded Movies", "Bob's Downloads"]))
        self.db.commit()
        refresh_recently_added_tags(self.db)
        self.assertIn("Bob's Downloads", self.db.query(Movie).filter(Movie.tmdb_id == 604).one().tags,
                      "the retired name took the new user's own tag off their request")
        self.assertIn("Bob's Downloads", tentacle_owned_tags(self.db))

    def test_two_renames_carried_over_out_of_order(self):
        # Robert, then Rob, by two sign-ins at once: the second one's carry-over
        # runs first. The playlist still ends under the name the user has now.
        with mock.patch.object(auth_router, "_carry_over_downloads_playlist",
                               lambda db, uid, old: self.later.append((uid, old))):
            self.later = []
            self.login(BOB, "Robert")
            self.login(BOB, "Rob")
        self.assertEqual([(self.bob_id, "Bob"), (self.bob_id, "Robert")], self.later)
        for uid, old in reversed(self.later):
            sl.rename_downloads_playlist(self.db, uid, old)
        self.assertEqual(["Rob's Downloads"], list(self.configs()))
        owned = tentacle_owned_tags(self.db)
        self.assertTrue({"Bob's Downloads", "Robert's Downloads", "Rob's Downloads"} <= owned)

    def test_a_running_refresh_does_not_hold_up_the_sign_in(self):
        # Another thread holds the playlist lock (a long refresh): the SmartList
        # is renamed at once and only Jellyfin's name for it waits.
        held, done = threading.Event(), threading.Event()

        def refresh():
            with sl._playlist_refresh_lock:
                held.set()
                done.wait(5)
        t = threading.Thread(target=refresh)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        held.wait(5)
        with mock.patch.object(sl, "FAST_PATH_LOCK_TIMEOUT", 0.05):
            self.login(BOB, "Robert")
        self.assertEqual(["Robert's Downloads"], list(self.configs()))
        self.assertEqual([], _FakeJellyfin.renamed)

    def test_a_failed_carry_over_does_not_fail_the_sign_in(self):
        with mock.patch.object(sl, "rename_downloads_playlist", side_effect=OSError("disk full")):
            self.login(BOB, "Robert")
        self.assertEqual("Robert", self.db.get(TentacleUser, self.bob_id).display_name)
        self.assertIn("Bob's Downloads", tentacle_owned_tags(self.db), "the retirement was lost")

    def test_a_sign_in_under_the_same_name_changes_nothing(self):
        before = (self.folder / "config.json").read_text()
        self.login(BOB, "Bob")
        self.assertEqual(before, (self.folder / "config.json").read_text())
        self.assertIsNone(self.db.query(Setting).filter(Setting.key == "tentacle_retired_tags").first())
        self.assertEqual([], _FakeJellyfin.renamed)

    def test_a_playlist_already_under_the_new_name_is_not_overwritten(self):
        cfg = json.loads((self.folder / "config.json").read_text())
        cfg.update(Name="Robert's Downloads", Id="other")
        cfg["UserPlaylists"][0]["JellyfinPlaylistId"] = "pl-9"
        other = self.folder.parent / "other"
        other.mkdir()
        (other / "config.json").write_text(json.dumps(cfg))
        before = {n: (f / "config.json").read_text() for n, (f, _c) in self.configs().items()}
        self.db.get(TentacleUser, self.bob_id).display_name = "Robert"
        self.db.commit()
        self.assertFalse(sl.rename_downloads_playlist(self.db, self.bob_id, "Bob"))
        self.assertEqual(before, {n: (f / "config.json").read_text() for n, (f, _c) in self.configs().items()})
        self.assertEqual([], _FakeJellyfin.renamed)


class _Resp:
    def __init__(self, status=204):
        self.status_code, self.text = status, ""


class _Session:
    def __init__(self):
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        return _Resp()


class RenameTentaclePlaylist(unittest.TestCase):
    """JellyfinService.rename_tentacle_playlist, with only HTTP faked."""

    PLAYLIST = {"Id": "pl-1", "Name": "Bob's Downloads", "Type": "Playlist", "LockData": False,
                "ProviderIds": {"Tentacle": "managed"}, "Tags": []}

    def _service(self, item):
        svc = jellyfin_mod.JellyfinService("http://jf:8096", "k", "jf-1")
        svc.session = _Session()
        svc.seen = []

        def get(path, params=None):
            svc.seen.append(path)
            return dict(item) if item else None
        svc._get = get
        return svc

    def test_renames_tentacles_playlist_in_place(self):
        svc = self._service(self.PLAYLIST)
        self.assertTrue(svc.rename_tentacle_playlist("pl-1", "Robert's Downloads", "jf-bob"))
        url, payload = svc.session.posted[0]
        self.assertTrue(url.endswith("/Items/pl-1"))
        self.assertEqual("Robert's Downloads", payload["Name"])
        self.assertEqual({"Tentacle": "managed"}, payload["ProviderIds"], "the mark was lost")
        self.assertIs(False, payload["LockData"])
        self.assertEqual(["/Users/jf-bob/Items/pl-1"], svc.seen, "read as its owner sees it")

    def test_a_users_own_playlist_is_not_renamed(self):
        svc = self._service({**self.PLAYLIST, "ProviderIds": {}})
        self.assertFalse(svc.rename_tentacle_playlist("pl-1", "Robert's Downloads"))
        self.assertEqual([], svc.session.posted)

    def test_nothing_is_written_when_the_name_is_already_right_or_unreadable(self):
        svc = self._service({**self.PLAYLIST, "Name": "Robert's Downloads"})
        self.assertTrue(svc.rename_tentacle_playlist("pl-1", "Robert's Downloads"))
        svc2 = self._service(None)
        self.assertFalse(svc2.rename_tentacle_playlist("pl-1", "Robert's Downloads"))
        self.assertEqual([], svc.session.posted + svc2.session.posted)


if __name__ == "__main__":
    unittest.main()
