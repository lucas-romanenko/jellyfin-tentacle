"""#538: the rule builder's "N items match" must count as the signed-in user.

Run from the tentacle/ directory:  python -m unittest discover -s tests

preview_count asked Jellyfin's /Items without a UserId, so Jellyfin answered as
the server: every library, no parental or tag limits, and (10.11) metadata as
it was several edits ago. The rule's playlist is built as its owner (#112), so
a user with limited library access was promised titles the playlist never got.
Rules belong to the user who saves them, so the caller is the owner.
"""
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

KID = "b" * 32     # can open the "Family" library only
ADMIN = "a" * 32   # can open every library
FILMS = [("f1", 2021, "Family"), ("f2", 2022, "Family"), ("f3", 2023, "Adults")]
CONDITIONS = [{"field": "year", "operator": "greater_than", "value": "2020"}]


def fake_get(svc, path, params=None):
    """Jellyfin's /Items: with UserId, that user's libraries; without, everything."""
    uid = (params or {}).get("UserId")
    items = [{"Id": i, "Type": "Movie", "Name": i, "ProductionYear": y, "ProviderIds": {"Tmdb": i[1:]}}
             for i, y, lib in FILMS if uid in (None, ADMIN) or lib == "Family"]
    return {"Items": items, "TotalRecordCount": len(items)}


class RulePreviewCountsAsTheUser(unittest.TestCase):
    def setUp(self):
        import main
        import models.database as mdb
        from services.jellyfin import JellyfinService
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        mdb.set_setting(db, "jellyfin_url", "http://jf.test:8096")
        mdb.set_setting(db, "jellyfin_api_key", "server-key")
        db.close()

        def get_db():
            s = self.Session()
            try:
                yield s
            finally:
                s.close()
        self.app = main.app
        self.app.dependency_overrides[mdb.get_db] = get_db
        self.addCleanup(self.app.dependency_overrides.clear)
        p = mock.patch.object(JellyfinService, "_get", fake_get)
        p.start()
        self.addCleanup(p.stop)

    def _sign_in(self, jf_id, is_admin):
        import models.database as mdb
        from routers import auth
        db = self.Session()
        user = mdb.TentacleUser(jellyfin_user_id=jf_id, display_name=jf_id[:1], is_admin=is_admin)
        db.add(user); db.commit(); db.refresh(user); db.expunge(user); db.close()
        self.app.dependency_overrides[auth.get_user_from_request] = lambda: user

    def _promised(self):
        r = TestClient(self.app).post("/api/smartlists/preview-count",
                                      json={"apply_to": "movies", "conditions": CONDITIONS})
        self.assertEqual(200, r.status_code)
        return r.json()["count"]

    def _held(self, jf_id):
        """What the rule's playlist holds, built as its owner."""
        from services.jellyfin import JellyfinService
        from services.smartlists import (_build_config, _conditions_to_expressions,
                                         _process_single_playlist_locked)
        config = _build_config("New Films", "New Films", ["Movie"], "fid", True, jf_id,
                               expressions=_conditions_to_expressions(CONDITIONS))
        jf = JellyfinService("http://jf.test:8096", "server-key", jf_id)
        held = {}

        def create_playlist(name, item_ids=None, user_id=None, is_public=False):
            held["ids"] = list(item_ids or [])
            return "PL1"
        jf.create_playlist = create_playlist
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        _process_single_playlist_locked(jf, Path(temp_dir(self)), config, jf_id, stats, db=None)
        return held["ids"]

    def test_a_user_with_limited_libraries_is_promised_what_the_playlist_holds(self):
        self._sign_in(KID, is_admin=False)
        held = self._held(KID)
        self.assertEqual(["f1", "f2"], held)
        self.assertEqual(len(held), self._promised(),
                         "the rule builder promises a different number of titles than its playlist holds")

    def test_a_user_who_sees_everything_gets_the_same_count_as_before(self):
        self._sign_in(ADMIN, is_admin=True)
        self.assertEqual(3, self._promised())
        self.assertEqual(3, len(self._held(ADMIN)))


if __name__ == "__main__":
    unittest.main()
