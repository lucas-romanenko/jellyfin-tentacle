"""services.media_files in merged-folder layouts: delete only what Tentacle wrote.

Tentacle writes exactly these files:
  movies:  <Title (Year)>/<Title (Year)>.strm  and  <Title (Year)>.nfo
  series:  <Title (Year)>/tvshow.nfo  and  Season NN/<Title (Year)> SxxEyy.strm
It never writes episode .nfo or season.nfo. Everything else in a shared folder
belongs to Sonarr/Radarr, Jellyfin's NFO saver, or a release group.

Includes the regression cover for #1 through routers/providers.py::delete_provider.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie, Series, Provider  # noqa: E402
from services.media_files import delete_movie_files, delete_series_files  # noqa: E402


class TestSeriesDeleteMergedFolder(unittest.TestCase):
    def setUp(self):
        self.show = Path(tempfile.mkdtemp()) / "Brooklyn Nine-Nine (2013)"
        s1 = self.show / "Season 01"
        s4 = self.show / "Season 04"
        s1.mkdir(parents=True)
        s4.mkdir(parents=True)
        # Tentacle's files
        (self.show / "tvshow.nfo").write_text("<tvshow><originaltitle>x</originaltitle></tvshow>")
        self.strms = [s1 / f"Brooklyn Nine-Nine (2013) S01E0{i}.strm" for i in (1, 2, 3)]
        for p in self.strms:
            p.write_text("http://provider/series/1.mp4")
        # Sonarr download + Sonarr/Kodi metadata + subtitles
        self.foreign = [
            s4 / "Brooklyn Nine-Nine - S04E01 - Coast to Coast.mkv",
            s4 / "Brooklyn Nine-Nine - S04E01 - Coast to Coast.nfo",
            s4 / "Brooklyn Nine-Nine - S04E01 - Coast to Coast.en.srt",
            s4 / "season.nfo",
            # Scene release notes, exactly as found in a real hybrid show folder
            self.show / "Brooklyn.Nine-Nine.Season.4.Complete.720p.WEB.x264-[MULVAcoded].nfo",
            # Jellyfin NFO saver beside a download whose name matches Tentacle's
            # episode naming (Sonarr naming "{Series TitleYear} S{season:00}E{episode:00}")
            s4 / "Brooklyn Nine-Nine (2013) S04E02.mkv",
            s4 / "Brooklyn Nine-Nine (2013) S04E02.nfo",
        ]
        for p in self.foreign:
            p.write_text("not tentacle")

    def test_foreign_nfo_files_survive_series_delete(self):
        """Removing the VOD side of a hybrid show must leave every file Tentacle did not
        write. Fixed by cc231e0 (regression cover); at 97d25e1 delete_series_files() deletes every *.nfo in the tree: the
        Sonarr episode .nfo, season.nfo, the release .nfo, the Jellyfin .nfo and
        tvshow.nfo, although downloaded episodes still need them."""
        delete_series_files(self.show)
        for p in self.strms:
            self.assertFalse(p.exists(), f"{p.name} not deleted")
        for p in self.foreign:
            self.assertTrue(p.exists(), f"{p.relative_to(self.show)} was deleted")
        self.assertTrue((self.show / "tvshow.nfo").exists(),
                        "tvshow.nfo removed although downloaded episodes remain")

    def test_pure_vod_show_is_still_removed_completely(self):
        """Must-not-change: a show folder holding only Tentacle's files disappears."""
        show = Path(tempfile.mkdtemp()) / "Community (2009)"
        (show / "Season 01").mkdir(parents=True)
        (show / "tvshow.nfo").write_text("x")
        (show / "Season 01" / "Community (2009) S01E01.strm").write_text("http://x")
        self.assertEqual(delete_series_files(show), 2)
        self.assertFalse(show.exists())

    def test_nfo_matching_a_strm_stem_is_removed(self):
        """Must-not-change: <stem>.nfo next to <stem>.strm (and nothing else with that
        stem) counts as Tentacle's, as in the existing test_media_files fixture."""
        show = Path(tempfile.mkdtemp()) / "Community (2009)"
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 01" / "Community S01E01.strm").write_text("http://x")
        (show / "Season 01" / "Community S01E01.nfo").write_text("x")
        (show / "Season 01" / "Community S01E02.mkv").write_text("video")
        delete_series_files(show)
        self.assertFalse((show / "Season 01" / "Community S01E01.nfo").exists())
        self.assertTrue((show / "Season 01" / "Community S01E02.mkv").exists())


