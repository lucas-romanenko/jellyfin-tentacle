"""People: what each person may do in Jellyfin (Roadmap 1d) and the two
server-wide sign-in settings, always read from Jellyfin.

Run from the tentacle/ directory:  python -m unittest tests.test_person_access

Jellyfin's behaviour is from its 10.11.8 source: a policy POST replaces the
whole policy (UserManager.UpdatePolicyAsync); new accounts are hidden and
can't manage recordings (UserEntityExtensions.AddDefaultPermissions); an
access schedule allows start <= hour <= end on one day set, no wrap past
midnight (IsParentalScheduleAllowed); a wrong password at the lockout
count sets IsDisabled, and only a successful sign-in resets the count.
"""
import json
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
from services import family as F, person_access as PA
from test_family import ADMIN, ANN, BEN, Case, FakeJellyfin

MOVIES, SHOWS, HOME = "f137a2dd21bbc1b99aa5c0f6bf02a805", "a656b907eb3a73532e40e44b968d0225", "0" * 31 + "1"


class Jf(FakeJellyfin):
    def __init__(self, users):
        super().__init__(users)
        self.config = {"QuickConnectAvailable": True, "RemoteClientBitrateLimit": 0, "ServerName": "home"}
        for u in self.users.values():
            u["Policy"].update({"EnableRemoteAccess": True, "AccessSchedules": [], "EnabledFolders": [],
                                "IsHidden": True, "InvalidLoginAttemptCount": 0})

    def get(self, path, **p):
        if path == "/Library/VirtualFolders":
            self.calls.append(("GET", path, p))
            return [{"Name": "Movies", "ItemId": MOVIES}, {"Name": "Shows", "ItemId": SHOWS},
                    {"Name": "Home videos", "ItemId": HOME}]
        if path == "/System/Configuration":
            self.calls.append(("GET", path, p))
            return json.loads(json.dumps(self.config))
        return super().get(path, **p)

    def post(self, path, body=None, **p):
        if path == "/System/Configuration":
            self.calls.append(("POST", path, body, p))
            self.config = json.loads(json.dumps(body))
            return None
        return super().post(path, body, **p)


class Pure(unittest.TestCase):
    def test_reading_a_policy(self):
        a = PA.access_of({"EnableLiveTvManagement": False, "EnableRemoteAccess": False,
                          "EnableAllFolders": False, "EnabledFolders": ["A-B", "c"],
                          "AccessSchedules": [{"DayOfWeek": "Everyday", "StartHour": 7, "EndHour": 20.5}],
                          "IsDisabled": True})
        self.assertEqual({"can_record": False, "away": False, "libraries": ["ab", "c"],
                          "hours": {"start": 7.0, "end": 20.5}, "disabled": True, "live_tv": True}, a)
        self.assertEqual({"can_record": False, "away": True, "libraries": None, "hours": None,
                          "disabled": False, "live_tv": True}, PA.access_of({}))

    def test_an_evening_past_midnight_is_two_ranges_and_reads_back_as_one(self):
        s = PA.schedules_for({"start": 16, "end": 1}, ANN)
        self.assertEqual([(16, 24), (0, 1)], [(x["StartHour"], x["EndHour"]) for x in s])
        self.assertTrue(all(x["DayOfWeek"] == "Everyday" and "-" in x["UserId"] for x in s))
        self.assertEqual({"start": 16.0, "end": 1.0}, PA.hours_of(s))

    def test_hours_set_in_jellyfin_some_other_way_show_as_custom(self):
        self.assertEqual({"custom": True}, PA.hours_of([{"DayOfWeek": "Weekend", "StartHour": 8, "EndHour": 22}]))

    def test_bad_hours_are_refused(self):
        for h in ({"start": 7, "end": 7}, {"start": 7.25, "end": 9}, {"start": -1, "end": 9}, {"start": "x", "end": 1}):
            with self.assertRaises(F.FamilyError, msg=h):
                PA.schedules_for(h, ANN)

    def test_turning_on_clears_the_wrong_password_count(self):
        p = PA.changed_policy({"IsDisabled": True, "InvalidLoginAttemptCount": 3}, {"disabled": False}, ANN)
        self.assertEqual((False, 0), (p["IsDisabled"], p["InvalidLoginAttemptCount"]))

    def test_libraries_unknown_to_jellyfin_are_dropped_and_none_is_refused(self):
        p = PA.changed_policy({}, {"libraries": [MOVIES.upper(), "nope"]}, ANN, [MOVIES, SHOWS])
        self.assertEqual((False, [MOVIES]), (p["EnableAllFolders"], p["EnabledFolders"]))
        with self.assertRaises(F.FamilyError):
            PA.changed_policy({}, {"libraries": ["nope"]}, ANN, [MOVIES])
        p = PA.changed_policy(p, {"libraries": None}, ANN)
        self.assertEqual((True, []), (p["EnableAllFolders"], p["EnabledFolders"]))

    def test_only_what_was_sent_changes(self):
        before = {"EnableRemoteAccess": True, "EnableLiveTvManagement": False, "Other": 1}
        self.assertEqual({**before, "EnableRemoteAccess": False},
                         PA.changed_policy(before, {"away": False}, ANN))

    def test_new_people_are_shown_and_record_unless_kids(self):
        p = PA.new_person_policy({"IsHidden": True, "EnableLiveTvManagement": False, "Keep": 1}, kid=False)
        self.assertEqual((False, True, True, True, [], 1), (p["IsHidden"], p["EnableLiveTvManagement"],
                         p["EnableRemoteAccess"], p["EnableAllFolders"], p["AccessSchedules"], p["Keep"]))
        self.assertFalse(PA.new_person_policy({}, kid=True)["EnableLiveTvManagement"])


