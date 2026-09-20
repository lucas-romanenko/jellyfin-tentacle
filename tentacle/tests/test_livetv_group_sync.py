"""Tests for the nightly Live TV group refresh (services.discovery →
routers.livetv._sync_groups_from_xtream).

Two regressions this pins:

  1. _sync_groups_from_xtream never clears the shared sync-status slot it
     writes to. The nightly discovery step calls it directly (not through
     _run_group_sync_background, which is what normally writes the terminal
     "complete" status), so after the first nightly run the provider is stuck
     at phase="running" for the lifetime of the process and both Live TV sync
     endpoints refuse to start.

  2. The per-group channel_count is taken from a single bulk get_live_streams()
     call whose failure is only logged. When that call fails every group's
     channel_count is overwritten with 0.

Requires: fastapi (routers.livetv imports it). Run from the tentacle/
directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannelGroup, Provider
import routers.livetv as livetv
import services.discovery as discovery


def _session():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


CATEGORIES = [
    {"category_id": "1", "category_name": "CA| SPORTS"},
    {"category_id": "2", "category_name": "CA| NEWS"},
]


class _Client:
    """Stand-in for XtreamClient. `streams` may be a list or an Exception."""

    def __init__(self, streams, **kwargs):
        self._streams = streams
        self.closed = False

    def get_live_categories(self):
        return CATEGORIES

    def get_live_streams(self, category_id=None):
        if isinstance(self._streams, Exception):
            raise self._streams
        return self._streams

    def close(self):
        self.closed = True


class _ClientFactory:
    def __init__(self, streams):
        self.streams = streams

    def __call__(self, **kwargs):
        return _Client(self.streams)


class LiveGroupSyncTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        self.provider = Provider(name="P", server_url="http://p.example",
                                 username="u", password="p",
                                 active=True, live_tv_enabled=True)
        self.db.add(self.provider)
        self.db.commit()
        self.pid = self.provider.id
        self.provider_data = {
            "id": self.pid,
            "name": "P",
            "server_url": "http://p.example",
            "username": "u",
            "password": "p",
            "user_agent": "TiviMate/4.7.0",
        }
        with livetv._sync_status_lock:
            livetv._sync_status.clear()
        self._orig_client = None

    def tearDown(self):
        with livetv._sync_status_lock:
            livetv._sync_status.clear()
        self.db.close()

    def _patch_client(self, streams):
        import services.xtream_client as xc
        self._orig_client = xc.XtreamClient
        xc.XtreamClient = _ClientFactory(streams)
        self.addCleanup(setattr, xc, "XtreamClient", self._orig_client)

    # ── 1. status slot is left at phase="running" ────────────────────────

    def test_group_sync_ends_in_a_terminal_status(self):
        """After a successful group sync the provider must not still look busy."""
        self._patch_client([{"stream_id": 1, "category_id": "1"}])
        livetv._sync_groups_from_xtream(self.provider_data, self.db)
        self.assertNotEqual(
            livetv._get_sync_status(self.pid).get("phase"), "running",
            "sync status left at phase='running' — every later sync is refused",
        )

    def test_sync_endpoints_still_work_after_a_nightly_refresh(self):
        """The nightly discovery refresh must not wedge the sync buttons."""
        self._patch_client([{"stream_id": 1, "category_id": "1"}])
        discovery._refresh_live_groups(self.db, self.provider)

        started = []
        orig_thread = livetv.threading.Thread

        class _Thread:
            def __init__(self, *a, **kw):
                started.append(kw.get("args"))

            def start(self):
                pass

        livetv.threading.Thread = _Thread
        self.addCleanup(setattr, livetv.threading, "Thread", orig_thread)

        result = livetv.sync_live_groups(self.pid, self.db)
        self.assertTrue(
            result.get("success"),
            f"Live TV group sync refused after the nightly refresh: {result}",
        )
        self.assertEqual(len(started), 1)

    # ── 2. channel_count wiped when the bulk fetch fails ─────────────────

    def test_bulk_stream_fetch_failure_keeps_group_counts(self):
        """A failed get_live_streams() must not reset every group to 0 channels."""
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="CA| SPORTS",
                                     category_id="1", enabled=True, channel_count=412))
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="CA| NEWS",
                                     category_id="2", enabled=False, channel_count=77))
        self.db.commit()

        self._patch_client(RuntimeError("read timeout after 600s"))
        livetv._sync_groups_from_xtream(self.provider_data, self.db)

        counts = {g.name: g.channel_count
                  for g in self.db.query(LiveChannelGroup).all()}
        self.assertEqual(
            counts, {"CA| SPORTS": 412, "CA| NEWS": 77},
            "group channel counts were zeroed by a failed bulk stream fetch",
        )

    def test_successful_fetch_still_updates_counts(self):
        """The guard must not freeze counts when the provider does answer."""
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="CA| SPORTS",
                                     category_id="1", enabled=True, channel_count=1))
        self.db.commit()
        self._patch_client([
            {"stream_id": 1, "category_id": "1"},
            {"stream_id": 2, "category_id": "1"},
            {"stream_id": 3, "category_id": "2"},
        ])
        livetv._sync_groups_from_xtream(self.provider_data, self.db)
        counts = {g.name: g.channel_count
                  for g in self.db.query(LiveChannelGroup).all()}
        self.assertEqual(counts["CA| SPORTS"], 2)
        self.assertEqual(counts["CA| NEWS"], 1)


if __name__ == "__main__":
    unittest.main()
