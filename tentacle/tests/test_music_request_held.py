"""An album request from someone whose requests wait for an admin is kept (#714 follow-up).

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_music_request_held.py"

POST /api/music/request holds the request for everyone with request_mode
"ask" (the default for every non-admin) and passes it a title with
`(body.title or "")[:300]`, but AlbumRequest has no `title` field, so the
route raised AttributeError and answered 500 to every non-admin, from the
dashboard and from Jellyfin web (the plugin forwards to the same route).
Admins and people set to "direct" never reach that line.
"""
import json
import logging
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

RG = "11111111-1111-1111-1111-111111111111"


class MusicRequestHeld(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.ann = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Ann", is_admin=False)  # "ask"
        self.db.add_all([self.admin, self.ann])
        self.db.commit()
        mdb.set_setting(self.db, "music_enabled", "true")
        p = mock.patch("services.pushover.send", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def post(self, user, body):
        from routers import music
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(music.webhook_router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: user
        return TestClient(app, raise_server_exceptions=False).post("/api/music/request", json=body)

    def test_an_album_request_that_waits_for_an_admin_is_kept(self):
        with mock.patch("services.media_requests.request_album") as ra:
            r = self.post(self.ann, {"mbid": RG})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(1, r.json()["pending"])
        ra.assert_not_called()                      # never reaches Lidarr before an admin says yes
        row = self.db.query(mdb.MediaRequest).one()
        self.assertEqual(("album", "pending", RG), (row.kind, row.status, json.loads(row.payload)["mbid"]))

    def test_the_title_comes_from_what_tentacle_knows(self):
        self.db.add(mdb.MusicAlbum(mbid=RG, title="Radio City", artist_name="Big Star"))
        self.db.commit()
        with mock.patch("services.media_requests.request_album"):
            r = self.post(self.ann, {"mbid": RG, "tracks": 12})
        self.assertEqual(200, r.status_code, r.text)
        row = self.db.query(mdb.MediaRequest).one()
        self.assertEqual("Big Star — Radio City", row.title)
        self.assertEqual({"tracks": 12}, json.loads(row.payload)["choice"])

    def test_an_admin_request_goes_straight_through(self):
        with mock.patch("services.media_requests.request_album", return_value={"added": 1}) as ra:
            r = self.post(self.admin, {"mbid": RG})
        self.assertEqual(200, r.status_code, r.text)
        ra.assert_called_once()
        self.assertEqual(0, self.db.query(mdb.MediaRequest).count())


if __name__ == "__main__":
    unittest.main()