class Access(Case):
    def setUp(self):
        super().setUp()
        self.jf = Jf([(ADMIN, "Lucas", True), (ANN, "Ann", False), (BEN, "Ben", False)])
        self.mocks[0].return_value = self.jf
        PA.forget(ANN)

    def test_a_change_is_applied_to_the_whole_policy_and_read_back(self):
        before = json.loads(json.dumps(self.jf.users[ANN]["Policy"]))
        after = PA.set_access(self.db, self.admin, self.ann, {"can_record": True, "away": False,
                                                              "hours": {"start": 7, "end": 20}})
        p = self.jf.users[ANN]["Policy"]
        self.assertEqual({**before, "EnableLiveTvManagement": True, "EnableRemoteAccess": False,
                          "AccessSchedules": [{"DayOfWeek": "Everyday", "StartHour": 7.0, "EndHour": 20.0,
                                               "UserId": F.dashed(ANN)}]}, p)
        self.assertEqual(p, after["Policy"])
        self.assertEqual("GET", self.jf.calls[-1][0])   # read back after the save

    def test_nothing_new_is_not_posted(self):
        PA.set_access(self.db, self.admin, self.ann, {"away": True})
        self.assertFalse([c for c in self.jf.calls if c[0] == "POST"])

    def test_admins_and_kids_are_refused(self):
        with self.assertRaises(F.FamilyError) as e:
            PA.set_access(self.db, self.admin, self.admin, {"away": False})
        self.assertEqual(403, e.exception.status)
        F.set_kids(self.db, self.ann, "kids")
        with self.assertRaises(F.FamilyError):
            PA.set_access(self.db, self.admin, self.ann, {"can_record": True})
        PA.set_access(self.db, self.admin, self.ann, {"away": False})    # other limits are fine for kids
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableRemoteAccess"])

    def test_kids_profile_puts_recording_back_as_it_was(self):
        PA.set_access(self.db, self.admin, self.ann, {"can_record": True})
        F.set_kids(self.db, self.ann, "kids")
        self.assertFalse(self.jf.users[ANN]["Policy"]["EnableLiveTvManagement"])
        F.set_kids(self.db, self.ann, None)
        self.assertTrue(self.jf.users[ANN]["Policy"]["EnableLiveTvManagement"])

    def test_some_libraries_filter_tentacles_library(self):
        self.assertFalse(PA.library_limited(self.db, self.ann))
        PA.set_access(self.db, self.admin, self.ann, {"libraries": [MOVIES]})
        self.assertTrue(PA.library_limited(self.db, self.ann))
        self.assertEqual((False, [MOVIES]), (self.jf.users[ANN]["Policy"]["EnableAllFolders"],
                                             self.jf.users[ANN]["Policy"]["EnabledFolders"]))

    def test_library_limited_keeps_the_last_answer_while_jellyfin_is_away(self):
        PA.set_access(self.db, self.admin, self.ann, {"libraries": [MOVIES]})
        self.assertTrue(PA.library_limited(self.db, self.ann))
        PA._limited[ANN] = (0, True)     # stale
        with mock.patch.object(self.jf, "get", side_effect=F.requests.ConnectionError()):
            self.assertTrue(PA.library_limited(self.db, self.ann))

    def test_invite_starts_with_the_household_defaults(self):
        with mock.patch("services.family.start_new_person"):
            r = F.invite(self.db, self.admin, "Cara")
        p = self.jf.users["d" * 32]["Policy"]
        self.assertFalse(p["IsHidden"])
        self.assertTrue(p["EnableLiveTvManagement"])
        self.assertTrue(p["EnableRemoteAccess"])
        self.assertTrue(p["EnableAllFolders"])
        self.assertEqual("Cara", r["user"]["name"])

    def test_invited_kid_cannot_record(self):
        with mock.patch("services.family.start_new_person"):
            F.invite(self.db, self.admin, "Dot", kids_level="kids")
        p = self.jf.users["d" * 32]["Policy"]
        self.assertFalse(p["EnableLiveTvManagement"])
        self.assertFalse(p["IsHidden"])
        self.assertEqual(10, p["MaxParentalRating"])


