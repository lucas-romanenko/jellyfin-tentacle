"""#266: a Radarr/Sonarr scan changes only Tentacle's tags in an existing NFO.

Every scan rebuilt the NFO of every downloaded title from Tentacle's template,
with <dateadded> = now. Jellyfin reads <dateadded> as the item's DateCreated,
so each scan (every webhook runs one) moved every old download to the top of
"Latest" / "Date added", and whatever else the file held (<uniqueid>, cast,
the original date) was lost. A missing NFO is still written from the row.
"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

OWNER_MOVIE_NFO = ('<?xml version="1.0"?>\n<movie>\n  <title>Heat</title>\n'
                   '  <dateadded>2020-01-01 00:00:00</dateadded>\n'
                   '  <uniqueid type="imdb">tt0113277</uniqueid>\n</movie>\n')
OWNER_SERIES_NFO = ('<?xml version="1.0"?>\n<tvshow>\n  <title>Breaking Bad</title>\n'
                    '  <dateadded>2019-05-05 00:00:00</dateadded>\n'
                    '  <uniqueid type="imdb">tt0903747</uniqueid>\n</tvshow>\n')


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
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

    @staticmethod
    def _dates(path):
        return re.findall(r"<dateadded>([^<]*)", path.read_text())


class RadarrScanKeepsNfo(_Base):
    def _scan(self, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return [dict(m) for m in movies]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            return radarr.scan_radarr_library(self.db)

    def test_existing_nfo_keeps_dateadded_and_owner_fields(self):
        root = self.tmp / "movies" / "Heat (1995)"
        root.mkdir(parents=True)
        (root / "Heat (1995).mkv").write_bytes(b"x")
        nfo = root / "Heat (1995).nfo"
        nfo.write_text(OWNER_MOVIE_NFO)
        movies = [{"tmdbId": 949, "title": "Heat", "year": 1995, "hasFile": True, "path": str(root),
                   "movieFile": {"path": str(root / "Heat (1995).mkv"), "dateAdded": "2020-01-01T00:00:00Z"}}]
        self._scan(movies)
        self._scan(movies)
        text = nfo.read_text()
        self.assertEqual(["2020-01-01 00:00:00"], self._dates(nfo))
        self.assertIn("tt0113277", text)
        self.assertIn("<tag>Downloaded Movies</tag>", text)  # Tentacle's tags still land
        row = self.db.query(mdb.Movie).filter_by(tmdb_id=949).one()
        self.assertEqual(str(nfo), row.nfo_path)

    def test_second_scan_writes_nothing(self):
        root = self.tmp / "movies" / "Heat (1995)"
        root.mkdir(parents=True)
        (root / "Heat (1995).mkv").write_bytes(b"x")
        (root / "Heat (1995).nfo").write_text(OWNER_MOVIE_NFO)
        movies = [{"tmdbId": 949, "title": "Heat", "year": 1995, "hasFile": True, "path": str(root),
                   "movieFile": {"path": str(root / "Heat (1995).mkv")}}]
        self._scan(movies)
        self.assertEqual(0, self._scan(movies)["nfo_written"])

    def test_missing_nfo_is_still_written(self):
        root = self.tmp / "movies" / "Ronin (1998)"
        root.mkdir(parents=True)
        (root / "Ronin (1998).mkv").write_bytes(b"x")
        movies = [{"tmdbId": 8195, "title": "Ronin", "year": 1998, "hasFile": True, "path": str(root),
                   "movieFile": {"path": str(root / "Ronin (1998).mkv")}}]
        self.assertEqual(1, self._scan(movies)["nfo_written"])
        self.assertTrue((root / "Ronin (1998).nfo").exists())


class SonarrScanKeepsNfo(_Base):
    def _scan(self, shows):
        import services.sonarr as sonarr

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

    def test_existing_tvshow_nfo_keeps_dateadded_and_owner_fields(self):
        root = self.tmp / "tv" / "Breaking Bad (2008)"
        root.mkdir(parents=True)
        nfo = root / "tvshow.nfo"
        nfo.write_text(OWNER_SERIES_NFO)
        shows = [{"tmdbId": 1396, "tvdbId": 81189, "title": "Breaking Bad", "year": 2008,
                  "path": str(root), "monitorNewItems": "none", "statistics": {"episodeFileCount": 3}}]
        self._scan(shows)
        self._scan(shows)
        self.assertEqual(["2019-05-05 00:00:00"], self._dates(nfo))
        self.assertIn("tt0903747", nfo.read_text())
        self.assertIn("<tag>", nfo.read_text())


if __name__ == "__main__":
    unittest.main()
