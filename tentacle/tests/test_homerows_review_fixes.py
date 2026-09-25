"""Follow-ups from the independent review of fix/homerows (R1, R2, R3, R5).

R1 A hero reload that landed after the user left Home ran in the hidden media bar (slide timer, trailer lookups;
   audible with trailer audio on). refreshIfChanged must act only on Home and drop an answer that arrives after
   leaving (without storing its key, so show() applies it on the next visit); the home poll drops a stale answer.
R2 A late toolbar answer for the previous user overwrote the new user's toolbar after an in-tab switch.
R3 A corrupt home config counted as "no config": the new-user seed overwrote it and, in the same second, the
   regeneration's backup replaced the seed's backup of the corrupt original.
R5 After a user switch the remembered hero config was the previous user's (one extra reload / reshuffle), and a
   trailer-audio change kept the old mute default.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.smartlists as ssl

HOME_JS = Path("../tentacle-plugin/Inject/tentacle-home.js")
MEDIABAR_JS = Path("../tentacle-plugin/Inject/tentacle-mediabar.js")
NAVBAR_JS = Path("../tentacle-plugin/Inject/tentacle-navbar.js")


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _method(src, name):
    i = src.index(name + ": function")
    m = re.search(r"\n        [A-Za-z_]+: function", src[i + 1:])
    return src[i: i + 1 + m.start()] if m else src[i:]


class TestR1HeroNeverReloadsOffHome(unittest.TestCase):
    def setUp(self):
        self.bar = MEDIABAR_JS.read_text()
        self.refresh = _method(self.bar, "refreshIfChanged")

    def test_refuses_to_start_off_home(self):
        head = self.refresh[: self.refresh.index("getJSON")]
        self.assertRegex(head, r"!this\.isHomePage\(\)\) return Promise\.resolve\(false\)")

    def test_drops_a_late_answer_without_storing_its_key(self):
        then = self.refresh[self.refresh.index(".then(function (cfg)"):]
        guard = re.search(r"if \(gen !== self\.generation \|\| forUser !== self\.userId \|\| !self\.isHomePage\(\)\) return false;", then)
        self.assertIsNotNone(guard, "the HeroConfig answer is applied even after leaving Home")
        self.assertLess(guard.start(), then.index("_applyHeroConfig"), "key stored before the Home check")
        self.assertIn("var gen = this.generation", self.refresh)

    def test_no_timer_restart_after_leaving_during_the_reload(self):
        tail = self.refresh[self.refresh.index("return loading.then"):]
        self.assertLess(tail.index("if (!self.isHomePage()) return true"), tail.index("resetAutoAdvance"))

    def test_show_applies_a_change_skipped_while_away(self):
        show = _method(self.bar, "show")
        self.assertIn("this.refreshIfChanged()", show)

    def test_hide_still_cancels(self):
        self.assertIn("this.generation++", _method(self.bar, "hide"))

    def test_home_poll_drops_an_answer_that_arrives_after_leaving(self):
        home = HOME_JS.read_text()
        i = home.index("function startVersionPolling(")
        poll = home[home.index("setInterval", i):]
        then = poll[poll.index(".then(function (data)"):]
        guard = then.index("if (gen !== MH.generation) return;")
        self.assertLess(guard, then.index("refreshPlaylistRows(gen)"))
        self.assertLess(then.index("MH.versionPollInFlight = false"), guard, "in-flight flag must still clear")


class TestR2ToolbarAnswersAreTagged(unittest.TestCase):
    def setUp(self):
        self.nav = NAVBAR_JS.read_text()
        self.fetch = _method(self.nav, "fetchToolbarConfig")

    def test_each_request_is_numbered_and_bound_to_its_user(self):
        self.assertRegex(self.fetch, r"var seq = self\._toolbarSeq = \(self\._toolbarSeq \|\| 0\) \+ 1")
        self.assertRegex(self.fetch, r"if \(seq !== self\._toolbarSeq \|\| api\.getCurrentUserId\(\) !== userId\)")
        self.assertLess(self.fetch.index("seq !== self._toolbarSeq"), self.fetch.index("self.toolbarConfig = data.buttons"))

    def test_a_user_change_forgets_the_previous_users_buttons(self):
        self.assertRegex(self.fetch.replace("\n", " "),
                         r"if \(self\._toolbarUser !== userId\) \{\s*self\._toolbarUser = userId;\s*self\.toolbarConfig = null;")

    def test_refresh_does_not_rebuild_from_a_stale_answer(self):
        self.assertIn("if (result === 'stale') return;", _method(self.nav, "refreshToolbar"))


class TestR5HeroConfigState(unittest.TestCase):
    def setUp(self):
        self.bar = MEDIABAR_JS.read_text()

    def test_trailer_audio_is_applied_on_every_config_change(self):
        apply_ = _method(self.bar, "_applyHeroConfig")
        self.assertIn("this._heroConfigKey = JSON.stringify(cfg || null)", apply_)
        self.assertIn("cfg.trailerAudio === false", apply_)
        self.assertIn("self._applyHeroConfig(cfg)", _method(self.bar, "refreshIfChanged"))
        self.assertIn("self._applyHeroConfig(cfg)", _method(self.bar, "init"))

    def test_a_user_switch_replaces_the_remembered_config(self):
        show = _method(self.bar, "show")
        block = show[show.index("if (userChanged && this.apiClient) {"): show.index("} else if (wasDetached")]
        self.assertIn("this._heroConfigKey = undefined", block)
        self.assertIn("this.refreshIfChanged()", block)

    def test_the_in_flight_request_is_shared_only_for_the_same_user(self):
        self.assertIn("this._heroCfgInFlightUser === this.userId", _method(self.bar, "refreshIfChanged"))


class TestR1CancelledReloadIsRetried(unittest.TestCase):
    """Live finding while re-running the review's repro: the key was stored, then hide() cancelled the hero load,
    so the next Home visit saw 'no change' and kept the old hero for good."""

    def test_a_cancelled_or_failed_load_restores_the_previous_key(self):
        r = _method(MEDIABAR_JS.read_text(), "refreshIfChanged")
        self.assertIn("var prevKey = self._heroConfigKey", r)
        self.assertRegex(r, r"var undo = function \(\) \{ if \(self\._heroConfigKey === key\) self\._heroConfigKey = prevKey; return false; \}")
        self.assertRegex(r.replace("\n", " "), r"var loading = self\.loadContent\(\);\s*var loadGen = self\.generation;")
        self.assertIn("if (self.generation !== loadGen) return undo();", r)
        self.assertRegex(r, r"\}, undo\);")


class TestR3CorruptConfigIsNotSeededOver(unittest.TestCase):
    def _db(self, admin=False):
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="U", is_admin=admin))
        db.commit()
        return tmp, db

    def test_a_corrupt_file_is_not_seeded_and_its_backup_survives(self):
        tmp, db = self._db()
        home = tmp / "home-configs"; home.mkdir()
        path = home / "jf-1.json"
        corrupt = '{"rows": [ {"type": "builtin"'
        path.write_text(corrupt)
        seed = mock.Mock(return_value={})
        smart = [{"name": "QA Tube", "playlist_id": "pl-yt", "media_types": ["Movie"], "enabled": True,
                  "sort_by": "releasedate", "sort_order": "Descending", "is_youtube": True}]
        with mock.patch.object(ssl, "_get_smartlists_with_playlist_ids", lambda db, user_id=None: smart), \
             mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: path), \
             mock.patch("routers.smartlists._seed_home_config_from_jellyfin", seed), \
             mock.patch.object(ssl, "bump_playlist_version"):
            ssl.write_home_config(db, user_id=1)
        seed.assert_not_called()
        kept = [b.read_text() for b in (home / "backups").glob("jf-1-*.json")]
        self.assertIn(corrupt, kept, "the corrupt original has no backup left")

    def test_two_backups_in_one_second_are_both_kept(self):
        tmp, _ = self._db()
        path = tmp / "jf-1.json"
        path.write_text('{"a": 1}')
        ssl._backup_home_config(path)
        path.write_text('{"a": 2}')
        ssl._backup_home_config(path)
        got = sorted(b.read_text() for b in (tmp / "backups").glob("jf-1-*.json"))
        self.assertEqual(got, ['{"a": 1}', '{"a": 2}'])

    def test_new_and_old_backup_names_still_prune_oldest_first(self):
        tmp, _ = self._db()
        b = tmp / "backups"; b.mkdir()
        (b / "jf-1-20200101T000000.json").write_text("old")
        path = tmp / "jf-1.json"
        for i in range(ssl.HOME_CONFIG_BACKUPS + 1):
            path.write_text(str(i)); ssl._backup_home_config(path)
        names = sorted(p.name for p in b.glob("jf-1-*.json"))
        self.assertEqual(len(names), ssl.HOME_CONFIG_BACKUPS)
        self.assertNotIn("jf-1-20200101T000000.json", names, "the second-precision name must sort as older")

    def test_the_admins_legacy_file_counts_as_a_config(self):
        tmp, db = self._db(admin=True)
        legacy = tmp / "tentacle-home.json"; legacy.write_text('{"rows": []}')
        missing = tmp / "home-configs" / "jf-1.json"
        with mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: missing), \
             mock.patch.object(ssl, "get_setting", lambda db, k, d=None: str(legacy) if k == "home_config_path" else d):
            self.assertTrue(ssl._home_config_exists(db, 1))
        tmp2, db2 = self._db(admin=False)
        with mock.patch.object(ssl, "_user_home_config_path", lambda db, user_id=None: tmp2 / "none.json"), \
             mock.patch.object(ssl, "get_setting", lambda db, k, d=None: str(legacy) if k == "home_config_path" else d):
            self.assertFalse(ssl._home_config_exists(db2, 1), "a non-admin must not inherit the legacy file")


if __name__ == "__main__":
    unittest.main()
