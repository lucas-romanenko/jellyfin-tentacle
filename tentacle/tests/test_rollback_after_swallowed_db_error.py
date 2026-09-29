"""A step that swallows a database error rolls the session back first.

A failed flush (a constraint, "database is locked") leaves a SQLAlchemy
session refusing every statement until it is rolled back. Two handlers
logged such an error and carried on with the same session:

* run_full_jellyfin_pipeline's tag step: the playlist refresh after it then
  failed too, so after "Sync now" new titles reached no playlist until the
  nightly job;
* nightly discovery, per provider: every later provider's discovery and the
  new-content notice then failed too.
"""
import json
import logging
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir


def _session(owner):
    engine = create_engine(f"sqlite:///{temp_dir(owner)}/t.db", connect_args={"check_same_thread": False})
    owner.addCleanup(engine.dispose)
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()   # as SessionLocal
    owner.addCleanup(db.close)
    return db


def _poison(db):
    """A real failed flush: the session now refuses every statement."""
    db.add(mdb.Movie(tmdb_id=1, title="again", source="radarr"))
    db.flush()


class PipelineTagStep(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.db = _session(self)
        mdb.set_setting(self.db, "jellyfin_url", "http://jellyfin.test:8096")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        self.db.add(mdb.Movie(tmdb_id=1, title="Heat", source="radarr"))
        self.db.commit()
        self.refreshed = []

    def _run(self, tag_refresh, push=lambda db, prefix: 0):
        import services.jellyfin as jfs

        def refresh_playlists(db, *a, **k):
            self.refreshed.append(db.query(mdb.Setting).count())   # its first statement
            return {}
        with mock.patch.object(jfs.JellyfinService, "wait_for_library_scan", lambda *a, **k: True), \
                mock.patch.object(jfs, "push_tags_to_jellyfin", push), \
                mock.patch("services.tagger.refresh_recently_added_tags", tag_refresh), \
                mock.patch("services.smartlists.refresh_smartlist_playlists", refresh_playlists):
            return jfs.run_full_jellyfin_pipeline(self.db, log_prefix="VOD sync")

    def test_a_database_error_in_the_tag_step_still_refreshes_the_playlists(self):
        stats = self._run(_poison)
        self.assertTrue(stats["playlists_refreshed"], stats)
        self.assertEqual(1, len(self.refreshed))
        self.assertEqual(1, self.db.query(mdb.Movie).count())

    def test_committed_tag_changes_survive_a_later_error_in_the_step(self):
        def tag_refresh(db):
            db.query(mdb.Movie).one().tags = ["Recently Added Movies"]
            db.commit()                                  # refresh_recently_added_tags commits its work

        def push(db, prefix):
            raise ConnectionError("Jellyfin went away")  # not a database error
        stats = self._run(tag_refresh, push)
        self.assertTrue(stats["playlists_refreshed"])
        self.db.expire_all()
        self.assertEqual(["Recently Added Movies"], self.db.query(mdb.Movie).one().tags)

    def test_a_rollback_that_fails_does_not_escape_the_pipeline(self):
        def bad_rollback():
            raise RuntimeError("connection invalidated")
        with mock.patch.object(self.db, "rollback", bad_rollback):
            stats = self._run(_poison)                   # must not raise
        self.assertFalse(stats["playlists_refreshed"])   # step 4 ran and failed on its own


class DiscoveryPerProvider(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.db = _session(self)
        self.db.add(mdb.Movie(tmdb_id=1, title="Heat", source="radarr"))
        for name in ("A", "B"):
            self.db.add(mdb.Provider(name=name, server_url=f"http://{name.lower()}.test", username="u",
                                     password="p", active=True, live_tv_enabled=False))
        self.db.commit()

    def _discover(self):
        import services.discovery as disc
        real = disc._refresh_vod_categories

        def refresh(db, provider):
            if provider.name == "A":
                _poison(db)
            return real(db, provider)

        def fetch(provider):
            return [{"category_id": "7", "category_name": f"{provider.name} Movies"}], [], {"7": 3}, {}
        with mock.patch.object(disc, "_refresh_vod_categories", refresh), \
                mock.patch("routers.providers.fetch_provider_categories", fetch):
            return disc.discover_new_provider_content(self.db)

    def test_a_database_error_for_one_provider_leaves_the_next_one_and_the_notice(self):
        result = self._discover()
        self.assertEqual(["B Movies"], result["vod_new"])
        self.assertEqual(["B Movies"], [c.category_name for c in self.db.query(mdb.ProviderCategory).all()])
        notice = json.loads(mdb.get_setting(self.db, "new_content_notice", "") or "{}")
        self.assertEqual(["B Movies"], notice.get("vod"))

    def test_a_rollback_that_fails_does_not_escape_discovery(self):
        def bad_rollback():
            raise RuntimeError("connection invalidated")
        with mock.patch.object(self.db, "rollback", bad_rollback):
            try:
                self._discover()
            except RuntimeError as e:
                self.fail(f"the rollback's own error escaped: {e}")
            except Exception:
                pass    # the session stays broken: later statements may still fail, as before


class DiscoveryLiveGroups(unittest.TestCase):
    """The Live TV half: a group sync whose flush fails (two new categories
    with the same name, uq_live_group) leaves the session usable."""

    def test_the_session_is_usable_after_a_failed_group_sync(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        db = _session(self)
        db.add(mdb.Provider(name="Live", server_url="http://live.test", username="u", password="p",
                            active=True, live_tv_enabled=True, provider_type="xtream"))
        db.commit()

        class Client:
            def __init__(self, *a, **k):
                pass

            def get_live_categories(self):
                return [{"category_id": "1", "category_name": "SPORTS"},
                        {"category_id": "2", "category_name": "SPORTS"}]

            def get_live_streams(self):
                return []

            def close(self):
                pass
        import services.discovery as disc
        with mock.patch("services.xtream_client.XtreamClient", Client):
            result = disc.discover_new_provider_content(db)
        self.assertEqual([], result["live_new"])
        self.assertEqual(0, db.query(mdb.Setting).filter(mdb.Setting.key == "x").count())   # no PendingRollbackError


if __name__ == "__main__":
    unittest.main()
