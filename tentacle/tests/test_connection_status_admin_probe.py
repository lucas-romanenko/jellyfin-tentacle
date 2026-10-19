"""The Jellyfin connection test must tell a non-admin key apart (#129).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The key needs to be an administrator's, because the plugin's refresh (what
live-updates the Android TV app) requires elevation. The check probed
/System/Configuration and took a 403 to mean "not an admin", but on Jellyfin
10.11 any signed-in user may read that route, so a non-admin key passed and
the failure surfaced much later as silent plugin refresh errors. /Plugins is
RequiresElevation, as the plugin refresh is.
"""
import logging
import shutil
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Resp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# What Jellyfin 10.11.8's OpenAPI document says each route needs.
ELEVATED = {"Plugins"}
ANY_USER = {"System/Info", "System/Configuration"}


def _jellyfin_answering_as(is_admin):
    def get(url, headers=None, timeout=None, **kw):
        path = url.split("://", 1)[1].split("/", 1)[1]
        if path in ELEVATED and not is_admin:
            return _Resp(403)
        return _Resp(200)
    return get


class AdminProbe(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jf:8096"), ("jellyfin_api_key", "k")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()

    def _status(self, is_admin):
        import routers.settings as settings_router
        with mock.patch.object(settings_router.requests, "get", _jellyfin_answering_as(is_admin)):
            return settings_router.connection_status(db=self.db)["jellyfin"]

    def test_a_non_admin_key_is_reported(self):
        result = self._status(is_admin=False)
        self.assertFalse(result["ok"])
        self.assertIn("not an administrator", result["error"])

    def test_an_admin_key_is_ok(self):
        self.assertTrue(self._status(is_admin=True)["ok"])


if __name__ == "__main__":
    unittest.main()
