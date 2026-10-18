"""Session re-check (#272 follow-up): an answer that is not a readable Jellyfin
user keeps the last known state, and Jellyfin is asked again a minute later.

* Down, timing out or answering an error status, Jellyfin was asked again on
  every authenticated request (up to the 5 s timeout each), for every user.
* A JSON object that is not a user ({"error": ...} from a gateway, a missing
  or partial Policy) was read as one: a real admin lost admin, and
  "IsAdministrator": "false" (a string) would have granted it.
* Requests made while a check is in flight each asked as well.
"""
import os
import random
import threading
import time
import unittest
from unittest import mock

import requests

from models.database import TentacleUser
from routers import auth as auth_router
from tests import test_session_lifecycle as base


def _answer(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    r.ok = status == 200
    r.json.return_value = body
    return r


def _user(is_admin=True, disabled=False):
    return _answer(body={"Id": base.JF_ID, "Name": "Owner",
                         "Policy": {"IsAdministrator": is_admin, "IsDisabled": disabled}})


class _Base(unittest.TestCase):
    """test_session_lifecycle's fixture (a real app + DB), without its tests."""
    setUp = base.TestSessionLifecycle.setUp
    tearDown = base.TestSessionLifecycle.tearDown
    _db = base.TestSessionLifecycle._db
    _token = base.TestSessionLifecycle._token


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ReCheck(_Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        p = mock.patch.object(auth_router.time, "monotonic", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.token = self._token()

    def _me(self, answer):
        self.client.cookies.clear()
        self.client.cookies.set(auth_router.COOKIE_NAME, self.token)
        kw = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
        with mock.patch.object(auth_router.requests, "get", **kw) as get:
            r = self.client.get("/api/auth/me")
        return r.status_code, get.call_count

    def _is_admin(self):
        db = self.Session()
        try:
            return db.get(TentacleUser, self.uid).is_admin
        finally:
            db.close()

    def test_down_or_error_status_is_asked_once_a_minute(self):
        for answer in (requests.ConnectTimeout("timed out"), requests.ConnectionError("refused"),
                       _answer(503), _answer(401)):
            with self.subTest(answer=answer):
                auth_router._session_checks.clear()
                calls = [self._me(answer) for _ in range(10)]
                self.assertEqual([200] * 10, [c[0] for c in calls])
                self.assertEqual(1, sum(c[1] for c in calls))
                self.clock.t += 61
                self.assertEqual((200, 1), self._me(answer))     # asked again after a minute

    def test_objects_that_are_not_a_user_keep_the_last_state(self):
        for body in ({"error": "sso"}, {"Policy": None}, {"Policy": {}},
                     {"Policy": {"IsAdministrator": "false"}}, {"Policy": {"IsAdministrator": 0}}):
            with self.subTest(body=body):
                auth_router._session_checks.clear()
                self.assertEqual((200, 1), self._me(_answer(body=body)))
                self.assertTrue(self._is_admin(), "a real admin was demoted")
                self.assertEqual((200, 0), self._me(_answer(body=body)))   # not asked again at once
                self.clock.t += 61
                self.assertEqual((200, 1), self._me(_answer(body=body)))

    def test_a_string_false_never_grants_admin(self):
        db = self.Session()
        db.get(TentacleUser, self.uid).is_admin = False
        db.commit(); db.close()
        self._me(_answer(body={"Policy": {"IsAdministrator": "false", "IsDisabled": False}}))
        self.assertFalse(self._is_admin())

    def test_a_readable_user_is_applied_at_once(self):
        self.assertEqual(200, self._me(_user(is_admin=False))[0])
        self.assertFalse(self._is_admin())                     # demoted, as before
        self.clock.t += 301
        self.assertEqual(401, self._me(_user(disabled=True))[0])
        self.clock.t += 1
        self.assertEqual(401, self._me(_answer(404))[0])

    def test_disabled_only_when_it_is_true(self):
        auth_router._session_checks.clear()
        body = {"Policy": {"IsAdministrator": True, "IsDisabled": "true"}}
        self.assertEqual(200, self._me(_answer(body=body))[0])
        self.assertTrue(self._is_admin())

    def test_recovery_within_a_minute(self):
        self._me(requests.ConnectTimeout("t"))
        self.clock.t += 61
        self.assertEqual((200, 1), self._me(_user(is_admin=False)))
        self.assertFalse(self._is_admin())
        self.assertEqual((200, 0), self._me(_user(is_admin=False)))   # a good check lasts 5 min

    def test_a_demotion_that_could_not_be_saved_is_retried_on_the_next_request(self):
        from sqlalchemy.exc import OperationalError
        db = self.Session()
        user = db.get(TentacleUser, self.uid)
        real_commit, failed = db.commit, []

        def commit_once_locked():
            if not failed:
                failed.append(1)
                raise OperationalError("UPDATE tentacle_users", {}, Exception("database is locked"))
            return real_commit()
        calls = []
        with mock.patch.object(auth_router.requests, "get",
                               side_effect=lambda *a, **k: calls.append(1) or _user(is_admin=False)), \
                mock.patch.object(db, "commit", commit_once_locked):
            with self.assertRaises(OperationalError):
                auth_router._refresh_from_jellyfin(db, user)
            self.assertTrue(auth_router._refresh_from_jellyfin(db, db.get(TentacleUser, self.uid)))
        db.close()
        self.assertEqual(2, len(calls))            # asked again at once, not a minute later
        self.assertFalse(self._is_admin())

    def test_a_slow_check_never_moves_a_newer_mark_back(self):
        uid = base.JF_ID
        real_get = auth_router.requests.get

        def slow_get(*a, **k):
            # while this check waits, a newer one finished and marked a later time
            auth_router._session_checks[uid] = self.clock.t + 500
            return _user(is_admin=True)
        with mock.patch.object(auth_router.requests, "get", slow_get):
            db = self.Session()
            try:
                auth_router._refresh_from_jellyfin(db, db.get(TentacleUser, self.uid))
            finally:
                db.close()
        self.assertEqual(self.clock.t + 500, auth_router._session_checks[uid])


class ParallelRequests(_Base):
    """Requests made while a check is waiting for Jellyfin don't each ask."""

    def test_one_call_for_requests_during_a_slow_check(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_get(*a, **k):
            calls.append(1)
            started.set()
            release.wait(5)
            raise requests.ReadTimeout("timed out")
        results = []

        def one():
            db = self.Session()
            try:
                results.append(auth_router._refresh_from_jellyfin(db, db.get(TentacleUser, self.uid)))
            finally:
                db.close()
        with mock.patch.object(auth_router.requests, "get", slow_get):
            first = threading.Thread(target=one)
            first.start()
            self.assertTrue(started.wait(5))
            others = [threading.Thread(target=one) for _ in range(8)]
            for t in others:
                t.start()
            for t in others:
                t.join(5)
            release.set()
            first.join(5)
        self.assertEqual([True] * 9, results)
        self.assertEqual(1, len(calls))


class RandomisedSequences(_Base):
    """Random answer sequences x clock steps x threads. Invariants:
    I8 never an exception; unreadable answers never change the admin flag
    I9 admin granted only by a readable IsAdministrator true
    I10 404 or IsDisabled true -> False
    I11 at most one call per 60 s while answers are unusable
    I12 a readable IsAdministrator false demotes at once
    SEEDS env (default 1000); the seed is in the failure message."""

    ANSWERS = ["timeout", "503", "html", "list", "error_obj", "policy_none", "policy_empty",
               "str_false", "admin", "not_admin", "disabled", "disabled_str", "404"]

    def _make(self, kind):
        return {
            "timeout": requests.ReadTimeout("t"), "503": _answer(503), "404": _answer(404),
            "html": mock.Mock(status_code=200, json=mock.Mock(side_effect=ValueError("html"))),
            "list": _answer(body=["x"]), "error_obj": _answer(body={"error": "sso"}),
            "policy_none": _answer(body={"Policy": None}), "policy_empty": _answer(body={"Policy": {}}),
            "str_false": _answer(body={"Policy": {"IsAdministrator": "false", "IsDisabled": False}}),
            "admin": _user(True), "not_admin": _user(False), "disabled": _user(True, True),
            "disabled_str": _answer(body={"Policy": {"IsAdministrator": True, "IsDisabled": "true"}}),
        }[kind]

    def test_invariants(self):
        seeds = int(os.environ.get("SEEDS", "1000"))
        clock = Clock()
        with mock.patch.object(auth_router.time, "monotonic", clock):
            for seed in range(seeds):
                rnd = random.Random(seed)
                auth_router._session_checks.clear()
                db = self.Session()
                start_admin = rnd.choice([True, False])
                db.get(TentacleUser, self.uid).is_admin = start_admin
                db.commit()
                expected_admin, last_call_unusable_at = start_admin, None
                for step in range(rnd.randint(1, 12)):
                    clock.t += rnd.choice([0, 1, 30, 59, 61, 120, 299, 301, 900])
                    kind = rnd.choice(self.ANSWERS)
                    ctx = f"seed {seed} step {step} kind {kind}"
                    answer = self._make(kind)
                    kw = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
                    with mock.patch.object(auth_router.requests, "get", **kw) as get:
                        user = db.get(TentacleUser, self.uid)
                        try:
                            out = auth_router._refresh_from_jellyfin(db, user)
                        except Exception as e:
                            self.fail(f"I8 raised {e!r} | {ctx}")
                    db.expire_all()
                    admin_now = db.get(TentacleUser, self.uid).is_admin
                    if get.call_count:
                        if last_call_unusable_at is not None:
                            self.assertGreaterEqual(clock.t - last_call_unusable_at, 60, f"I11 | {ctx}")
                        last_call_unusable_at = None
                        if kind in ("404", "disabled"):
                            self.assertFalse(out, f"I10 | {ctx}")
                            auth_router._session_checks.clear()   # the session is gone; a new login starts over
                            continue
                        self.assertTrue(out, ctx)
                        if kind in ("admin", "disabled_str"):   # readable; "true" as a string is not disabled
                            expected_admin = True
                        elif kind == "not_admin":
                            expected_admin = False
                        else:
                            last_call_unusable_at = clock.t
                    else:
                        self.assertTrue(out, ctx)
                    self.assertEqual(expected_admin, admin_now, f"I8/I9/I12 admin flag | {ctx}")
                db.close()


class AdminFlagOnSignIn(_Base):
    """The same rule where a user first gets their admin flag: plugin
    provisioning (/Users/Me) and the dashboard login."""

    def test_a_string_false_from_users_me_provisions_a_non_admin(self):
        new = "c" * 32
        me = _answer(body={"Id": new, "Name": "New", "Policy": {"IsAdministrator": "false"}})
        db = self.Session()
        try:
            with mock.patch.object(auth_router.requests, "get", return_value=me):
                self.assertEqual(new, auth_router._resolve_token_user(db, "tok-new"))
            user = auth_router._provision_plugin_user(db, new)
            self.assertFalse(user.is_admin)
        finally:
            auth_router._token_cache.pop("tok-new", None)
            db.close()

    def test_a_string_false_at_login_is_not_admin(self):
        from starlette.requests import Request
        from starlette.responses import Response
        answer = mock.Mock()
        answer.raise_for_status.return_value = None
        answer.json.return_value = {"User": {"Id": "b" * 32, "Name": "Kid",
                                             "Policy": {"IsAdministrator": "false"}}}
        request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        db = self.Session()
        try:
            with mock.patch.object(auth_router.requests, "post", return_value=answer):
                auth_router.login(auth_router.LoginRequest(username="Kid", password="x"), Response(), request, db)
            db.expire_all()
            self.assertFalse(db.query(TentacleUser).filter_by(jellyfin_user_id="b" * 32).one().is_admin)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
