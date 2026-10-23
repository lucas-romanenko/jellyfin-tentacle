"""Adding someone (Roadmap 1e): the password made from their name, what
they start with (Live TV, home, favourites; kids the ready-made kids
setup), the message to send them, the addresses it gives, the welcome at
their first sign-in, and the lock after wrong passwords.

Run from the tentacle/ directory:  python -m unittest tests.test_invite_wizard

Jellyfin is test_family's fake (answers like 10.11.8 where it matters). The
lock is Jellyfin's own: UserPolicy.LoginAttemptsBeforeLockout (-1 = never,
its default for a new account) and InvalidLoginAttemptCount, which a
policy POST may reset (Jellyfin.Server.Implementations/Users/UserManager.cs).
"""
import json
import re
import sqlite3
import time
import unittest
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
from services import family as F, kids_start, person_access as PA, welcome
from test_family import ADMIN, ANN, BEN, Case
from tmp_dirs import temp_dir

NEW = "d" * 32


def _new_row(db):
    return db.query(mdb.TentacleUser).filter(mdb.TentacleUser.jellyfin_user_id == NEW).one()


class Passwords(unittest.TestCase):
    def test_made_from_the_name(self):
        self.assertRegex(F.make_password("Cam"), r"^cam\d{5}!$")
        self.assertRegex(F.make_password("Grandma Rose"), r"^grandmarose\d{5}!$")
        self.assertRegex(F.make_password("Zoë O'Neil"), r"^zoeoneil\d{5}!$")
        self.assertRegex(F.make_password("Мария"), r"^tv\d{5}!$")
        self.assertRegex(F.make_password("x" * 40), r"^x{16}\d{5}!$")

    def test_too_short_or_long_is_refused(self):
        for bad in ("", "   ", "abc", "x" * (F.PASSWORD_MAX + 1)):
            with self.assertRaises(F.FamilyError):
                F.check_password(bad)
        self.assertEqual("cam1", F.check_password("cam1"))


class Addresses(unittest.TestCase):
    def test_what_works_away_from_home(self):
        for url in ("https://tentacle.example.com", "http://8.8.8.8:8888", "https://jf.rmnk.dev/"):
            self.assertTrue(F.reachable_away(url), url)
        for url in ("http://192.168.1.10:8888", "http://jellyfin:8096", "http://nas.lan:8096",
                    "http://localhost:8888", "http://10.0.0.2", "http://media.local", "", "not a url"):
            self.assertFalse(F.reachable_away(url), url)


class Invite(Case):
    def _invite(self, **kw):
        with mock.patch("services.family.start_new_person") as start:
            out = F.invite(self.db, self.admin, kw.pop("name", "Cam"), **kw)
        return out, start

    def test_the_admins_password_is_used(self):
        out, _ = self._invite(password="mine-1234")
        self.assertEqual("mine-1234", self.jf.passwords[NEW])
        self.assertEqual("mine-1234", out["invite"]["password"])
        with self.assertRaises(F.FamilyError):
            self._invite(name="Dee", password="abc")

    def test_new_people_lock_after_five_wrong_passwords(self):
        self._invite()
        self.assertEqual(F.LOCKOUT_AFTER, self.jf.users[NEW]["Policy"]["LoginAttemptsBeforeLockout"])

    def test_no_live_tv(self):
        self._invite(live_tv=False)
        p = self.jf.users[NEW]["Policy"]
        self.assertEqual((False, False), (p["EnableLiveTvAccess"], p["EnableLiveTvManagement"]))
        self.assertTrue(_new_row(self.db).live_tv_off)

    def test_a_kid_with_no_live_tv_stays_without_when_kids_channels_come(self):
        self._invite(kids_level="kids", live_tv=False)
        F.set_kids_channels(self.db, ["c2"])
        self.assertFalse(self.jf.users[NEW]["Policy"]["EnableLiveTvAccess"])

    def test_choices_go_to_the_start(self):
        _, start = self._invite(home=str(self.ann.id), channels=str(self.ben.id))
        start.assert_called_once_with(_new_row(self.db).id, self.ann.id, self.ben.id)
        _, start = self._invite(name="Mia", kids_level="young", home="kids", channels="kids")
        self.assertEqual(("kids", "kids"), start.call_args[0][1:])

    def test_the_kids_home_is_only_for_kids(self):
        with self.assertRaises(F.FamilyError):
            self._invite(home="kids")
        with self.assertRaises(F.FamilyError):
            self._invite(name="Mia", kids_level="kids", channels=str(self.ann.id))
        _, start = self._invite(channels="kids")
        self.assertEqual("none", start.call_args[0][2])

    def test_welcome_is_due(self):
        self._invite()
        self.assertIsNone(_new_row(self.db).welcomed_at)


