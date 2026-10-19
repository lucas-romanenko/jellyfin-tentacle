"""#212: a new user's first requests must not race to create their row.

A Jellyfin user who has never used Tentacle has no TentacleUser row, and the
first page load fires several requests at once (the plugin's Sections,
Toolbar, Hero and HeroConfig; the TV app's rows). Each saw no row and
inserted one; all but the first failed on the unique jellyfin_user_id and
answered 500, so for a few seconds that user got Jellyfin's native home. The
dashboard login had the same race, and two brand-new users logging in at once
on a fresh install could both be taken for the first user.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import threading
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from models.database import Base, Setting, TentacleUser
from routers import auth as auth_router

NEW = "c" * 32
THREADS = 6


class _SlowProfiles(dict):
    """The profile lookup sits between "is there a row?" and the insert: slowing
    it lets every simultaneous request get past the check before any commits."""

    def get(self, key, default=None):
        time.sleep(0.2)
        return super().get(key, default)


def _plugin_request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                    "query_string": f"api_key=tok&userId={NEW}".encode()})


def _login_request():
    return Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                    "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})


def _jellyfin_login(ids_by_name, barrier=None, admin=False):
    """requests.post for /Users/AuthenticateByName, answering by username.
    Patched once around all the threads: patching per thread would race."""
    def post(*args, **kwargs):
        name = kwargs["json"]["Username"]
        if barrier is not None:
            barrier.wait(timeout=10)   # every login reaches the row check together
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"User": {"Id": ids_by_name[name], "Name": name,
                                        "Policy": {"IsAdministrator": admin}}}
        return r
    return post


def _login(db, name):
    return auth_router.login(auth_router.LoginRequest(username=name, password="x"),
                             Response(), _login_request(), db)["id"]


class _Base(unittest.TestCase):
    existing_users = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # As in production: WAL, and a busy timeout so a writer waits its turn.
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _wal(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for k, v in (("jellyfin_url", "http://jf:8096"), ("jellyfin_api_key", "KEY"),
                     ("session_secret", "s3cret")):
            db.add(Setting(key=k, value=v))
        if self.existing_users:
            db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="Owner", is_admin=True))
        db.commit()
        db.close()
        self.built = []
        self._patches = [
            mock.patch.object(auth_router, "_build_playlists_for_new_user", self.built.append),
            mock.patch.object(auth_router, "_resolve_token_user", return_value=NEW),
            mock.patch.object(auth_router, "_refresh_from_jellyfin", return_value=True),
            mock.patch.object(auth_router, "_token_profiles", _SlowProfiles({NEW: {"name": "Teen"}})),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _all_at_once(self, call):
        """Run call(db) in THREADS threads released together; (results, errors)."""
        barrier = threading.Barrier(THREADS)
        results, errors, lock = [], [], threading.Lock()

        def run():
            db = self.Session()
            try:
                barrier.wait(timeout=10)
                out = call(db)
                with lock:
                    results.append(out)
            except Exception as e:     # noqa: BLE001 -- a 500 in production
                with lock:
                    errors.append(repr(e))
            finally:
                db.close()

        threads = [threading.Thread(target=run) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results, errors

    def _rows(self, jellyfin_user_id):
        db = self.Session()
        try:
            return db.query(TentacleUser).filter_by(jellyfin_user_id=jellyfin_user_id).all()
        finally:
            db.close()


class TestPluginFirstCalls(_Base):
    def test_simultaneous_first_calls_all_get_the_one_new_row(self):
        results, errors = self._all_at_once(
            lambda db: auth_router.get_user_from_request(_plugin_request(), db).id)
        self.assertEqual(errors, [])
        rows = self._rows(NEW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(results, [rows[0].id] * THREADS)
        self.assertEqual(self.built, [rows[0].id], "playlists are built once, for the one row")

    def test_a_session_that_looked_before_the_row_existed_still_gets_it(self):
        # The issue's unit test: the second session asked (and found nothing)
        # before the first committed.
        late = self.Session()
        self.assertIsNone(late.query(TentacleUser).filter_by(jellyfin_user_id=NEW).first())
        first = self.Session()
        made = auth_router._provision_plugin_user(first, NEW)
        first.close()
        again = auth_router._provision_plugin_user(late, NEW)
        self.assertEqual(again.id, made.id)
        late.close()
        self.assertEqual(len(self._rows(NEW)), 1)
        self.assertEqual(self.built, [made.id])

    def test_a_row_made_outside_the_lock_is_used_not_a_500(self):
        # Another process created the row between this one's check and its
        # insert: the insert loses on the unique id and the row that won is used.
        other = self.Session()
        other.add(TentacleUser(jellyfin_user_id=NEW, display_name="Teen"))
        other.commit()
        won = other.query(TentacleUser).filter_by(jellyfin_user_id=NEW).one().id
        other.close()
        real, calls = auth_router._user_row, []

        def missed_it_first(db, uid):
            calls.append(uid)
            return None if len(calls) == 1 else real(db, uid)

        db = self.Session()
        with mock.patch.object(auth_router, "_user_row", missed_it_first):
            user = auth_router._provision_plugin_user(db, NEW)
        self.assertEqual(user.id, won)
        db.close()
        self.assertEqual(self.built, [], "the request that made the row builds its playlists")


class TestLoginFirstCalls(_Base):
    def test_the_same_new_user_logging_in_at_once_gets_one_row(self):
        post = _jellyfin_login({"Teen": NEW}, barrier=threading.Barrier(THREADS))
        with mock.patch.object(auth_router.requests, "post", post):
            results, errors = self._all_at_once(lambda db: _login(db, "Teen"))
        self.assertEqual(errors, [])
        rows = self._rows(NEW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(results, [rows[0].id] * THREADS)
        self.assertEqual(self.built, [rows[0].id])

    def test_a_login_whose_insert_loses_logs_in_to_the_winner(self):
        other = self.Session()
        other.add(TentacleUser(jellyfin_user_id=NEW, display_name="Old name"))
        other.commit()
        won = other.query(TentacleUser).filter_by(jellyfin_user_id=NEW).one().id
        other.close()
        real, calls = auth_router._user_row, []

        def missed_it_first(db, uid):
            calls.append(uid)
            return None if len(calls) == 1 else real(db, uid)

        db = self.Session()
        with mock.patch.object(auth_router, "_user_row", missed_it_first), \
                mock.patch.object(auth_router.requests, "post", _jellyfin_login({"Teen": NEW})):
            self.assertEqual(_login(db, "Teen"), won)
        db.close()
        self.assertEqual([u.display_name for u in self._rows(NEW)], ["Teen"], "the login still updates the row")
        self.assertEqual(self.built, [])


class TestFreshInstallFirstUser(_Base):
    existing_users = False

    def test_only_one_of_two_simultaneous_new_users_becomes_the_first_user(self):
        migrated = []

        def slow_migration(db, user_id):
            time.sleep(0.2)     # both logins would be past "count() == 0" by now
            migrated.append(user_id)

        names = iter(["Ann", "Bob"] * THREADS)
        pick = threading.Lock()

        def login(db):
            with pick:
                name = next(names)
            return _login(db, name)

        with mock.patch.object(auth_router, "migrate_orphaned_data_to_user", slow_migration), \
                mock.patch.object(auth_router.requests, "post",
                                  _jellyfin_login({"Ann": "d" * 32, "Bob": "e" * 32}, admin=True)):
            results, errors = self._all_at_once(login)
        self.assertEqual(errors, [])
        self.assertEqual(len(migrated), 1, "exactly one login inherits the pre-multi-user data")
        self.assertEqual(len(self._rows("d" * 32)) + len(self._rows("e" * 32)), 2)


if __name__ == "__main__":
    unittest.main()
