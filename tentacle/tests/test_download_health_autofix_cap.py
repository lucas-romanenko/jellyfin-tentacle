"""Tests for the 5-minute auto-fix sweep (services.download_health).

classify_queue_item() reports "stuck" the moment the arr publishes
status="warning" with any message — no elapsed-time threshold at all, because
a hard fault is assumed to be a per-download diagnosis. But the messages a
download client produces when it goes away ("qBittorrent is unavailable",
"Unable to communicate with the download client") are published against EVERY
item in the queue at once.

run_download_health_check() then walks the whole queue and calls
resolve_stuck_download() on each item, which DELETEs it with blocklist=true
and grabs a replacement release. There is no cap and no outage check, so one
download-client restart blocklists the entire queue in a single sweep — and a
blocklisted release is never grabbed again.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting
import services.download_health as dh

CLIENT_DOWN = "Unable to communicate with qBittorrent. Connection refused"


def _session_factory():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _item(i, message=CLIENT_DOWN):
    return {
        "id": i,
        "downloadId": f"D{i}",
        "title": f"Release.{i}.1080p",
        "size": 5_000_000_000,
        "sizeleft": 2_000_000_000,
        "status": "warning",
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "t", "messages": [message]}],
        "protocol": "torrent",
        "movieId": 10 + i,
    }


class _Arr:
    """Records every write the sweep makes against the arr."""

    def __init__(self, records):
        self.records = records
        self.deletes = []
        self.grabs = []

    def get(self, url, key, path, **params):
        if path == "queue":
            return {"records": self.records, "totalRecords": len(self.records)}
        if path == "release":
            return [{"guid": "g1", "indexerId": 1, "protocol": "usenet",
                     "title": "Replacement", "rejected": False}]
        return []

    def post(self, url, key, path, body):
        if path == "release":
            self.grabs.append(body)
        return {}

    def delete(self, url, key, path, **params):
        self.deletes.append((path, params))


class AutoFixSweepTests(unittest.TestCase):
    def setUp(self):
        Session = _session_factory()
        self.db = Session()
        for k, v in (("radarr_url", "http://radarr.example"),
                     ("radarr_api_key", "k"),
                     ("downloads_auto_fix_enabled", "true")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        self.db.close()

        self._orig_session = dh.SessionLocal
        dh.SessionLocal = Session
        self.addCleanup(setattr, dh, "SessionLocal", self._orig_session)
        self.session_factory = Session

    def _install(self, arr):
        for name, fn in (("_arr_get", arr.get), ("_arr_post", arr.post),
                         ("_arr_delete", arr.delete)):
            orig = getattr(dh, name)
            setattr(dh, name, fn)
            self.addCleanup(setattr, dh, name, orig)

    def test_client_outage_does_not_blocklist_the_whole_queue(self):
        arr = _Arr([_item(i) for i in range(1, 41)])
        self._install(arr)
        dh.run_download_health_check()
        blocklisted = [p for p, params in arr.deletes
                       if params.get("blocklist") == "true"]
        self.assertLess(
            len(blocklisted), 40,
            f"a download-client outage blocklisted all {len(blocklisted)} "
            "queue items in one 5-minute sweep")

    def test_auto_fix_is_capped_per_sweep(self):
        """Even a genuine multi-failure run must not act without a bound.

        10 stalled items in a 40-item queue is below the outage threshold, so
        this exercises the per-sweep cap rather than the outage guard.
        """
        arr = _Arr([_item(i, message=f"The download is stalled ({i})")
                    for i in range(1, 11)]
                   + [{"id": i, "downloadId": f"D{i}", "title": f"Healthy{i}",
                       "size": 100, "sizeleft": 50 - (i % 10), "status": "downloading",
                       "trackedDownloadState": "downloading",
                       "trackedDownloadStatus": "ok", "statusMessages": [],
                       "protocol": "torrent", "movieId": 100 + i}
                      for i in range(11, 41)])
        self._install(arr)
        dh.run_download_health_check()
        blocklisted = [p for p, params in arr.deletes
                       if params.get("blocklist") == "true"]
        self.assertLessEqual(
            len(blocklisted), dh.AUTO_FIX_MAX_PER_RUN,
            f"{len(blocklisted)} items cancelled + blocklisted in one sweep")
        self.assertGreater(len(blocklisted), 0,
                           "the cap must not disable auto-fix entirely")

    # ── controls ─────────────────────────────────────────────────────────

    def test_a_single_stuck_download_is_still_fixed(self):
        arr = _Arr([_item(1, message="The download is stalled with no connections"),
                    {"id": 2, "downloadId": "D2", "title": "Healthy",
                     "size": 100, "sizeleft": 50, "status": "downloading",
                     "trackedDownloadState": "downloading",
                     "trackedDownloadStatus": "ok", "statusMessages": [],
                     "protocol": "torrent", "movieId": 12},
                    {"id": 3, "downloadId": "D3", "title": "Healthy2",
                     "size": 100, "sizeleft": 20, "status": "downloading",
                     "trackedDownloadState": "downloading",
                     "trackedDownloadStatus": "ok", "statusMessages": [],
                     "protocol": "torrent", "movieId": 13},
                    {"id": 4, "downloadId": "D4", "title": "Healthy3",
                     "size": 100, "sizeleft": 10, "status": "downloading",
                     "trackedDownloadState": "downloading",
                     "trackedDownloadStatus": "ok", "statusMessages": [],
                     "protocol": "torrent", "movieId": 14}])
        self._install(arr)
        dh.run_download_health_check()
        blocklisted = [p for p, params in arr.deletes
                       if params.get("blocklist") == "true"]
        self.assertEqual(blocklisted, ["queue/1"],
                         "the one genuinely stuck download was not fixed")
        self.assertEqual(len(arr.grabs), 1, "no replacement release was grabbed")

    def test_classification_itself_is_unchanged(self):
        self.assertEqual(dh.classify_queue_item(_item(1), {})["status"], "stuck")
        healthy = {"id": 9, "downloadId": "D9", "size": 100, "sizeleft": 50,
                   "status": "downloading", "trackedDownloadState": "downloading",
                   "trackedDownloadStatus": "ok", "statusMessages": []}
        self.assertEqual(dh.classify_queue_item(healthy, {})["status"], "downloading")


if __name__ == "__main__":
    unittest.main()
