"""Deleting a provider while it syncs left that category's .strm/.nfo files on
disk with no row and no provider, for good (#450).

A sync writes each new title's files before its row and commits the rows once
per category, so a delete under it missed them; the sync's commit then failed
on the deleted category. The delete now takes the provider's sync slot: it is
refused while a sync holds it, and a "Sync now" is refused while it runs.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p "test_delete_provider_mid_sync.py"
"""
import logging
from datetime import datetime
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import routers.providers as providers  # noqa: E402
import routers.sync as sync_router  # noqa: E402
import services.sync as sync  # noqa: E402
from models.database import Movie, Provider, SyncRun  # noqa: E402
from nightly_harness import NightlyHarness  # noqa: E402


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class DeleteProviderMidSync(NightlyHarness):
    def setUp(self):
        super().setUp()
        # The app's sessions: WAL, busy_timeout, autoflush off (models.database)
        url = self.db.bind.url
        self.db.close()
        self.db.bind.dispose()
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _rec):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")

        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.db = self.Session()                 # the sync's session
        self.provider = self.db.query(Provider).one()
        self.pid = self.provider.id
        sync_router._running_syncs.clear()
        self.addCleanup(sync_router._running_syncs.clear)

    def delete_provider(self):
        s = self.Session()                       # the DELETE request's own session
        try:
            with mock.patch("services.smartlists.sync_smartlists"), \
                    mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                    mock.patch("services.smartlists.write_home_config"):
                return providers.delete_provider(self.pid, db=s)
        except HTTPException as e:
            return {"refused": e.status_code, "detail": e.detail}
        finally:
            s.close()

    def files_left(self):
        return sorted(p.name for p in self.vod.rglob("*") if p.suffix in (".strm", ".nfo"))

    def test_delete_during_a_sync_is_refused_and_leaves_nothing_after_it(self):
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat", "Ronin", "Collateral"], first_tmdb=1000)
        self.catalogue_movies("2", ["Alien"], first_tmdb=2000)
        answer = {}

        def progress(phase, category, stats, item_title=None, item_pos=None, item_total=None):
            # Delete pressed while the dashboard shows "CAT 1 — 2/3: Ronin"
            if category == "CAT 1" and item_pos == 2 and not answer:
                answer.update(self.delete_provider())

        # "Sync now" and the nightly hold the provider's slot while they sync
        sync_router._running_syncs[self.pid] = True
        try:
            run = sync.sync_provider(self.provider, "full", self.db, progress_callback=progress)
        finally:
            sync_router._running_syncs.pop(self.pid, None)
        self.assertEqual(409, answer.get("refused"), answer)
        self.assertIn("cancel it", answer["detail"])
        self.assertEqual("completed", run.status, run.error_message)
        self.assertEqual(8, len(self.files_left()))  # the sync was not disturbed

        done = self.delete_provider()            # once the sync is over
        self.assertEqual(4, done.get("deleted_movies"), done)
        self.assertEqual([], self.files_left())
        s = self.Session()
        self.assertEqual((0, 0), (s.query(Provider).count(), s.query(Movie).count()))
        s.close()
        self.assertNotIn(self.pid, sync_router._running_syncs)

    def test_sync_now_during_a_delete_is_refused(self):
        self.add_category("1")
        self.catalogue_movies("1", ["Heat", "Ronin"], first_tmdb=1000)
        sync.sync_provider(self.provider, "full", self.db)
        self.db.close()
        tried = []
        real = providers.delete_movie_files

        def delete_movie_files(path):
            if not tried:                        # "Sync now" pressed while the delete removes files
                s = self.Session()
                try:
                    with mock.patch.object(sync_router.threading, "Thread") as thread:
                        sync_router.trigger_sync(sync_router.SyncRequest(provider_id=self.pid), db=s)
                    tried.append(("started", thread.call_count))
                except HTTPException as e:
                    tried.append((e.status_code, e.detail))
                finally:
                    s.close()
            return real(path)

        with mock.patch.object(providers, "delete_movie_files", delete_movie_files):
            done = self.delete_provider()
        self.assertEqual(2, done.get("deleted_movies"), done)
        self.assertEqual(400, tried[0][0], tried)
        self.assertEqual([], self.files_left())
        # The slot is free again once the delete is over
        self.assertNotIn(self.pid, sync_router._running_syncs)

    def test_a_running_sync_run_in_the_database_also_refuses(self):
        self.db.add(SyncRun(provider_id=self.pid, sync_type="full", status="running",
                            started_at=datetime.utcnow()))
        self.db.commit()
        self.assertEqual(409, self.delete_provider().get("refused"))
        self.assertEqual(1, self.db.query(Provider).count())
        self.assertNotIn(self.pid, sync_router._running_syncs)


if __name__ == "__main__":
    import unittest
    unittest.main()
