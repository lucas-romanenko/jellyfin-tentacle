"""A Radarr/Sonarr scan and the tag refresh agree on a download's tags.

The scans tagged a download with its raw source ("Netflix") and a recency
window of N days. The tag refresh (refresh_recently_added_tags) keeps
"Netflix Movies" / "Netflix TV" (plus "Netflix Recently Added ...") and a
window that reaches back to at least RECENTLY_ADDED_MIN titles. So every scan
(nightly, and each Radarr/Sonarr webhook) rewrote those NFOs, counted them as
written and asked Jellyfin for a library scan, and the next refresh wrote
them back; in between the titles were out of their "Netflix Movies" playlist.
"""
import re
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir


def _tags(nfo):
    return sorted(re.findall(r"<tag>([^<]*)</tag>", nfo.read_text()))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", str(self.tmp))):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def refresh(self):
        from services.tagger import refresh_recently_added_tags
        return refresh_recently_added_tags(self.db)


class RadarrScanAndRefreshAgree(_Base):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "movies" / "Heat (1995)"
        self.root.mkdir(parents=True)
        (self.root / "Heat (1995).mkv").write_bytes(b"x")
        self.movies = [{"tmdbId": 949, "title": "Heat", "year": 1995, "hasFile": True, "path": str(self.root),
                        "movieFile": {"path": str(self.root / "Heat (1995).mkv"),
                                      "dateAdded": "2020-01-01T00:00:00Z"}}]

    def scan(self):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(inner):
                return [dict(m) for m in self.movies]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            return radarr.scan_radarr_library(self.db)

    def _row(self):
        return self.db.query(mdb.Movie).filter_by(tmdb_id=949).one()

    def test_a_source_tagged_download_is_written_once(self):
        self.scan()
        row = self._row()
        row.source_tag = "Netflix"
        row.date_added = datetime(2020, 1, 1)
        self.db.commit()
        self.scan()
        self.refresh()
        nfo = self.root / "Heat (1995).nfo"
        settled = _tags(nfo)
        self.assertIn("Netflix Movies", settled)
        self.assertEqual(0, self.scan()["nfo_written"])
        self.assertEqual((0, 0), self.refresh())
        self.assertEqual(settled, _tags(nfo))
        self.assertIn("Netflix Movies", self._row().tags)

    def test_recent_by_the_refresh_window_stays_recent(self):
        # One title only: the refresh's window reaches back to it (RECENTLY_ADDED_MIN),
        # while N days alone would not.
        self.scan()
        row = self._row()
        row.date_added = datetime(2020, 1, 1)
        self.db.commit()
        self.scan()
        self.refresh()
        self.assertIn("Recently Added Movies", _tags(self.root / "Heat (1995).nfo"))
        self.assertEqual(0, self.scan()["nfo_written"])
        self.assertEqual((0, 0), self.refresh())


class SonarrScanAndRefreshAgree(_Base):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "tv" / "Dark (2017)"
        self.root.mkdir(parents=True)
        (self.root / "S01E01.mkv").write_bytes(b"x")
        self.shows = [{"tmdbId": 70523, "tvdbId": 334824, "title": "Dark", "year": 2017,
                       "path": str(self.root), "monitorNewItems": "none", "statistics": {"episodeFileCount": 1}}]

    def scan(self):
        import services.sonarr as sonarr
        shows = self.shows

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, raise_errors=False):
                return [dict(s) for s in shows]

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            return sonarr.scan_sonarr_library(self.db)

    def test_a_source_tagged_download_is_written_once(self):
        self.scan()
        row = self.db.query(mdb.Series).filter_by(tmdb_id=70523).one()
        row.source_tag = "Netflix"
        row.date_added = datetime(2020, 1, 1)
        self.db.commit()
        self.scan()
        self.refresh()
        nfo = self.root / "tvshow.nfo"
        settled = _tags(nfo)
        self.assertIn("Netflix TV", settled)
        self.assertEqual(0, self.scan()["nfo_written"])
        self.assertEqual((0, 0), self.refresh())
        self.assertEqual(settled, _tags(nfo))


if __name__ == "__main__":
    unittest.main()