class Message(Case):
    def setUp(self):
        super().setUp()
        mdb.set_setting(self.db, "jellyfin_public_url", "https://jellyfin.example.com")
        mdb.set_setting(self.db, "youtube_base_url", "https://tentacle.example.com")

    def test_tentacle_first_then_where_to_watch_then_the_tv(self):
        self.ann.display_name = "ann"
        text = welcome.message(self.db, self.admin, self.ann, "ann12345!")
        self.assertTrue(text.startswith("Hi Ann! You have an account on Lucas's server."))
        order = [text.index(s) for s in (
            "1. First open Tentacle: https://tentacle.example.com",
            "pick your channels, set up your home screen and ask for movies and shows",
            "Name: ann", "Password: ann12345!", "same name and password",
            "2. Then watch at https://jellyfin.example.com", "iPhone or iPad, use the website",
            "Android TV or Google TV", "Downloader", "https://tentacle.example.com/tv")]
        self.assertEqual(sorted(order), order)
        self.assertNotIn("at home, on your Wi-Fi", text)

    def test_a_kids_message_has_no_requests(self):
        self.ann.kids_level = "kids"
        text = welcome.message(self.db, self.admin, self.ann, "x")
        self.assertIn("set up your home screen", text)
        self.assertNotIn("ask for movies", text)

    def test_home_only_addresses_say_so(self):
        mdb.set_setting(self.db, "jellyfin_public_url", "")
        mdb.set_setting(self.db, "youtube_base_url", "http://192.168.1.5:8888")
        text = welcome.message(self.db, self.admin, self.ann, "x")
        self.assertIn("1. First open Tentacle: http://192.168.1.5:8888 (at home, on your Wi-Fi)", text)
        self.assertIn("2. Then watch at http://jf.lan:8096 (at home, on your Wi-Fi)", text)

    def test_whose_server(self):
        self.admin.display_name = "Chris"
        self.assertEqual("Chris's server", welcome.server_name(self.db, self.admin))
        self.assertEqual("DJ", welcome.display_name("DJ"))

    def test_a_new_password_comes_with_the_message(self):
        card = F.new_password(self.db, self.ann)
        self.assertRegex(card["password"], r"^ann\d{5}!$")
        self.assertIn("here's your new password", card["message"])
        self.assertIn(card["password"], card["message"])


