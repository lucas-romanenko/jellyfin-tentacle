"""The login picker's avatar URL must be one a BROWSER can reach.

Run from the tentacle/ directory:  python -m unittest discover -s tests

/api/auth/users handed the browser `jellyfin_url` to build avatar <img> URLs
from. That is the address TENTACLE uses to reach Jellyfin -- often a docker
service name -- so every avatar was a broken image. `jellyfin_public_url`
exists for exactly this and routers/discover.py already prefers it for links.
"""
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return [{"Id": "u1", "Name": "Alice", "HasPassword": True, "PrimaryImageTag": "tag1"}]


class AvatarUrl(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "jellyfin_url", "http://jellyfin:8096/")
        self.db.commit()

    def _users(self):
        from routers import auth
        with mock.patch.object(auth.requests, "get", return_value=_Resp()) as get:
            users = auth.get_jellyfin_users(db=self.db)
        return users, get.call_args[0][0]

    def test_the_public_url_is_what_the_browser_is_given(self):
        from models.database import set_setting
        set_setting(self.db, "jellyfin_public_url", "https://tv.example.org/")
        self.db.commit()
        users, fetched = self._users()
        self.assertEqual("https://tv.example.org", users[0]["jellyfin_url"])
        self.assertTrue(fetched.startswith("http://jellyfin:8096/"),
                        "Tentacle itself must still talk to Jellyfin on the internal address")

    def test_without_a_public_url_nothing_changes(self):
        users, _ = self._users()
        self.assertEqual("http://jellyfin:8096", users[0]["jellyfin_url"])


if __name__ == "__main__":
    unittest.main()
