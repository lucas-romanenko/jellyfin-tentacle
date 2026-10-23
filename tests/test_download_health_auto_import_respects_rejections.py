"""Auto-import must respect why Sonarr/Radarr declined a download.

A finished download the arr won't import is "import_blocked". With
"downloads_auto_import_enabled" on, the 5-minute sweep (run_download_health_check)
asks the arr's Manual Import lookup for the file and sends a ManualImport
command (attempt_manual_import). A manual import does not go through the arr's
import decisions: the file is imported, replacing the episode's/film's existing
file. The lookup returns why the arr refused it (`rejections`, e.g. "Not an
upgrade for existing episode file(s)"), but attempt_manual_import never reads
them, so a release the arr judged worse than the file the library has replaces
it. Nothing remembers that a download was already force-imported either: an item
still blocked after the import is force-imported again every 5 minutes.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
import os
import shutil
import tempfile
import json
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting
import services.download_health as dh

def temp_dir(cls):
    return tempfile.mkdtemp()

def get_setting(db, key, default):
    res = db.query(Setting).filter(Setting.key == key).first()
    return res.value if res else default

def set_setting(db, key, value):
    res = db.query(Setting).filter(Setting.key == key).first()
    if res:
        res.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()

def _blocked(reason, download_id="D1"):
    """A finished Sonarr download the arr declined to import."""
    return {"id": 1, "downloadId": download_id, "title": "Show.S01E03.720p.WEB", "size": 1, "sizeleft": 0,
            "status": "completed", "trackedDownloadState": "importBlocked",
            "trackedDownloadStatus": "warning", "protocol": "torrent", "seriesId": 5, "episodeId": 77,
            "statusMessages": [{"title": "Show.S01E03.720p.WEB", "messages": [reason]}]}


def _candidate(rejections):
    """What GET /api/v3/manualimport?downloadId= answers for it."""
    return {"path": "/downloads/Show.S01E03.720p.WEB.mkv", "name": "Show.S01E03.720p.WEB",
            "quality": {"quality": {"name": "WEBDL-720p"}}, "series": {"id": 5},
            "episodes": [{"id": 77}], "releaseType": "singleEpisode", "rejections": rejections}


class Sonarr:
    def __init__(self, record, candidate):
        self.record, self.candidate = record, candidate
        self.commands, self.deletes = [], []

    def get(self, url, key, path, **params):
        if path == "queue":
            return {"records": [self.record], "totalRecords": 1}
        if path == "manualimport":
            return [self.candidate]
        if path.startswith("command/"):
            return {"status": "completed"}
        return []

    def post(self, url, key, path, body):
        if path == "command":
            self.commands.append(body)
            return {"id": len(self.commands)}
        return {}

    def delete(self, url, key, path, **params):
        self.deletes.append((path, params))

    def imports(self):
        return [c for c in self.commands if c.get("name") == "ManualImport"]


class AutoImportRespectsRejections(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{os.path.join(self.test_dir, 't.db')}")
        self.addCleanup(engine.dispose)
        self.addCleanup(shutil.rmtree, self.test_dir)
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        self.db.add_all([Setting(key="sonarr_url", value="http://sonarr.example"),
                    Setting(key="sonarr_api_key", value="k"),
                    Setting(key="downloads_auto_import_enabled", value="true")])
        self.db.commit()

        # Mock the SessionLocal on dh
        self.original_session_local = dh.SessionLocal
        dh.SessionLocal = lambda: self.db
        self.addCleanup(setattr, dh, "SessionLocal", self.original_session_local)

    def _install(self, arr):
        for name in ("get", "post", "delete"):
            self.addCleanup(setattr, dh, f"_arr_{name}", getattr(dh, f"_arr_{name}"))
            setattr(dh, f"_arr_{name}", getattr(arr, name))

    def test_a_release_refused_as_not_an_upgrade_is_not_force_imported(self):
        for n, reason in enumerate(("Not an upgrade for existing episode file(s)", "Sample")):
            with self.subTest(reason=reason):
                arr = Sonarr(_blocked(reason, f"D{n}"), _candidate([{"reason": reason, "type": "permanent"}]))
                self._install(arr)
                dh.run_download_health_check()
                self.assertEqual([], arr.imports(),
                                 f"force-imported although Sonarr refused it: {reason!r}")

    def test_a_download_is_force_imported_once_not_every_sweep(self):
        # The import "completed" but the item is still blocked in the queue:
        # the next sweeps must not import the same download again and again.
        arr = Sonarr(_blocked("Found matching series via grab history, but release was matched to series by ID. "
                                 "Automatic import is not possible."), _candidate([]))
        self._install(arr)
        for _ in range(3):           # three 5-minute sweeps
            dh.run_download_health_check()
        self.assertLessEqual(len(arr.imports()), 1,
                             f"the same download was force-imported {len(arr.imports())} times")


if __name__ == "__main__":
    unittest.main()
