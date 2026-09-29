"""Music pages waiting on MusicBrainz must not stall the rest of Tentacle (#243).

MusicBrainz takes one request a second for the whole server. A burst of album
and artist pages used to take every thread of the shared pool and, since each
held its database session while it waited, every connection of the pool: other
routes stalled for tens of seconds and pages failed with QueuePool timeouts.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import _Base  # noqa: E402


class TestPageBurst(_Base):
    def setUp(self):
        super().setUp()
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        import routers.music as music
        import services.musicbrainz as mbmod
        from routers.auth import get_user_from_request

        # A small connection pool, like the real one relative to a burst of pages.
        engine = create_engine(self.db.get_bind().url, connect_args={"check_same_thread": False},
                               pool_size=3, max_overflow=0, pool_timeout=3)
        Small = sessionmaker(bind=engine)
        self.addCleanup(engine.dispose)

        def get_db():
            db = Small()
            try:
                yield db
            finally:
                db.close()

        def fake_get(url, **kw):   # a slow but healthy MusicBrainz
            time.sleep(0.1)
            if "/release-group" in url:
                return mock.Mock(status_code=200, json=lambda: {"release-groups": [], "release-group-count": 0})
            return mock.Mock(status_code=200, json=lambda: {"name": "Someone", "relations": []})
        for p in (mock.patch.object(mbmod.requests, "get", side_effect=fake_get),
                  mock.patch.object(mbmod, "MIN_INTERVAL", 0.0),
                  mock.patch.object(music, "PAGE_QUEUE_WAIT", 1.5, create=True)):
            p.start()
            self.addCleanup(p.stop)
        mbmod._interactive.update(waiting=0, last=0.0)

        app = FastAPI()
        app.include_router(music.webhook_router)

        @app.get("/ping")
        def ping():
            return {"ok": True}

        @app.get("/ping-db")
        def ping_db(db=Depends(get_db)):
            return {"n": db.execute(text("select count(*) from settings")).scalar()}

        app.dependency_overrides[mdb.get_db] = get_db
        app.dependency_overrides[get_user_from_request] = lambda: self.user
        self.client = TestClient(app)
        self.client.__enter__()   # one event loop for every request, as under uvicorn
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_a_burst_of_pages_leaves_other_routes_and_the_database_alone(self):
        ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(45)]   # distinct: no cache hits
        with ThreadPoolExecutor(max_workers=45) as pool:
            pages = [pool.submit(self.client.get, f"/api/music/artist/{i}") for i in ids]
            time.sleep(0.5)   # the burst is waiting on MusicBrainz now
            t0 = time.monotonic()
            self.assertEqual(self.client.get("/ping").status_code, 200)
            ping = time.monotonic() - t0
            t0 = time.monotonic()
            self.assertEqual(self.client.get("/ping-db").status_code, 200)
            ping_db = time.monotonic() - t0
            codes = [f.result().status_code for f in pages]
        self.assertLess(ping, 1.0)
        self.assertLess(ping_db, 1.0)
        # Every page is served or told MusicBrainz is busy: none fails on the pool.
        self.assertEqual(set(codes) - {200, 503}, set(), codes)
        self.assertIn(200, codes)
        busy = self.client.get(f"/api/music/artist/{ids[0]}")   # served from the cache now
        self.assertEqual(busy.status_code, 200)


class TestInteractiveWait(unittest.TestCase):
    def test_a_page_gives_up_behind_a_hung_lookup_but_the_worker_waits(self):
        import services.musicbrainz as mbmod
        mbmod._interactive.update(waiting=0, last=0.0)
        with mock.patch.object(mbmod, "INTERACTIVE_WAIT", 0.3, create=True), \
                mock.patch.object(mbmod.requests, "get",
                                  return_value=mock.Mock(status_code=200, json=lambda: {"ok": True})):
            mbmod._gate.acquire()   # a lookup that hangs
            outcome = {}

            def page():
                try:
                    mbmod.get("/artist/x", contact="me@example.com")
                    outcome["page"] = "answered"
                except mbmod.MusicBrainzError as e:
                    outcome["page"] = e.status
            t = threading.Thread(target=page)
            t.start()
            t.join(2)
            try:
                self.assertFalse(t.is_alive(), "the page waited for good")
                self.assertEqual(outcome["page"], 503)
                self.assertEqual(mbmod._interactive["waiting"], 0)
            finally:
                mbmod._gate.release()
                t.join(2)


if __name__ == "__main__":
    unittest.main()
