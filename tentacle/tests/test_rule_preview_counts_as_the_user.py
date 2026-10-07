"""The rule builder's "N items match" must count what the playlist will hold.

Run from the tentacle/ directory:
  python tests/hermetic.py discover -s tests -p test_rule_preview_counts_as_the_user.py

POST /api/smartlists/preview-count (routers/smartlists.py preview_count) asks
Jellyfin's recursive /Items with the server API key and NO user, so Jellyfin
answers as the server: every library, no parental limit, and (Jellyfin 10.11,
see PR #112) metadata as it was several edits ago. The playlist the rule then
makes is built by services/smartlists._process_single_playlist_locked, which
asks the same query AS the playlist's owner (query["user_id"] = user_id ->
UserId=...), so it gets only what that user may see.

For a user whose library access (or parental rating) is limited the preview
therefore promises more titles than the playlist will ever hold, and counts
titles from libraries that user cannot open.

The fake Jellyfin below answers like the real one: a query with UserId gets
that user's view, a query without one gets everything.
"""
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

KID = "b" * 32  # a Jellyfin user with access to the "Family" library only

# Three films match "Year > 2020"; one sits in a library the user cannot open.
FILMS = [
    {"Id": "f1", "Type": "Movie", "Name": "Family One", "ProductionYear": 2021,
     "ProviderIds": {"Tmdb": "101"}, "_library": "Family"},
    {"Id": "f2", "Type": "Movie", "Name": "Family Two", "ProductionYear": 2022,
     "ProviderIds": {"Tmdb": "102"}, "_library": "Family"},
    {"Id": "f3", "Type": "Movie", "Name": "Adults Only", "ProductionYear": 2023,
     "ProviderIds": {"Tmdb": "103"}, "_library": "Adults"},
]
ACCESS = {KID: {"Family"}}

CONDITIONS = [{"field": "year", "operator": "greater_than", "value": "2020"}]


class FakeJellyfinItems:
    """JellyfinService._get for /Items, scoped like Jellyfin scopes it."""

    def __init__(self, access=ACCESS):
        self.access = access
        self.calls = []

    def __call__(self, svc, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path != "/Items":
            return None
        uid = params.get("UserId")
        items = FILMS if not uid else [f for f in FILMS if f["_library"] in self.access.get(uid, set())]
        items = [{k: v for k, v in f.items() if not k.startswith("_")} for f in items]
        return {"Items": items, "TotalRecordCount": len(items)}


class _App(unittest.TestCase):
    access = ACCESS

    def setUp(self):
        import main
        import models.database as mdb
        from routers import auth
        root = temp_dir(self)
        engine = create_engine(f"sqlite:///{root}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        mdb.set_setting(db, "jellyfin_url", "http://jf.test:8096")
        mdb.set_setting(db, "jellyfin_api_key", "server-key")
        user = mdb.TentacleUser(jellyfin_user_id=KID, display_name="Kid", is_admin=False)
        db.add(user); db.commit(); db.refresh(user); db.expunge(user)
        db.close()

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()
        self.app = main.app
        self.app.dependency_overrides[mdb.get_db] = _db
        for dep in (auth.get_user_from_request, auth.get_current_user):
            self.app.dependency_overrides[dep] = lambda: user
        self.c = TestClient(self.app, raise_server_exceptions=False)

        from services.jellyfin import JellyfinService
        self.jf_items = FakeJellyfinItems(self.access)
        jf_items = self.jf_items
        p = mock.patch.object(JellyfinService, "_get",
                              lambda svc, path, params=None: jf_items(svc, path, params))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def preview(self):
        r = self.c.post("/api/smartlists/preview-count",
                        json={"apply_to": "movies", "conditions": CONDITIONS})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()["count"]

    def playlist_the_rule_makes(self):
        """What the playlist builder puts in the rule's playlist for this user."""
        from services.jellyfin import JellyfinService
        from services.smartlists import (_build_config, _conditions_to_expressions,
                                         _process_single_playlist_locked)
        config = _build_config("New Films", "New Films", ["Movie"], "fid", True, KID,
                               expressions=_conditions_to_expressions(CONDITIONS))
        jf = JellyfinService("http://jf.test:8096", "server-key", KID)
        created = {}

        def create_playlist(name, item_ids=None, user_id=None, is_public=False):
            created["ids"] = list(item_ids or [])
            return "PL1"
        jf.create_playlist = create_playlist
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        _process_single_playlist_locked(jf, Path(temp_dir(self)), config, KID, stats, db=None)
        self.assertEqual(1, stats["created"], stats)
        return created["ids"]


class RulePreviewCountsAsTheUser(_App):
    def test_preview_count_equals_what_the_playlist_will_hold(self):
        held = self.playlist_the_rule_makes()
        self.assertEqual(["f1", "f2"], held)  # the builder asks as the user
        self.assertEqual(len(held), self.preview(),
                         "the rule builder promises a different number of titles "
                         "than the playlist it makes will hold")

    def test_preview_asks_jellyfin_as_the_caller(self):
        self.preview()
        items_queries = [p for path, p in self.jf_items.calls if path == "/Items"]
        self.assertTrue(items_queries)
        self.assertTrue(all(p.get("UserId") == KID for p in items_queries),
                        "preview-count queried Jellyfin as the server (no UserId), "
                        "counting titles from libraries the caller cannot open")


class FullAccessUnchanged(_App):
    """Control: for a user who can open every library both numbers already agree."""
    access = {KID: {"Family", "Adults"}}

    def test_same_count_for_a_user_with_every_library(self):
        held = self.playlist_the_rule_makes()
        self.assertEqual(3, len(held))
        self.assertEqual(3, self.preview())


if __name__ == "__main__":
    unittest.main()
