"""#284: the Sonarr webhook's follow-up adds a missing list tag without crashing.

_after_scan re-imported tentacle_owned_tags inside the function, which made
the name local to all of it, so the earlier list-tag block raised
UnboundLocalError and the rest of the follow-up (NFO, Jellyfin push,
playlists, "ready to watch") was skipped.
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.sonarr as sonarr
from tmp_dirs import temp_dir


class AfterScanListTag(unittest.TestCase):
    def setUp(self):
        d = temp_dir(self)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        engine = create_engine(f"sqlite:///{d}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.nfo = Path(d) / "tvshow.nfo"
        self.nfo.write_text("<tvshow><title>Show</title></tvshow>")
        self.db.add(mdb.Series(tmdb_id=4242, title="Show", source="sonarr", nfo_path=str(self.nfo), tags=[]))
        sub = mdb.ListSubscription(name="My list", type="trakt", url="x", tag="My List Tag", active=True)
        self.db.add(sub)
        self.db.flush()
        self.db.add(mdb.ListItem(list_id=sub.id, tmdb_id=4242, media_type="series"))
        self.db.commit()

    def test_download_event_applies_list_tag(self):
        with self.assertLogs("routers.sonarr", level="INFO") as logs, \
             mock.patch("services.logstream.emit_library_event"):
            sonarr._after_scan(self.db, 4242, "Show", "Download",
                               {"seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"})
        self.assertEqual([], [l for l in logs.output if "failed" in l.lower()])
        self.db.expire_all()
        s = self.db.query(mdb.Series).filter_by(tmdb_id=4242).one()
        self.assertIn("My List Tag", s.tags or [])
        self.assertIn("My List Tag", self.nfo.read_text())


if __name__ == "__main__":
    unittest.main()
