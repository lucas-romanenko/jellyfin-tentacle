"""Errors shown to users say what failed, not where the service lives.

Run from the tentacle/ directory:  python -m unittest discover -s tests

When Radarr, Sonarr, Lidarr or Jellyfin could not be reached, several
replies carried the exception text, which names the service's internal
address ("HTTPConnectionPool(host='10.0.0.5', port=7878): ..."):
  - /api/lists/radarr-profiles, /sonarr-profiles, /radarr-folders,
    /sonarr-folders (any signed-in user; the Add dialogs);
  - a music request while Lidarr is down ("Can't reach Lidarr at http://...");
  - the Activity "problems" entries' detail (any signed-in user), and a
    requester's "Download" when Radarr/Sonarr can't be reached;
  - the login picker and login while Jellyfin is down (before sign-in).
They now name the failure (and the error type); the full text goes to the
log. Admins still see the problems' detail.
"""
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402

ADDR = "10.9.8.7"
ERR = requests.ConnectionError(f"HTTPConnectionPool(host='{ADDR}', port=7878): Max retries exceeded")


class _Db(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k in ("radarr", "sonarr"):
            mdb.set_setting(self.db, f"{k}_url", f"http://{ADDR}:7878")
            mdb.set_setting(self.db, f"{k}_api_key", "k" * 32)
        self.user = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="User", is_admin=False)
        self.db.add(self.user)
        self.db.commit()


class ListsRoutes(_Db):
    def test_profiles_and_folders(self):
        from routers import lists
        with mock.patch.object(lists, "_profiles_with_default", side_effect=ERR), \
                mock.patch.object(lists.requests, "get", side_effect=ERR):
            for route in (lists.radarr_profiles, lists.sonarr_profiles, lists.radarr_folders, lists.sonarr_folders):
                with self.subTest(route=route.__name__), self.assertRaises(HTTPException) as ctx:
                    route(db=self.db, user=self.user)
                self.assertEqual(502, ctx.exception.status_code)
                self.assertNotIn(ADDR, ctx.exception.detail)
                self.assertIn("ConnectionError", ctx.exception.detail)


class LidarrText(unittest.TestCase):
    def test_unreachable_and_bad_address(self):
        from services import lidarr
        client = lidarr.LidarrClient(f"http://{ADDR}:8686", "k")
        for exc in (requests.ConnectionError(f"host='{ADDR}'"), requests.exceptions.InvalidURL(f"http://{ADDR}:8686/x")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(lidarr.requests, "request", side_effect=exc), \
                    mock.patch.object(lidarr, "RETRY_DELAYS", (0,)), mock.patch.object(lidarr.time, "sleep"):
                with self.assertRaises(lidarr.LidarrError) as ctx:
                    client._request("GET", "/api/v1/system/status", retries=0)
                self.assertNotIn(ADDR, ctx.exception.message)
                self.assertIn("Lidarr", ctx.exception.message)


class LoginText(_Db):
    def test_login_picker_and_login(self):
        from routers import auth
        mdb.set_setting(self.db, "jellyfin_url", f"http://{ADDR}:8096")
        with mock.patch.object(auth.requests, "get", side_effect=ERR), self.assertRaises(HTTPException) as ctx:
            auth.get_jellyfin_users(db=self.db)
        self.assertNotIn(ADDR, ctx.exception.detail)
        with mock.patch.object(auth.requests, "post", side_effect=ERR), self.assertRaises(HTTPException) as ctx:
            auth.login(auth.LoginRequest(username="u", password="p"), response=mock.Mock(), request=mock.Mock(), db=self.db)
        self.assertEqual(502, ctx.exception.status_code)
        self.assertNotIn(ADDR, ctx.exception.detail)


class GrabText(_Db):
    def test_grab_when_radarr_is_down(self):
        from services import arr_insight
        with mock.patch.object(arr_insight.requests, "post", side_effect=ERR), \
                mock.patch.object(arr_insight, "_conn", return_value=(f"http://{ADDR}:7878", "k")):
            with self.assertRaises(arr_insight.InsightError) as ctx:
                arr_insight.grab(self.db, "movie", 1, 0, "g", 1)
        self.assertNotIn(ADDR, str(ctx.exception))
        self.assertIn("ConnectionError", str(ctx.exception))


class ActivityProblems(unittest.TestCase):
    def test_detail_only_for_admins(self):
        import inspect
        from routers import activity
        src = inspect.getsource(activity.get_activity)
        self.assertIn('if k != "detail"', src)
        self.assertIn("if not is_admin:", src.split("searching_problems(db)", 1)[1][:200])


if __name__ == "__main__":
    unittest.main()
