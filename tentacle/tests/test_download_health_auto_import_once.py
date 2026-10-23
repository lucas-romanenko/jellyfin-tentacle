"""Auto-import and the Health page Import button after #766.

The sweep never force-imports a file the arr refused for good (it dequeues it,
keeps the file and says why in Activity), leaves temporary refusals to the arr,
tries each download once and only calls it "force-imported" when the download
really left the blocked state. The Import button shows the refusal and imports
a refused file only when the admin confirmed (allow_rejected).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ActivityLog, Setting
import services.download_health as dh
from tmp_dirs import temp_dir


def _blocked(download_id="D1", reason="Not an upgrade for existing episode file(s)"):
    return {"id": 1, "downloadId": download_id, "title": "Show.S01E03.720p.WEB", "size": 1, "sizeleft": 0,
            "status": "completed", "trackedDownloadState": "importBlocked",
            "trackedDownloadStatus": "warning", "protocol": "torrent", "seriesId": 5, "episodeId": 77,
            "statusMessages": [{"title": "Show.S01E03.720p.WEB", "messages": [reason]}]}


def _candidate(rejections=()):
    return {"path": "/downloads/Show.S01E03.720p.WEB.mkv", "name": "Show.S01E03.720p.WEB",
            "quality": {"quality": {"name": "WEBDL-720p"}}, "series": {"id": 5},
            "episodes": [{"id": 77}], "releaseType": "singleEpisode", "rejections": list(rejections)}


class Sonarr:
    """A fake Sonarr: the queue holds one blocked download; a ManualImport
    empties it unless `import_takes` is False."""

    def __init__(self, record, candidate, import_takes=True):
        self.record, self.candidate, self.import_takes = record, candidate, import_takes
        self.commands, self.deletes = [], []

    def get(self, url, key, path, **params):
        if path == "queue":
            return {"records": [self.record] if self.record else [], "totalRecords": 1 if self.record else 0}
        if path == "manualimport":
            return [self.candidate]
        if path.startswith("command/"):
            return {"status": "completed"}
        return []

    def post(self, url, key, path, body):
        if path == "command":
            self.commands.append(body)
            if body.get("name") == "ManualImport" and self.import_takes:
                self.record = None
            return {"id": len(self.commands)}
        return {}

    def delete(self, url, key, path, **params):
        self.deletes.append((path, params))
        self.record = None

    def imports(self):
        return [c for c in self.commands if c.get("name") == "ManualImport"]


class Base(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        db.add_all([Setting(key="sonarr_url", value="http://sonarr.example"),
                    Setting(key="sonarr_api_key", value="k"),
                    Setting(key="downloads_auto_import_enabled", value="true")])
        db.commit()
        db.close()
        self.addCleanup(setattr, dh, "SessionLocal", dh.SessionLocal)
        dh.SessionLocal = self.Session
        sleep = mock.patch.object(dh.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def _install(self, arr):
        for name in ("get", "post", "delete"):
            self.addCleanup(setattr, dh, f"_arr_{name}", getattr(dh, f"_arr_{name}"))
            setattr(dh, f"_arr_{name}", getattr(arr, name))
        return arr

    def activity(self):
        db = self.Session()
        try:
            return [a.message for a in db.query(ActivityLog).order_by(ActivityLog.id)]
        finally:
            db.close()

    def tried(self):
        db = self.Session()
        try:
            return dh.get_import_tried(db)
        finally:
            db.close()


class Sweep(Base):
    def test_refused_file_is_dequeued_with_its_file_kept_and_the_reason_shown(self):
        arr = self._install(Sonarr(_blocked(), _candidate(
            [{"reason": "Not an upgrade for existing episode file(s)", "type": "permanent"}])))
        dh.run_download_health_check()
        self.assertEqual([], arr.imports())
        self.assertEqual("false", arr.deletes[0][1]["removeFromClient"])
        self.assertEqual(1, len(self.activity()))
        self.assertIn("left for review", self.activity()[0])
        self.assertIn("Sonarr refused it: Not an upgrade", self.activity()[0])
        self.assertEqual({}, self.tried())

    def test_temporary_refusal_is_left_to_the_arr(self):
        arr = self._install(Sonarr(_blocked(), _candidate([{"reason": "Locked", "type": "temporary"}])))
        dh.run_download_health_check()
        self.assertEqual([], arr.imports())
        self.assertEqual([], arr.deletes)
        self.assertEqual([], self.activity())

    def test_import_that_clears_the_queue_is_force_imported_once(self):
        arr = self._install(Sonarr(_blocked(reason="matched to series by ID. Automatic import is not possible."),
                                   _candidate()))
        dh.run_download_health_check()
        self.assertEqual(1, len(arr.imports()))
        self.assertEqual(["Blocked import force-imported: Show.S01E03.720p.WEB"], self.activity())
        self.assertIn("D1", self.tried())

    def test_completed_command_that_leaves_it_blocked_is_not_called_imported(self):
        arr = self._install(Sonarr(_blocked(reason="matched to series by ID."), _candidate(), import_takes=False))
        for _ in range(3):
            dh.run_download_health_check()
        self.assertEqual(1, len(arr.imports()))
        self.assertEqual([], arr.deletes, "left on the Health page for the admin")
        msgs = self.activity()
        self.assertEqual(1, len(msgs), msgs)
        self.assertIn("not imported, left for you", msgs[0])
        self.assertNotIn("force-imported", msgs[0])

    def test_download_back_in_blocked_after_an_import_is_reported_once(self):
        arr = self._install(Sonarr(_blocked(reason="matched to series by ID."), _candidate()))
        dh.run_download_health_check()
        arr.record = _blocked(reason="matched to series by ID.")
        for _ in range(2):
            dh.run_download_health_check()
        self.assertEqual(1, len(arr.imports()))
        self.assertEqual(2, len(self.activity()))
        self.assertIn("already tried it once", self.activity()[1])

    def test_a_try_older_than_a_week_is_forgotten(self):
        db = self.Session()
        old = (datetime.utcnow() - timedelta(days=dh.IMPORT_TRIED_DAYS + 1)).isoformat()
        mdb.set_setting(db, dh.IMPORT_TRIED_KEY, json.dumps({"D1": {"at": old, "told": True}}))
        db.close()
        arr = self._install(Sonarr(_blocked(reason="matched to series by ID."), _candidate()))
        dh.run_download_health_check()
        self.assertEqual(1, len(arr.imports()))


class ImportButton(Base):
    def _press(self, **extra):
        from routers.health import DownloadImportRequest, manual_import_download
        db = self.Session()
        try:
            return manual_import_download(DownloadImportRequest(source="sonarr", download_id="D1", **extra), db)
        finally:
            db.close()

    def test_refused_file_needs_the_admins_confirmation(self):
        arr = self._install(Sonarr(_blocked(), _candidate(
            [{"reason": "Not an upgrade for existing episode file(s)", "type": "permanent"}])))
        r = self._press()
        self.assertFalse(r["ok"])
        self.assertEqual(["Not an upgrade for existing episode file(s)"], r["refused"])
        self.assertEqual([], arr.imports())

        r = self._press(allow_rejected=True)
        self.assertTrue(r["ok"])
        self.assertEqual(1, len(arr.imports()))

    def test_ambiguous_lookup_is_an_answer_not_an_error(self):
        arr = self._install(Sonarr(_blocked(), _candidate()))
        arr.get = lambda url, key, path, **p: [] if path == "manualimport" else Sonarr.get(arr, url, key, path, **p)
        dh._arr_get = arr.get
        r = self._press()
        self.assertFalse(r["ok"])
        self.assertIn("needs manual review", r["reason"])


if __name__ == "__main__":
    unittest.main()