class AskOnce(Case):
    def test_asks_while_an_address_only_works_at_home(self):
        a = F.addresses(self.db)
        self.assertEqual(("http://jf.lan:8096", "http://tentacle.lan:8888", False, False, True),
                         (a["jellyfin"], a["tentacle"], a["tentacle_away"], a["jellyfin_away"], a["ask"]))
        a = F.save_addresses(self.db, "https://t.example.com/", "https://j.example.com")
        self.assertEqual(("https://t.example.com", "https://j.example.com", "https://t.example.com/tv", False),
                         (a["tentacle"], a["jellyfin"], a["tv_app"], a["ask"]))

    def test_answered_once_even_at_home(self):
        a = F.save_addresses(self.db, "http://192.168.1.5:8888", None)
        self.assertEqual(("http://192.168.1.5:8888", False, False), (a["tentacle"], a["tentacle_away"], a["ask"]))

    def test_docker_names_are_never_given(self):
        mdb.set_setting(self.db, "youtube_base_url", "http://tentacle:8888")
        mdb.set_setting(self.db, "jellyfin_url", "http://jellyfin:8096")
        a = F.addresses(self.db)
        self.assertEqual(("", "", "", True), (a["tentacle"], a["jellyfin"], a["tv_app"], a["ask"]))
        self.assertTrue(F.opens_in_a_browser("http://192.168.1.5:8096"))
        self.assertFalse(F.opens_in_a_browser("http://127.0.0.1:8096"))

    def test_a_bad_address_is_refused(self):
        with self.assertRaises(F.FamilyError):
            F.save_addresses(self.db, "tentacle.example.com", None)
        with self.assertRaises(F.FamilyError):
            F.save_addresses(self.db, "javascript:alert(1)", None)

    def test_public_addresses_need_no_question(self):
        mdb.set_setting(self.db, "youtube_base_url", "https://tentacle.example.com")
        mdb.set_setting(self.db, "jellyfin_public_url", "https://jellyfin.example.com")
        self.assertFalse(F.addresses(self.db)["ask"])


class StartingPoint(Case):
    def test_grown_ups_follow_new_people_start_with(self):
        new = mdb.TentacleUser(jellyfin_user_id=NEW, display_name="New")
        self.db.add(new)
        self.db.commit()
        self.assertEqual({"home": "default", "channels": "none"}, F.starting_point(self.db, new))
        F.save_defaults(self.db, self.ann.id, self.ben.id)
        self.assertEqual({"home": self.ann.id, "channels": self.ben.id}, F.starting_point(self.db, new))
        self.assertEqual({"home": "default", "channels": "none"}, F.starting_point(self.db, new, "default", "none"))

    def test_kids_start_on_the_kids_setup_until_the_admin_picks(self):
        self.ann.kids_level = "kids"
        self.assertEqual({"home": "kids", "channels": "kids"}, F.starting_point(self.db, self.ann))
        out = F.save_defaults(self.db, None, None, {"home": "default", "channels": "none"})
        self.assertEqual({"home": "default", "channels": "none"}, out["kids"])
        self.assertEqual({"home": "default", "channels": "none"}, F.starting_point(self.db, self.ann))
        with self.assertRaises(F.FamilyError):
            F.save_defaults(self.db, None, None, {"home": "kids", "channels": "all"})
        with self.assertRaises(F.FamilyError):
            F.save_defaults(self.db, None, None, {"home": 999, "channels": "kids"})


