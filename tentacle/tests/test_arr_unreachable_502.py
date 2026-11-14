"""Health page Missing tabs and the root-folder pickers when Radarr/Sonarr can't
be reached: a clear 502 naming the app, like the other arr routes, instead of
an unhandled exception (500 "Internal server error").

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting
from routers import health, radarr, sonarr
from tmp_dirs import temp_dir


class ArrUnreachable(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add_all([Setting(key="radarr_url", value="http://radarr.example"),
                         Setting(key="radarr_api_key", value="k"),
                         Setting(key="sonarr_url", value="http://sonarr.example"),
                         Setting(key="sonarr_api_key", value="k")])
        self.db.commit()
        self.addCleanup(self.db.close)
        patcher = mock.patch("requests.get", side_effect=requests.ConnectionError("down"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_502(self, call, app):
        with self.assertRaises(HTTPException) as ctx:
            call()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn(app, ctx.exception.detail)
        self.assertIn("ConnectionError", ctx.exception.detail)

    def test_missing_movies(self):
        self.assert_502(lambda: health.missing_movies(db=self.db), "Radarr")

    def test_missing_episodes(self):
        self.assert_502(lambda: health.missing_episodes(db=self.db), "Sonarr")

    def test_radarr_root_folders(self):
        self.assert_502(lambda: radarr.get_root_folders(db=self.db), "Radarr")

    def test_sonarr_root_folders(self):
        self.assert_502(lambda: sonarr.get_root_folders(db=self.db), "Sonarr")

    def test_an_arr_that_answers_still_works(self):
        ok = mock.Mock(status_code=200, json=lambda: [{"path": "/movies", "freeSpace": 5}])
        ok.raise_for_status = lambda: None
        with mock.patch("requests.get", return_value=ok):
            self.assertEqual(radarr.get_root_folders(db=self.db), [{"path": "/movies", "freeSpace": 5}])


if __name__ == "__main__":
    unittest.main()
