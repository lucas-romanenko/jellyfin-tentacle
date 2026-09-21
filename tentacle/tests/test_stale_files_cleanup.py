"""routers/settings.py "Delete All & Start Fresh" (POST /api/settings/stale-files/delete)
in a merged-folder setup.

The endpoint deletes every *.strm and every *.nfo under /media/vod/movies and
/media/vod/shows. When /media/vod/shows is also the Sonarr library (the merged setup
the docs describe), that includes Sonarr/Kodi episode .nfo and tvshow.nfo of shows
that have nothing to do with any .strm.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import tempfile
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie, Provider  # noqa: E402
import routers.settings as settings  # noqa: E402


class TestStaleFilesCleanup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.vod = _RealPath(tmp) / "vod"
        vod = self.vod

        def mapped(*parts):
            p = _RealPath(*parts)
            s = str(p)
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else p
        self._saved = settings.Path
        settings.Path = mapped

        shows = vod / "shows"
        (vod / "movies").mkdir(parents=True)
        # A show left behind by a previous .strm tool: only .strm + its tvshow.nfo
        self.old = shows / "Old Show (2001)"
        (self.old / "Season 01").mkdir(parents=True)
        (self.old / "tvshow.nfo").write_text("<tvshow/>")
        (self.old / "Season 01" / "Old Show S01E01.strm").write_text("http://old")
        # A Sonarr show in the same (merged) library, as found on the reporter's server
        self.dino = shows / "Dinosaurs (1991)"
        (self.dino / "Season 04").mkdir(parents=True)
        self.keep = [
            self.dino / "tvshow.nfo",
            self.dino / "Season 04" / "Dinosaurs - S4E10 - Working Girl.avi",
            self.dino / "Season 04" / "Dinosaurs - S4E10 - Working Girl.nfo",
            self.dino / "Season 04" / "Dinosaurs - S4E10 - Working Girl.en.hi.srt",
        ]
        for f in self.keep:
            f.write_text("sonarr")

    def tearDown(self):
        settings.Path = self._saved
        self.db.close()

    def test_start_fresh_keeps_metadata_of_downloaded_shows(self):
        """Fresh install pointed at a merged library that holds an old tool's .strm.
        At 0e1805f (unchanged since 97d25e1) the Sonarr episode .nfo and tvshow.nfo of Dinosaurs are deleted."""
        settings.delete_stale_files(body=settings.StaleFilesDelete(confirm=True), db=self.db)
        self.assertFalse(self.old.exists(), "old tool's .strm show not cleaned up")
        for f in self.keep:
            self.assertTrue(f.exists(), f"{f.relative_to(self.vod)} deleted")

    def test_start_fresh_refuses_once_tentacle_owns_content(self):
        """The GET only offers the cleanup on an empty install, but the POST does not
        re-check. After a sync it would delete every .strm Tentacle itself wrote."""
        p = Provider(name="P", server_url="x", username="u", password="p")
        self.db.add(p)
        self.db.commit()
        self.db.add(Movie(tmdb_id=1, title="M", source=f"provider_{p.id}", provider_id=p.id))
        self.db.commit()
        strm = self.old / "Season 01" / "Old Show S01E01.strm"
        try:
            settings.delete_stale_files(body=settings.StaleFilesDelete(confirm=True), db=self.db)
        except Exception as e:  # HTTPException(409) is the expected outcome
            self.assertEqual(getattr(e, "status_code", None), 409)
        self.assertTrue(strm.exists(), "stale-file cleanup ran on an install with synced content")




class TestStaleFilesBannerCount(TestStaleFilesCleanup):
    def test_the_banner_counts_what_start_fresh_would_delete(self):
        """The banner said `nfo_count: 10` when the action deleted 4 (#107):
        it counted Sonarr's episode NFOs and tvshow.nfo of downloaded shows,
        which the cleanup leaves alone."""
        (self.old / "Season 01" / "Old Show S01E01.nfo").write_text("<episodedetails/>")
        before = settings.check_stale_files(db=self.db)
        self.assertTrue(before["show"])
        result = settings.delete_stale_files(body=settings.StaleFilesDelete(confirm=True), db=self.db)
        self.assertEqual(before["strm_count"], result["deleted_strm"])
        self.assertEqual(before["nfo_count"], result["deleted_nfo"])
        self.assertEqual(before["nfo_count"], 2)  # the episode's NFO + Old Show's tvshow.nfo


if __name__ == "__main__":
    unittest.main()