class KidsSetup(Case):
    def test_kids_home_rows_and_navbar(self):
        self.ann.kids_level = "kids"
        self.db.commit()
        written = {}
        made = []
        with mock.patch("services.smartlists.sync_single_custom_playlist",
                        side_effect=lambda db, uid, name, cond, apply_to, tag: made.append((name, apply_to, cond)) or {}), \
                mock.patch("services.smartlists._get_smartlists_with_playlist_ids",
                           return_value=[{"name": "Kids Movies", "playlist_id": "km"},
                                         {"name": "Kids Shows", "playlist_id": "ks"}]), \
                mock.patch("services.smartlists.write_home_config"), \
                mock.patch("services.smartlists._notify_jellyfin_plugin") as notify, \
                mock.patch("services.smartlists.bump_playlist_version") as bump, \
                mock.patch("services.smartlists._user_home_config_path", return_value="p"), \
                mock.patch("services.smartlists.write_home_json", side_effect=lambda p, c: written.update(c)):
            self.assertEqual(3, kids_start.kids_home(self.db, self.ann))
            kids_start.kids_home(self.db, self.ann)
        self.assertEqual([("builtin", "resumevideo"), ("playlist", "km"), ("playlist", "ks")],
                         [(r["type"], r.get("section_id") or r.get("playlist_id")) for r in written["rows"]])
        self.assertEqual(("km", True), (written["hero"]["playlist_id"], written["hero"]["enabled"]))
        off = {t["id"] for t in written["toolbar"] if not t["enabled"]}
        self.assertTrue({"discover", "activity"} <= off)
        bump.assert_called()
        notify.assert_called()
        # Genre playlists, any of the genres; made once however often it runs.
        self.assertEqual(("Kids Movies", "movies"), made[0][:2])
        self.assertEqual({"or"}, {c["genre_logic"] for c in made[1][2]})
        self.assertEqual({"Animation", "Family", "Kids"}, {c["value"] for c in made[1][2]})
        rules = self.db.query(mdb.TagRule).filter(mdb.TagRule.user_id == self.ann.id).all()
        self.assertEqual(["Kids Movies", "Kids Shows"], sorted(r.output_tag for r in rules))

    def test_kids_channels_are_starred(self):
        self.ann.kids_level = "kids"
        F.set_kids_channels(self.db, ["c2", "c4"])
        lineup = [{"key": "c2", "stream_path": "/2"}, {"key": "c4", "stream_path": "/4"}]
        starred = []
        with mock.patch("routers.my_livetv.my_lineup", return_value=lineup), \
                mock.patch("services.livetv_tuners.all_tuner_url", return_value="http://t/m3u"), \
                mock.patch("services.livetv_lineups.item_ids_for", return_value={"i2": "c2", "i4": "c4"}), \
                mock.patch("services.livetv_tuners.favourite_ids", return_value={"i4"}), \
                mock.patch("services.livetv_tuners.set_favourite", side_effect=lambda jf, u, i, on: starred.append(i)):
            self.assertEqual(1, kids_start.star_kids_channels(self.db, self.ann))
        self.assertEqual(["i2"], starred)


class LiveTvPerPerson(Case):
    def test_off_and_on_for_a_grown_up(self):
        PA.set_access(self.db, self.admin, self.ann, {"live_tv": False})
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])
        self.assertTrue(self.ann.live_tv_off)
        PA.set_access(self.db, self.admin, self.ann, {"live_tv": True})
        self.assertTrue(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])

    def test_a_kid_without_kids_channels_has_none_even_when_allowed(self):
        F.set_kids(self.db, self.ann, "kids")
        PA.set_access(self.db, self.admin, self.ann, {"live_tv": True})
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])
        self.assertFalse(self.ann.live_tv_off)

    def test_kids_profile_off_keeps_live_tv_off(self):
        PA.set_access(self.db, self.admin, self.ann, {"live_tv": False})
        F.set_kids(self.db, self.ann, "kids")
        F.set_kids(self.db, self.ann, None)
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvAccess"])


class Lockout(Case):
    def test_everyone_but_admins_and_only_once(self):
        self.jf.users[BEN]["Policy"]["LoginAttemptsBeforeLockout"] = 3
        self.assertEqual(1, PA.ensure_lockout(self.db))
        self.assertEqual(F.LOCKOUT_AFTER, self.jf.users[ANN]["Policy"]["LoginAttemptsBeforeLockout"])
        self.assertEqual(3, self.jf.users[BEN]["Policy"]["LoginAttemptsBeforeLockout"])
        self.assertNotIn("LoginAttemptsBeforeLockout", self.jf.users[ADMIN]["Policy"])
        self.assertEqual(0, PA.ensure_lockout(self.db))

    def test_jellyfin_down_is_quiet(self):
        with mock.patch.object(self.jf, "get", side_effect=requests.ConnectionError()):
            self.assertEqual(0, PA.ensure_lockout(self.db))

    def test_locked_is_told_apart_from_turned_off(self):
        self.assertTrue(F.is_locked({"IsDisabled": True, "LoginAttemptsBeforeLockout": 5, "InvalidLoginAttemptCount": 5}))
        self.assertFalse(F.is_locked({"IsDisabled": True, "LoginAttemptsBeforeLockout": 5, "InvalidLoginAttemptCount": 0}))
        self.assertFalse(F.is_locked({"IsDisabled": False, "LoginAttemptsBeforeLockout": 5, "InvalidLoginAttemptCount": 5}))
        self.assertFalse(F.is_locked({"IsDisabled": True, "LoginAttemptsBeforeLockout": -1, "InvalidLoginAttemptCount": 9}))

    def test_unlock_turns_on_and_clears_the_count(self):
        self.jf.users[ANN]["Policy"].update(IsDisabled=True, LoginAttemptsBeforeLockout=5, InvalidLoginAttemptCount=5)
        after = PA.set_access(self.db, self.admin, self.ann, {"disabled": False})
        self.assertEqual((False, 0), (after["Policy"]["IsDisabled"], after["Policy"]["InvalidLoginAttemptCount"]))
        self.assertFalse(F.person(self.db, self.ann, after)["locked"])