class TestMovieDeleteMergedFolder(unittest.TestCase):
    def test_nfo_shared_with_a_downloaded_copy_survives(self):
        """Radarr (and Tentacle's own Radarr scan, services/radarr.py) write
        <video stem>.nfo. In a merged movie folder that stem equals the .strm stem, so
        at 0e1805f delete_movie_files() still deletes the downloaded movie's .nfo
        (cc231e0 fixed the series path only)."""
        folder = Path(tempfile.mkdtemp()) / "Heat (1995)"
        folder.mkdir()
        strm = folder / "Heat (1995).strm"
        strm.write_text("http://provider/movie/1.mp4")
        (folder / "Heat (1995).mkv").write_text("video")
        (folder / "Heat (1995).nfo").write_text("<movie/>")
        delete_movie_files(strm)
        self.assertFalse(strm.exists())
        self.assertTrue((folder / "Heat (1995).mkv").exists())
        self.assertTrue((folder / "Heat (1995).nfo").exists(),
                        ".nfo of the downloaded copy was deleted")

    def test_pure_vod_movie_folder_is_removed(self):
        """Must-not-change."""
        folder = Path(tempfile.mkdtemp()) / "Heat (1995)"
        folder.mkdir()
        strm = folder / "Heat (1995).strm"
        strm.write_text("http://x")
        (folder / "Heat (1995).nfo").write_text("<movie/>")
        self.assertEqual(delete_movie_files(strm), 2)
        self.assertFalse(folder.exists())


class TestDeleteProviderMergedFolder(unittest.TestCase):
    """#1: DELETE /api/providers/{id} used shutil.rmtree on every series folder."""

    def _setup(self):
        import routers.providers as providers
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        p = Provider(name="P", server_url="http://x", username="u", password="p")
        db.add(p)
        db.commit()
        show = Path(tmp) / "shows" / "Community (2009)"
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 02").mkdir(parents=True)
        (show / "tvshow.nfo").write_text("<tvshow/>")
        (show / "Season 01" / "Community (2009) S01E01.strm").write_text("http://x")
        files = {
            "mkv": show / "Season 02" / "Community - S02E01 - Anthropology 101.mkv",
            "nfo": show / "Season 02" / "Community - S02E01 - Anthropology 101.nfo",
            "srt": show / "Season 02" / "Community - S02E01 - Anthropology 101.en.srt",
        }
        for f in files.values():
            f.write_text("download")
        db.add(Series(tmdb_id=18347, title="Community", source=f"provider_{p.id}",
                      provider_id=p.id, strm_path=str(show), sonarr_path=str(show)))
        db.commit()
        providers.delete_provider(p.id, db=db)
        return db, show, files

    def test_delete_provider_keeps_downloaded_media(self):
        """#1: at dd32f32 the whole show folder was rmtree'd, downloads included.
        Passes at 97d25e1 (#1 fixed)."""
        db, show, files = self._setup()
        self.assertFalse((show / "Season 01").exists(), "empty VOD season folder left behind")
        self.assertTrue(files["mkv"].exists())
        self.assertTrue(files["srt"].exists())
        self.assertEqual(db.query(Series).count(), 0)

    def test_delete_provider_keeps_download_metadata(self):
        """Follow-up: at 97d25e1 every .nfo in the hybrid folder went; passes since cc231e0."""
        db, show, files = self._setup()
        for f in (files["nfo"], show / "tvshow.nfo"):
            self.assertTrue(f.exists(), f"{f.name} deleted by provider removal")

if __name__ == "__main__":
    unittest.main()
