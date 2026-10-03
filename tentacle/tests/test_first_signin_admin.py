"""The first sign-in must be a Jellyfin administrator (#390).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The first Tentacle user is the owner: it inherits the pre-multi-user data and
its Jellyfin id is stored as jellyfin_user_id, the account Tentacle reads
Jellyfin as (Discover's Jellyfin map, the tag and wrong-match reads). The docs
say that is the first *admin*; the login took whoever came first, so a
non-admin who could see one library made Discover's map of the whole library
empty for everyone. A non-admin signing in first is now refused and leaves
nothing behind; the install waits for an administrator. Nothing changes once
an owner exists.
"""
import threading
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from models.database import Base, Setting, TentacleUser, get_setting
from routers import auth as auth_router
from tmp_dirs import temp_dir

ADMIN, KID, OTHER_ADMIN = "b" * 32, "a" * 32, "c" * 32


def fresh_engine(test):
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    test.addCleanup(engine.dispose)
    Session = sessionmaker(bind=engine)
    db = Session()
    for k, v in (("jellyfin_url", "http://jellyfin:8096"), ("jellyfin_api_key", "k" * 32),
                 ("jellyfin_user_id", ""), ("jellyfin_user_name", ""), ("session_secret", "s" * 64)):
        db.add(Setting(key=k, value=v))
    db.commit()
    db.close()
    return Session


def jellyfin(accounts):
    """requests.post for /Users/AuthenticateByName: {name: (id, is_admin)}."""
    def post(*args, **kwargs):
        uid, admin = accounts[kwargs["json"]["Username"]]
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"User": {"Id": uid, "Name": kwargs["json"]["Username"],
                                        "Policy": {"IsAdministrator": admin}}}
        return r
    return post


ACCOUNTS = {"boss": (ADMIN, True), "kid": (KID, False), "boss2": (OTHER_ADMIN, True)}


def login(db, name):
    response = Response()
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                       "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
    body = auth_router.login(auth_router.LoginRequest(username=name, password="x"), response, request, db)
    return body, response


class FirstSignIn(unittest.TestCase):
    def setUp(self):
        self.Session = fresh_engine(self)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.built, self.migrated = [], []
        for p in (mock.patch.object(auth_router, "_build_playlists_for_new_user", self.built.append),
                  mock.patch.object(auth_router, "migrate_orphaned_data_to_user",
                                    lambda db, uid: self.migrated.append(uid)),
                  mock.patch.object(auth_router.requests, "post", jellyfin(ACCOUNTS))):
            p.start()
            self.addCleanup(p.stop)

    def owner(self):
        self.db.expire_all()
        return self.db.query(TentacleUser).order_by(TentacleUser.id).first()

    def test_a_non_admin_first_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(HTTPException) as cm:
            login(self.db, "kid")
        self.assertEqual(403, cm.exception.status_code)
        self.assertIn("administrator", cm.exception.detail)
        self.assertIsNone(self.owner(), "a non-admin became the owner (first user)")
        self.assertEqual("", get_setting(self.db, "jellyfin_user_id"),
                         "a non-admin became the account Tentacle reads Jellyfin as")
        self.assertEqual(([], []), (self.migrated, self.built))

    def test_the_install_then_waits_for_an_admin(self):
        with self.assertRaises(HTTPException):
            login(self.db, "kid")
        # still the first-run state: the wizard's routes stay open
        self.assertIsNone(auth_router.require_admin(Request({"type": "http", "headers": []}), self.db))
        body, response = login(self.db, "boss")
        self.assertTrue(body["is_admin"])
        self.assertIn(auth_router.COOKIE_NAME, response.headers.get("set-cookie", ""))
        self.assertEqual(ADMIN, self.owner().jellyfin_user_id)
        self.assertEqual(ADMIN, get_setting(self.db, "jellyfin_user_id"))
        self.assertEqual("boss", get_setting(self.db, "jellyfin_user_name"))

    def test_an_admin_first_still_becomes_the_owner(self):
        login(self.db, "boss")
        self.assertEqual((ADMIN, True), (self.owner().jellyfin_user_id, self.owner().is_admin))
        self.assertEqual(ADMIN, get_setting(self.db, "jellyfin_user_id"))
        self.assertEqual(1, len(self.migrated))

    def test_after_the_owner_non_admins_sign_in_as_before(self):
        login(self.db, "boss")
        body, response = login(self.db, "kid")
        self.assertFalse(body["is_admin"])
        self.assertIn(auth_router.COOKIE_NAME, response.headers.get("set-cookie", ""))
        self.assertEqual(ADMIN, self.owner().jellyfin_user_id)
        self.assertEqual(ADMIN, get_setting(self.db, "jellyfin_user_id"), "jellyfin_user_id moved")
        self.assertEqual(1, len(self.migrated))

    def test_a_policy_missing_from_jellyfins_answer_counts_as_not_admin(self):
        def no_policy(*args, **kwargs):
            r = mock.Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = {"User": {"Id": KID, "Name": "kid"}}
            return r
        with mock.patch.object(auth_router.requests, "post", no_policy):
            with self.assertRaises(HTTPException) as cm:
                login(self.db, "kid")
        self.assertEqual(403, cm.exception.status_code)
        self.assertIsNone(self.owner())

    def test_only_a_real_true_counts_as_admin(self):
        for odd in ("true", "True", 1, None, {}):
            def answer(*args, odd=odd, **kwargs):
                r = mock.Mock()
                r.raise_for_status.return_value = None
                r.json.return_value = {"User": {"Id": KID, "Name": "kid", "Policy": {"IsAdministrator": odd}}}
                return r
            with self.subTest(odd=odd), mock.patch.object(auth_router.requests, "post", answer):
                with self.assertRaises(HTTPException) as cm:
                    login(self.db, "kid")
                self.assertEqual(403, cm.exception.status_code)
                self.assertIsNone(self.owner())
                self.assertEqual("", get_setting(self.db, "jellyfin_user_id"))

    def test_a_wrong_password_is_still_a_401(self):
        def refused(*args, **kwargs):
            r = mock.Mock(status_code=401)
            # requests attaches the response; login reads its status (#392).
            r.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized", response=r)
            return r
        with mock.patch.object(auth_router.requests, "post", refused):
            with self.assertRaises(HTTPException) as cm:
                login(self.db, "kid")
        self.assertEqual(401, cm.exception.status_code)

    def test_an_admin_and_a_non_admin_at_once_leave_an_admin_owner(self):
        for _ in range(10):
            self.db.query(TentacleUser).delete()
            self.db.query(Setting).filter(Setting.key == "jellyfin_user_id").update({"value": ""})
            self.db.commit()
            self.migrated.clear()
            barrier, errors, lock = threading.Barrier(2), [], threading.Lock()

            def run(name):
                db = self.Session()
                try:
                    barrier.wait(timeout=10)
                    login(db, name)
                except HTTPException as e:
                    with lock:
                        errors.append((name, e.status_code))
                finally:
                    db.close()

            threads = [threading.Thread(target=run, args=(n,)) for n in ("kid", "boss")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertIn(errors, ([], [("kid", 403)]))
            self.assertEqual((ADMIN, True), (self.owner().jellyfin_user_id, self.owner().is_admin))
            self.assertEqual(ADMIN, get_setting(self.db, "jellyfin_user_id"))
            self.assertEqual(1, len(self.migrated))


if __name__ == "__main__":
    unittest.main()