class Welcome(Case):
    def test_shown_to_new_people_not_the_owner(self):
        self.assertFalse(welcome.state(self.db, self.admin)["show"])
        s = welcome.state(self.db, self.ann)
        self.assertTrue(s["show"])
        self.assertEqual(("Ann", "Lucas's server", "Lucas", False, True), (s["name"], s["server"], s["admin"], s["kids"], s["live_tv"]))
        self.assertEqual("ask", s["requests"]["mode"])
        welcome.done(self.db, self.ann)
        self.assertFalse(welcome.state(self.db, self.ann)["show"])

    def test_routes(self):
        from routers import family
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        c = TestClient(app)
        self.assertTrue(c.get("/api/welcome").json()["show"])
        self.assertEqual({"ok": True}, c.post("/api/welcome/done").json())
        self.assertFalse(c.get("/api/welcome").json()["show"])

    def _auth(self, status):
        r = mock.Mock(status_code=status, ok=status < 400)
        return mock.patch("services.welcome.requests.post", return_value=r)

    def test_pick_my_own_password(self):
        with self._auth(200) as auth:
            welcome.change_password(self.db, self.ann, "old-one", "my-new-one")
        self.assertEqual({"Username": "Ann", "Pw": "old-one"}, auth.call_args.kwargs["json"])
        self.assertEqual("my-new-one", self.jf.passwords[ANN])

    def test_the_wrong_password_is_refused(self):
        with self._auth(401):
            with self.assertRaises(welcome.PasswordError) as e:
                welcome.change_password(self.db, self.ann, "wrong", "my-new-one")
        self.assertEqual(403, e.exception.status)
        self.assertNotIn(ANN, self.jf.passwords)
        for new in ("abc", "same-one"):
            with self._auth(200), self.assertRaises(welcome.PasswordError):
                welcome.change_password(self.db, self.ann, "same-one", new)


class WelcomedMigration(unittest.TestCase):
    def test_people_already_here_count_as_welcomed(self):
        tmp = temp_dir(self)
        conn = sqlite3.connect(f"{tmp}/t.db")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE tentacle_users (id INTEGER PRIMARY KEY, jellyfin_user_id TEXT, display_name TEXT)")
        conn.execute("INSERT INTO tentacle_users (jellyfin_user_id, display_name) VALUES ('x', 'Old')")
        conn.commit()
        mdb._migrate_welcomed(conn.cursor(), conn)
        mdb._migrate_welcomed(conn.cursor(), conn)
        self.assertIsNotNone(conn.execute("SELECT welcomed_at FROM tentacle_users").fetchone()[0])