class Routes(Case):
    def setUp(self):
        super().setUp()
        self.jf = Jf([(ADMIN, "Lucas", True), (ANN, "Ann", False), (BEN, "Ben", False)])
        self.mocks[0].return_value = self.jf
        from routers import family as R
        from routers.auth import require_admin
        app = FastAPI()
        app.include_router(R.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: self.admin
        self.c = TestClient(app)

    def test_people_shows_each_persons_access_from_jellyfin(self):
        self.jf.users[BEN]["Policy"]["EnableRemoteAccess"] = False   # changed in Jellyfin itself
        with mock.patch("services.tv_app.latest", return_value=None), \
                mock.patch("services.family.start_new_person"):
            d = self.c.get("/api/family/people").json()
        ben = next(p for p in d["people"] if p["name"] == "Ben")
        self.assertFalse(ben["access"]["away"])
        self.assertEqual(["Movies", "Shows", "Home videos"], [l["name"] for l in d["libraries"]])

    def test_put_access_answers_with_jellyfins_values(self):
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"libraries": [MOVIES, SHOWS]})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(sorted([MOVIES, SHOWS]), r.json()["access"]["libraries"])
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"libraries": None})
        self.assertIsNone(r.json()["access"]["libraries"])
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"hours": {"start": 22, "end": 6}})
        self.assertEqual({"start": 22.0, "end": 6.0}, r.json()["access"]["hours"])
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"hours": None})
        self.assertIsNone(r.json()["access"]["hours"])

    def test_put_access_refusals_are_plain(self):
        r = self.c.put(f"/api/family/people/{self.admin.id}/access", json={"away": False})
        self.assertEqual(403, r.status_code)
        self.assertIn("Admins can do everything", r.json()["detail"])
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"away": None})
        self.assertEqual(400, r.status_code)
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={})
        self.assertEqual(400, r.status_code)

    def test_turn_off_and_on(self):
        self.jf.users[ANN]["Policy"].update({"IsDisabled": True, "InvalidLoginAttemptCount": 5})
        r = self.c.put(f"/api/family/people/{self.ann.id}/access", json={"disabled": False})
        self.assertFalse(r.json()["disabled"])
        self.assertEqual(0, self.jf.users[ANN]["Policy"]["InvalidLoginAttemptCount"])

    def test_server_settings_round_trip(self):
        d = self.c.get("/api/family/server").json()
        self.assertEqual((True, 0), (d["quick_connect"], d["away_quality"]))
        d = self.c.put("/api/family/server", json={"away_quality": 10_000_000}).json()
        self.assertEqual(10_000_000, d["away_quality"])
        self.assertEqual("home", self.jf.config["ServerName"])     # the rest of the config kept
        self.assertTrue(self.jf.config["QuickConnectAvailable"])
        d = self.c.put("/api/family/server", json={"quick_connect": False}).json()
        self.assertFalse(d["quick_connect"])
        self.assertEqual(10_000_000, d["away_quality"])
        self.assertEqual(400, self.c.put("/api/family/server", json={"away_quality": 123}).status_code)


if __name__ == "__main__":
    unittest.main()
