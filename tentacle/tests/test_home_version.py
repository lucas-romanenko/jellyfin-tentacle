"""The per-user home version (Android TV catch-up, androidtv audit H2/H3).

Every save of a user's home or toolbar bumps that user's home version; a change
that isn't one user's (a sync, a Settings save) bumps everyone's. /home-config
hands it to the plugin with the config, and the push to the plugin carries it
(with the user for a one-user save), so a TV that missed the push (another
screen, asleep, offline) sees on its next look that its home is old.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.smartlists as sl
from tmp_dirs import temp_dir

try:
    import routers.smartlists as rsl
    import routers.settings as rset
except Exception:  # pragma: no cover - depends on optional deps
    rsl = rset = None


def _reset_versions():
    sl._home_versions.clear()
    sl._home_version_floor = 0


class TestHomeVersionCounter(unittest.TestCase):
    def setUp(self):
        _reset_versions()
        self.addCleanup(_reset_versions)

    def test_a_user_save_moves_only_that_users_version(self):
        a, b = sl.get_home_version(1), sl.get_home_version(2)
        new = sl.bump_home_version(1)
        self.assertNotEqual(new, a)
        self.assertEqual(sl.get_home_version(1), new)
        self.assertEqual(sl.get_home_version(2), b)

    def test_a_change_for_everyone_moves_every_version(self):
        sl.bump_home_version(1)
        before = {u: sl.get_home_version(u) for u in (1, 2)}
        everyone = sl.bump_home_version()
        for u in (1, 2):
            self.assertNotEqual(sl.get_home_version(u), before[u])
            self.assertEqual(sl.get_home_version(u), everyone)

    def test_versions_only_go_up(self):
        seen = [sl.get_home_version(1)]
        for user in (1, None, 1, 1, None, 1):
            sl.bump_home_version(user)
            seen.append(sl.get_home_version(1))
        self.assertEqual(seen, sorted(set(seen)), "a version repeated or went down")

    def test_a_restart_never_hands_out_an_old_number(self):
        # A TV remembers the last version it loaded; after a server restart (which
        # forgets the counters) the same number must not mean a different config.
        for _ in range(5):
            sl.bump_home_version(1)
        old = sl.get_home_version(1)
        _reset_versions()
        self.assertGreater(sl.get_home_version(1), old - 5)
        self.assertGreaterEqual(sl.get_home_version(1), int(time.time() * 1000) - 1000)


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


CONFIG = {
    "hero": {"enabled": False, "playlist_id": "", "display_name": ""},
    "rows": [
        {"type": "playlist", "playlist_id": "pl-a", "display_name": "A", "order": 1},
        {"type": "playlist", "playlist_id": "pl-b", "display_name": "B", "order": 2},
    ],
}


@unittest.skipIf(rsl is None, "fastapi not installed")
class TestHomeSavesCarryTheVersion(unittest.TestCase):
    def setUp(self):
        _reset_versions()
        self.addCleanup(_reset_versions)
        self.tmp = Path(temp_dir(self))
        self.db = _session(self.tmp)
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jellyfin.local:8096"), ("jellyfin_api_key", "KEY")):
            self.db.add(mdb.Setting(key=k, value=v))
        self.user = mdb.TentacleUser(id=1, jellyfin_user_id="0123-4567-89ab", display_name="U")
        self.db.add(self.user)
        self.db.commit()
        patcher = mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.tmp / "home-configs"))
        patcher.start()
        self.addCleanup(patcher.stop)
        rsl._write_home_json(self.user, CONFIG)

    def _post(self):
        p = mock.patch("services.smartlists.requests.post")
        post = p.start()
        self.addCleanup(p.stop)
        post.return_value.ok = True
        return post

    def test_home_config_answers_with_the_users_version(self):
        answer = rsl.read_home(db=self.db, user=self.user)
        self.assertTrue(answer["exists"])
        self.assertEqual(answer["home_version"], sl.get_home_version(self.user.id))

    def test_a_reorder_pushes_to_that_user_with_the_new_version(self):
        post = self._post()
        before = rsl.read_home(db=self.db, user=self.user)["home_version"]
        rsl.reorder(rsl.ReorderRequest(order=["playlist:pl-b", "playlist:pl-a"]), db=self.db, user=self.user)

        post.assert_called_once()
        params = post.call_args.kwargs["params"]
        self.assertEqual(params["userId"], "0123456789ab", "Jellyfin wants the user id without dashes")
        after = rsl.read_home(db=self.db, user=self.user)
        self.assertNotEqual(after["home_version"], before)
        self.assertEqual(params["homeVersion"], after["home_version"],
                         "the pushed version is not the one the new config is served with")
        self.assertEqual([r["playlist_id"] for r in after["config"]["rows"]], ["pl-b", "pl-a"])

    def test_every_home_page_save_is_scoped_to_its_user(self):
        post = self._post()
        rsl.set_row_shape(rsl.RowShapeRequest(playlist_id="pl-a", shape="wide"), db=self.db, user=self.user)
        rsl.set_toolbar(rsl.ToolbarRequest(buttons=[{"id": "search", "enabled": True}]), db=self.db, user=self.user)
        rsl.notify(db=self.db, user=self.user)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["params"].get("userId"), "0123456789ab")

    def test_write_home_config_now_tells_the_clients(self):
        post = self._post()
        with mock.patch.object(rsl, "write_home_config", return_value=CONFIG):
            rsl.write_home(db=self.db, user=self.user)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["params"]["userId"], "0123456789ab")

    def test_a_change_for_everyone_has_no_user_but_a_version(self):
        post = self._post()
        sl._notify_jellyfin_plugin(self.db)
        params = post.call_args.kwargs["params"]
        self.assertNotIn("userId", params)
        self.assertEqual(params["homeVersion"], sl.get_home_version(self.user.id))


@unittest.skipIf(rset is None, "fastapi not installed")
class TestSettingsSaveTellsThePlugin(unittest.TestCase):
    """The Settings page's save never reached the plugin (its notify helper was never called)."""

    def setUp(self):
        _reset_versions()
        self.addCleanup(_reset_versions)
        self.db = _session(Path(temp_dir(self)))
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jellyfin.local:8096"), ("jellyfin_api_key", "KEY"),
                     ("home_row_limit", "20")):
            self.db.add(mdb.Setting(key=k, value=v))
        self.db.commit()

    def _save(self, settings):
        with mock.patch("services.smartlists.requests.post") as post:
            post.return_value.ok = True
            rset.update_settings(rset.SettingsUpdate(settings=settings), db=self.db)
        return post

    def test_a_changed_setting_clears_the_plugin_for_everyone(self):
        post = self._save({"home_row_limit": "12"})
        post.assert_called_once()
        self.assertNotIn("userId", post.call_args.kwargs["params"])

    def test_a_save_with_nothing_changed_tells_nobody(self):
        post = self._save({"home_row_limit": "20"})
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