class WelcomeCounts(Case):
    """"You now have access to 12,345 movies, 1,234 TV shows and 1,730 live
    TV channels": what this person can watch, counted once per title, made
    in the background and kept."""

    def setUp(self):
        super().setUp()
        welcome._counts_cache.clear()
        welcome._counts_running.clear()
        self.addCleanup(welcome._counts_cache.clear)
        from routers import discover
        maps = {"movie": {1: "a", 2: "b", 3: "c"}, "series": {7: "s"}}
        p = mock.patch.object(discover, "_search_jellyfin_ids", side_effect=lambda k, deadline: maps[k])
        self.maps = p.start()
        self.addCleanup(p.stop)
        lineup = mock.patch("routers.my_livetv.my_lineup", side_effect=lambda db, u: [{}] * (2 if u.kids_level else 5))
        self.lineup = lineup.start()
        self.addCleanup(lineup.stop)
        # The counting thread opens its own session: the test's.
        sl = mock.patch("models.database.SessionLocal", side_effect=lambda: self.Session())
        sl.start()
        self.addCleanup(sl.stop)

    def test_everyone_counts_the_whole_library_once_per_title(self):
        self.assertEqual({"movies": 3, "shows": 1, "channels": 5}, welcome.counts(self.db, self.ann, wait=5))

    def test_a_kid_counts_what_jellyfin_shows_them_and_their_kids_channels(self):
        self.ann.kids_level = "young"
        self.db.commit()
        with mock.patch("services.family.visible_titles", return_value={("movie", 1), ("series", 7), ("series", 8)}):
            self.assertEqual({"movies": 1, "shows": 2, "channels": 2}, welcome.counts(self.db, self.ann, wait=5))

    def test_no_live_tv_no_channels(self):
        self.ann.live_tv_off = True
        self.db.commit()
        self.assertEqual(0, welcome.counts(self.db, self.ann, wait=5)["channels"])
        self.lineup.assert_not_called()

    def test_titles_and_channels_are_counted_once_then_kept(self):
        welcome.counts(self.db, self.ann, wait=5)
        maps, lineups = self.maps.call_count, self.lineup.call_count
        self.assertEqual({"movies": 3, "shows": 1, "channels": 5}, welcome.counts(self.db, self.ann, wait=0))
        self.assertEqual((maps, lineups), (self.maps.call_count, self.lineup.call_count))

    def test_an_old_count_is_given_at_once_and_counted_again_behind_it(self):
        welcome._counts_cache[self.ann.id] = (time.time() - welcome.COUNTS_FRESH_S - 1, {"movies": 9, "shows": 9, "channels": 9})
        self.assertEqual({"movies": 9, "shows": 9, "channels": 9}, welcome.counts(self.db, self.ann, wait=0))
        welcome._counts_running[self.ann.id].result(timeout=5)
        self.assertEqual({"movies": 3, "shows": 1, "channels": 5}, welcome.counts(self.db, self.ann, wait=0))

    def test_a_slow_count_comes_later_not_in_the_way(self):
        import threading
        gate = threading.Event()
        from routers import discover
        with mock.patch.object(discover, "_search_jellyfin_ids", side_effect=lambda k, d: gate.wait(5) and {1: "a"}):
            self.ann.welcomed_at = None
            self.db.commit()
            self.assertIsNone(welcome.state(self.db, self.ann)["counts"])
            gate.set()
            self.assertEqual({"movies": 1, "shows": 1, "channels": 5},
                             welcome.counts(self.db, self.ann, wait=welcome.COUNTS_LATE_WAIT_S))

    def test_everyone_the_welcome_is_due_for_is_counted_ahead(self):
        from datetime import datetime
        self.admin.welcomed_at = self.ben.welcomed_at = datetime.utcnow()
        self.ann.welcomed_at = None
        self.db.commit()
        self.assertEqual(1, welcome.warm_everyone_due())
        welcome._counts_running[self.ann.id].result(timeout=5)
        self.assertIn(self.ann.id, welcome._counts_cache)
        self.assertNotIn(self.ben.id, welcome._counts_cache)

    def test_nothing_is_counted_for_someone_already_welcomed(self):
        from datetime import datetime
        self.ann.welcomed_at = datetime.utcnow()
        self.db.commit()
        self.assertIsNone(welcome.state(self.db, self.ann)["counts"])
        self.maps.assert_not_called()

if __name__ == "__main__":
    unittest.main()
