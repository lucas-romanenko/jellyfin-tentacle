"""A sync that is still updating Jellyfin must not be reported as done (A13).

sync_provider marks the SyncRun "completed" when the VOD part ends, but the
sync thread then runs the Jellyfin pipeline (library scan wait up to 120 s,
tag push, playlists) and only clears _running_syncs afterwards. Every status
said "completed" while a new sync was refused with "A sync is already
running". The guard is right; the status now says "finishing".

Run from tentacle/:  python -m unittest discover -s tests -p "test_sync_finishing_status.py"
"""
import tempfile
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Provider, SyncRun


class TestFinishing(unittest.TestCase):
    def setUp(self):
        import routers.sync as rs
        self.rs = rs
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.p = Provider(name="P", server_url="http://p", username="u", password="x", active=True)
        self.db.add(self.p)
        self.db.commit()
        self.run = SyncRun(provider_id=self.p.id, status="completed", sync_type="full",
                           started_at=datetime.utcnow(), completed_at=datetime.utcnow())
        self.db.add(self.run)
        self.db.commit()
        rs._running_syncs[self.p.id] = True          # the Jellyfin pipeline is still going

    def tearDown(self):
        self.rs._running_syncs.pop(self.p.id, None)
        self.db.close()

    def test_a_new_sync_is_refused_with_the_real_reason(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self.rs.trigger_sync(self.rs.SyncRequest(provider_id=self.p.id), db=self.db)
        self.assertIn("still updating Jellyfin", cm.exception.detail)

    def test_history_and_status_say_finishing(self):
        hist = self.rs.get_sync_history(provider_id=self.p.id, limit=10, offset=0, db=self.db)
        self.assertEqual(hist["runs"][0]["status"], "finishing")
        st = self.rs.get_sync_status(db=self.db)
        self.assertEqual(st["last_status"], "finishing")
        self.assertTrue(st["recent"][0]["finishing"])

    def test_once_done_it_is_completed(self):
        self.rs._running_syncs.pop(self.p.id, None)
        hist = self.rs.get_sync_history(provider_id=self.p.id, limit=10, offset=0, db=self.db)
        self.assertEqual(hist["runs"][0]["status"], "completed")
        self.assertEqual(self.rs.get_sync_status(db=self.db)["last_status"], "completed")


if __name__ == "__main__":
    unittest.main()
