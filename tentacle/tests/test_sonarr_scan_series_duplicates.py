"""The Sonarr scan must notice a show that exists both as provider VOD and as a download.

Run from the tentacle/ directory:  python -m unittest discover -s tests

scan_sonarr_library() records a Duplicate for a VOD-only Series row that Sonarr
also has -- unless the row already carried a sonarr_path, which marks an
intentional "Download More Episodes" add. But it copied Sonarr's path onto the
row a few lines BEFORE testing `not existing.sonarr_path`, so the test could
only ever pass when Sonarr reported an empty path. Found live: with realistic
data, in either order, no series duplicate was ever recorded, while the Radarr
scan (which has no such condition) flags every movie overlap.
"""
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SHOW = {"tmdbId": 1403, "tvdbId": 9, "title": "Show", "path": "/tv/Show (2013)",
        "monitorNewItems": "none", "statistics": {"episodeFileCount": 3}}


class _Sonarr:
    def __init__(self, *a, **k):
        pass

    def get_all_series(self):
        return [dict(SHOW)]


class SeriesDuplicateDetection(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        self.mdb = mdb
        self.tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", self.tmp)):
            mdb.set_setting(self.db, k, v)
        self.db.commit()

    def _vod_row(self, **kw):
        row = self.mdb.Series(tmdb_id=1403, title="Show", year="2013", source="provider_1",
                              strm_path="/media/vod/shows/Show (2013)", **kw)
        self.db.add(row)
        self.db.commit()
        return row

    def _scan(self):
        import services.sonarr as sonarr
        with mock.patch.object(sonarr, "SonarrService", _Sonarr):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.commit()
        return out

    def _dups(self):
        return self.db.query(self.mdb.Duplicate).filter_by(media_type="series").all()

    def test_a_vod_series_that_sonarr_also_has_is_recorded(self):
        self._vod_row()
        self._scan()
        dups = self._dups()
        self.assertEqual(1, len(dups), "the overlap went unnoticed")
        self.assertEqual({"sonarr", "provider_1"}, {s["source"] for s in dups[0].sources})
        self.assertEqual("/tv/Show (2013)",
                         next(s["path"] for s in dups[0].sources if s["source"] == "sonarr"))

    def test_the_path_is_still_recorded_on_the_row(self):
        self._vod_row()
        self._scan()
        self.assertEqual("/tv/Show (2013)", self.db.query(self.mdb.Series).one().sonarr_path)

    def test_an_intentional_add_is_not_a_duplicate(self):
        """'Download More Episodes' sets sonarr_path when it adds the show."""
        self._vod_row(sonarr_path="/tv/Show (2013)")
        self._scan()
        self.assertEqual([], self._dups())

    def test_an_intentional_add_whose_folder_sonarr_moved_is_still_not_a_duplicate(self):
        self._vod_row(sonarr_path="/old/Show (2013)")
        self._scan()
        self.assertEqual([], self._dups())

    def test_a_second_scan_does_not_record_it_twice(self):
        self._vod_row()
        self._scan()
        self._scan()
        self.assertEqual(1, len(self._dups()))


if __name__ == "__main__":
    unittest.main()
