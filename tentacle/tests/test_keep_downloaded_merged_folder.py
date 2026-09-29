"""Duplicate "Keep Downloaded" (keep_radarr) goes through
services/duplicates.delete_vod_files, which never got the merged-folder guard
media_files.delete_movie_files has (#28): it deletes "<stem>.nfo" even when a
downloaded "<stem>.mkv" sits beside it -- Radarr's NFO for the file the user
chose to KEEP.  The sync's own enforcement of a keep_radarr resolution
(check_and_record_duplicate) calls the same helper, and for a SERIES (strm_path
is the show folder) it deletes nothing at all while converting the row, so the
.strm episodes are orphaned next to Sonarr's.  Self-contained.  755ea67.

Run from tentacle/:  python -m unittest discover -s tests -p "test_keep_downloaded_merged_folder.py"
"""
import logging, shutil, tempfile, unittest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import models.database as mdb
from models.database import Movie, Series, Duplicate, Provider
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db"); mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)(); self.addCleanup(self.db.close)
        self.p = Provider(name="P", server_url="http://p", username="u", password="p"); self.db.add(self.p); self.db.commit()


class KeepDownloadedMergedMovieFolder(Base):
    def test_keep_downloaded_keeps_the_downloads_nfo(self):
        d = self.root / "movies" / "Heat (1995)"; d.mkdir(parents=True)
        strm, mkv, nfo = d / "Heat (1995).strm", d / "Heat (1995).mkv", d / "Heat (1995).nfo"
        strm.write_text("http://p/movie/u/p/1.mp4"); mkv.write_bytes(b"\0" * 64)
        nfo.write_text("<movie><title>Heat</title><!-- Radarr's metadata for the .mkv --></movie>")
        self.db.add(Movie(tmdb_id=949, title="Heat", year="1995", source=f"provider_{self.p.id}", provider_id=self.p.id,
                          strm_path=str(strm), nfo_path=str(nfo), radarr_path=str(d)))
        dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(d)}, {"source": f"provider_{self.p.id}", "path": str(strm)}])
        self.db.add(dup); self.db.commit()
        from routers.duplicates import _apply_resolution
        _apply_resolution(dup, "keep_radarr", self.db)
        self.assertFalse(strm.exists())
        self.assertTrue(mkv.exists())
        self.assertTrue(nfo.exists(), "Keep Downloaded deleted the NFO of the downloaded copy it was told to keep")


class SyncEnforcementOfSeriesKeepDownloaded(Base):
    def test_enforcing_keep_downloaded_on_a_series_removes_its_strm_files(self):
        show = self.root / "shows" / "Cheers (1982)"; (show / "Season 01").mkdir(parents=True)
        ep = show / "Season 01" / "Cheers (1982) S01E01.strm"; ep.write_text("http://p/series/u/p/1.mp4")
        (show / "Season 01" / "Cheers - S01E01.mkv").write_bytes(b"\0" * 64)
        self.db.add(Series(tmdb_id=1414, title="Cheers", year="1982", source=f"provider_{self.p.id}",
                           provider_id=self.p.id, strm_path=str(show), sonarr_path=str(show)))
        self.db.add(Duplicate(tmdb_id=1414, media_type="series", resolution="keep_radarr",
                              sources=[{"source": "sonarr", "path": str(show)}, {"source": f"provider_{self.p.id}", "path": str(show)}]))
        self.db.commit()
        from services.sync import check_and_record_duplicate
        skipped = check_and_record_duplicate(1414, "series", f"provider_{self.p.id}", str(show), self.p, self.db)
        self.db.commit()
        row = self.db.query(Series).filter_by(tmdb_id=1414).one()
        self.assertTrue(skipped)
        self.assertEqual(row.source, "sonarr")
        self.assertIsNone(row.strm_path)
        self.assertFalse(ep.exists(), "row converted to Sonarr-only, but its VOD .strm episodes were left on disk "
                                      "(and nothing tracks them any more)")
        self.assertTrue((show / "Season 01" / "Cheers - S01E01.mkv").exists())


class KeepDownloadedStillCleansAPlainVodFolder(Base):
    def test_nfo_without_a_download_beside_it_is_removed(self):
        from services.duplicates import delete_vod_files
        d = self.root / "movies" / "Heat (1995)"; d.mkdir(parents=True)
        strm, nfo = d / "Heat (1995).strm", d / "Heat (1995).nfo"
        strm.write_text("http://p/movie/u/p/1.mp4"); nfo.write_text("<movie/>")
        delete_vod_files(str(strm))
        self.assertFalse(strm.exists())
        self.assertFalse(nfo.exists())
        self.assertFalse(d.exists())


if __name__ == "__main__":
    unittest.main()
