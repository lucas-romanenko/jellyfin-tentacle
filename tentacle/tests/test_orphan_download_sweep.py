"""services.jellyfin.sweep_orphaned_downloads must not read a failed Jellyfin fetch
as "every downloaded title was deleted".

Live evidence (reporter's deletion_log, 2026-08-13 .. 2026-09-17): 20 of 33 nightly
runs logged "orphan-sweep: 295-324 download record(s) removed — no longer present in
Jellyfin", i.e. every source='radarr' movie row, on alternating nights with the
normal ~14. The titles were in Jellyfin throughout and were re-imported by the next
night's Radarr scan.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, DownloadRequest, Setting, TentacleUser
import services.jellyfin as jellyfin

def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    # module-level disable would silence assertLogs() in every later test module
    logging.disable(logging.NOTSET)


class FakeJellyfinGet:
    """Serves /Items pages. ``fail_pages`` = {(type, start_index)} that time out
    (JellyfinService._get returns None on any transport error); ``empty_pages`` =
    pages answered 200 with no items but the real TotalRecordCount."""

    def __init__(self, movies, series, page_size=10000, fail_pages=(), empty_pages=()):
        self.data = {"Movie": movies, "Series": series}
        self.page_size = page_size
        self.fail_pages = set(fail_pages)
        self.empty_pages = set(empty_pages)

    def __call__(self, service, path, params=None):
        kind = params["IncludeItemTypes"]
        start = int(params["StartIndex"])
        if (kind, start) in self.fail_pages:
            return None
        items = self.data[kind]
        if (kind, start) in self.empty_pages:
            return {"Items": [], "TotalRecordCount": len(items)}
        limit = min(int(params["Limit"]), self.page_size)
        page = items[start:start + limit]
        return {"Items": [{"ProviderIds": {"Tmdb": str(t)}} for t in page],
                "TotalRecordCount": len(items)}


class TestOrphanDownloadSweep(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(Setting(key="jellyfin_url", value="http://jf"))
        self.db.add(Setting(key="jellyfin_api_key", value="k"))
        user = TentacleUser(jellyfin_user_id="u1", display_name="u")
        self.db.add(user)
        self.db.commit()
        # 300 downloaded movies, 20 downloaded series, 15,000 VOD movies in Jellyfin
        self.radarr_ids = list(range(1, 301))
        for t in self.radarr_ids:
            self.db.add(Movie(tmdb_id=t, title=f"D{t}", source="radarr"))
            self.db.add(DownloadRequest(tmdb_id=t, media_type="movie", user_id=user.id))
        for t in range(1, 21):
            self.db.add(Series(tmdb_id=t, title=f"S{t}", source="sonarr"))
        self.db.commit()
        self.jf_movies = list(range(100000, 115000)) + self.radarr_ids
        self.jf_series = list(range(1, 21))
        self._saved_get = jellyfin.JellyfinService._get

    def tearDown(self):
        jellyfin.JellyfinService._get = self._saved_get
        self.db.close()

    def _run(self, fake):
        jellyfin.JellyfinService._get = lambda service, path, params=None: fake(service, path, params)
        removed = jellyfin.sweep_orphaned_downloads(self.db)
        self.db.expire_all()
        return removed

    def test_first_page_timeout_deletes_nothing(self):
        """The Movie /Items request times out (15 s, e.g. during the library scan the
        nightly job has just triggered). _fetch_all_items() breaks and returns [], and at
        0e1805f all 300 radarr rows and their DownloadRequests are deleted: 6e50661
        added _fetch_all_items_checked() but sweep_orphaned_downloads() does not use it."""
        removed = self._run(FakeJellyfinGet(self.jf_movies, self.jf_series,
                                            fail_pages={("Movie", 0)}))
        self.assertEqual(removed, 0)
        self.assertEqual(self.db.query(Movie).count(), 300)
        self.assertEqual(self.db.query(DownloadRequest).count(), 300)

    def test_partial_fetch_deletes_nothing(self):
        """Page 2 of 2 times out: the id set holds only the first 10,000 items, so every
        downloaded title that sorts onto page 2 looks orphaned."""
        removed = self._run(FakeJellyfinGet(self.jf_movies, self.jf_series,
                                            fail_pages={("Movie", 10000)}))
        self.assertEqual(removed, 0)
        self.assertEqual(self.db.query(Movie).count(), 300)

    def test_empty_page_before_the_total_deletes_nothing(self):
        """Page 2 answers 200 with no items although TotalRecordCount says 15,300.
        _fetch_all_items_checked() at 0e1805f reports that as complete."""
        removed = self._run(FakeJellyfinGet(self.jf_movies, self.jf_series,
                                            empty_pages={("Movie", 10000)}))
        self.assertEqual(removed, 0)
        self.assertEqual(self.db.query(Movie).count(), 300)

    def test_empty_library_answer_deletes_nothing(self):
        """Defensive: Jellyfin answers 200 with zero movies (library mid-rebuild) while
        300 downloaded movies are recorded. That is not "all 300 were deleted"."""
        removed = self._run(FakeJellyfinGet([], self.jf_series))
        self.assertEqual(removed, 0)
        self.assertEqual(self.db.query(Movie).count(), 300)

    def test_genuinely_removed_download_is_still_swept(self):
        """Must-not-change: a complete fetch that lacks one downloaded title removes it."""
        jf = [t for t in self.jf_movies if t != 7]
        removed = self._run(FakeJellyfinGet(jf, self.jf_series))
        self.assertEqual(removed, 1)
        self.assertIsNone(self.db.query(Movie).filter(Movie.tmdb_id == 7).first())


if __name__ == "__main__":
    unittest.main()
