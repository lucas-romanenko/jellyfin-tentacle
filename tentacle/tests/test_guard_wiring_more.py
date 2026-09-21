"""Pins two deletion-adjacent guards the suite does not cover (#43 M9, M10).

Mutation-tested on 9bde42e: both tests pass as-is, and each fails when the
guard it names is reverted, while the rest of the suite (293 tests) stays green.
Run from tentacle/:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class StreamHealthSkipsOptedOutTitles(unittest.TestCase):
    """M9: services/stream_health._check_item must not probe a title whose
    .strm management the user switched off (strm_disabled)."""

    def test_strm_disabled_title_is_never_probed(self):
        import services.stream_health as sh
        strm = Path(tempfile.mkdtemp()) / "Heat (1995).strm"
        strm.write_text("http://provider.example/movie/u/p/123.mkv")
        item = SimpleNamespace(strm_disabled=True, strm_path=str(strm), provider_id=1,
                               tmdb_id=949, title="Heat")
        with mock.patch.object(sh, "check_stream", return_value=False) as probe, \
             mock.patch.object(sh, "_mark_bad") as mark:
            result = sh._check_item(db=None, item=item, media_type="movie", providers={1: None})
        self.assertIsNone(result)
        probe.assert_not_called()
        mark.assert_not_called()


class StrmManagedNeedsAdmin(unittest.TestCase):
    """M10: POST /api/library/strm-managed/... switches a title between .strm and
    downloaded copies (and can delete its files); it must stay admin-only."""

    def test_non_admin_is_refused(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        import main
        import models.database as mdb
        import routers.auth as auth

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        user = mdb.TentacleUser(jellyfin_user_id="jf-u", display_name="viewer", is_admin=False)
        db.add(user)
        db.commit()

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()

        main.app.dependency_overrides[mdb.get_db] = _db
        # Whatever resolves the caller, it resolves to this non-admin user.
        main.app.dependency_overrides[auth.get_user_from_request] = lambda: user
        self.addCleanup(main.app.dependency_overrides.clear)
        with mock.patch.object(auth, "get_user_from_request", return_value=user):
            r = TestClient(main.app, raise_server_exceptions=False).post(
                "/api/library/strm-managed/movie/949", json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
