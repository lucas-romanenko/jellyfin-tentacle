"""#264: "Add N Missing to Radarr" on a list holding movies and shows must send
only the movies (a show's TMDB id names a different film in Radarr's movie
namespace), and "…to Sonarr" only the shows. An item with no TMDB id (IMDb
only) is sent to neither.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class TestAddMissingByType(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = mdb.TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=False)
        self.db.add(self.user)
        self.db.add(mdb.ListSubscription(id=1, user_id=1, name="Mixed", type="trakt",
                                         url="https://example.invalid/list", tag="Mixed"))
        self.db.add_all([
            mdb.ListItem(list_id=1, tmdb_id=603, media_type="movie", title="The Matrix"),
            mdb.ListItem(list_id=1, tmdb_id=1396, media_type="series", title="Breaking Bad"),
            mdb.ListItem(list_id=1, tmdb_id=None, media_type="movie", title="IMDb only"),
            mdb.ListItem(list_id=1, tmdb_id=77, media_type=None, title="Old row, no type"),
        ])
        self.db.commit()

    def _sent(self, fn_name, request_name):
        import routers.lists as lists
        outcome = mock.Mock()
        outcome.as_response.return_value = {}
        with mock.patch.object(lists.media_requests, request_name, return_value=outcome) as req:
            getattr(lists, fn_name)(1, None, self.db, self.user)
        args, kwargs = req.call_args
        return list(kwargs.get("tmdb_ids", args[1] if len(args) > 1 else None))

    def test_radarr_gets_only_movies(self):
        self.assertEqual(self._sent("add_missing_to_radarr", "request_movies"), [603, 77])

    def test_sonarr_gets_only_series(self):
        self.assertEqual(self._sent("add_missing_to_sonarr", "request_series"), [1396])


if __name__ == "__main__":
    unittest.main()
