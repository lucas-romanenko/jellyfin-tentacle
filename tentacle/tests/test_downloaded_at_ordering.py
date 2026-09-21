"""A downloaded copy sorts by when it was DOWNLOADED, not by when the title first
entered the library.

"Downloaded Movies" and "<user>'s Downloads" sort by date_added. For a title
that already existed as a VOD .strm when Radarr downloaded it, date_added is
the IPTV sync date (the Radarr scan only backfilled rows Radarr itself
created), so the newest download sat mid-row. Each row now also carries
downloaded_at, and a DateCreated playlist sorts by the most recent of the two.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                 ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                 ("data_dir", tempfile.mkdtemp())):
        mdb.set_setting(db, k, v)
    db.commit()
    return db


class TestRadarrScanRecordsTheDownloadDate(unittest.TestCase):
    def _scan(self, db, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return movies

        with mock.patch.object(radarr, "RadarrService", Fake), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            radarr.scan_radarr_library(db)
        db.commit()

    def test_a_vod_title_that_was_downloaded_later_gets_the_download_date(self):
        from models.database import Movie
        db = _db()
        old = datetime(2025, 3, 1)
        db.add(Movie(tmdb_id=603, title="Tony", year="2015", source="provider_1",
                     strm_path="/media/vod/movies/Tony (2015)/Tony (2015).strm", date_added=old))
        db.commit()
        self._scan(db, [{"tmdbId": 603, "title": "Tony", "year": 2015, "hasFile": True,
                         "path": "/movies/Tony (2015)",
                         "movieFile": {"path": "/movies/Tony (2015)/Tony.mkv", "dateAdded": "2026-09-20T21:15:00Z"}}])
        row = db.query(Movie).filter_by(tmdb_id=603).one()
        self.assertEqual(row.downloaded_at, datetime(2026, 9, 20, 21, 15))
        self.assertEqual(row.date_added, old, "date_added (first seen) must keep its meaning")
        self.assertEqual(row.source, "provider_1")

    def test_a_radarr_row_records_both_dates(self):
        from models.database import Movie
        db = _db()
        self._scan(db, [{"tmdbId": 1, "title": "Film", "year": 2000, "hasFile": True, "path": "/movies/Film (2000)",
                         "movieFile": {"path": "/movies/Film (2000)/f.mkv", "dateAdded": "2026-09-19T10:00:00Z"}}])
        row = db.query(Movie).filter_by(tmdb_id=1).one()
        self.assertEqual(row.downloaded_at, datetime(2026, 9, 19, 10, 0))
        self.assertEqual(row.date_added, datetime(2026, 9, 19, 10, 0))


class TestSonarrScanRecordsTheDownloadDate(unittest.TestCase):
    def _scan(self, db, shows):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self):
                return shows

        with mock.patch.object(sonarr, "SonarrService", Fake), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            sonarr.scan_sonarr_library(db)
        db.commit()

    def _show(self, tmdb_id, added):
        return {"tmdbId": tmdb_id, "tvdbId": tmdb_id, "title": f"Show {tmdb_id}", "path": f"/tv/Show {tmdb_id}",
                "added": added, "monitorNewItems": "none", "statistics": {"episodeFileCount": 3}}

    def test_a_vod_series_added_to_sonarr_gets_a_download_date(self):
        from models.database import Series
        db = _db()
        db.add(Series(tmdb_id=7, title="Show 7", source="provider_1", sonarr_path="/tv/Show 7",
                      date_added=datetime(2025, 1, 1)))
        db.commit()
        self._scan(db, [self._show(7, "2026-09-18T08:00:00Z")])
        row = db.query(Series).filter_by(tmdb_id=7).one()
        self.assertEqual(row.downloaded_at, datetime(2026, 9, 18, 8, 0))
        self.assertEqual(row.date_added, datetime(2025, 1, 1))

    def test_a_newer_episode_download_is_never_moved_back_by_the_scan(self):
        from models.database import Series
        db = _db()
        bumped = datetime(2026, 9, 21, 6, 0)  # set by the Download webhook
        db.add(Series(tmdb_id=7, title="Show 7", source="sonarr", sonarr_path="/tv/Show 7",
                      date_added=datetime(2026, 1, 1), downloaded_at=bumped))
        db.commit()
        self._scan(db, [self._show(7, "2026-01-01T00:00:00Z")])
        self.assertEqual(db.query(Series).filter_by(tmdb_id=7).one().downloaded_at, bumped)


class TestRecentlyAddedSortUsesTheMostRecentArrival(unittest.TestCase):
    def test_a_late_download_of_an_old_vod_title_sorts_first(self):
        from models.database import Movie
        from services.smartlists import _resort_by_db_date
        db = _db()
        now = datetime(2026, 9, 21, 12, 0)
        db.add(Movie(tmdb_id=1, title="Old VOD, downloaded yesterday", source="provider_1",
                     date_added=now - timedelta(days=200), downloaded_at=now - timedelta(days=1)))
        db.add(Movie(tmdb_id=2, title="Downloaded a week ago", source="radarr",
                     date_added=now - timedelta(days=7), downloaded_at=now - timedelta(days=7)))
        db.add(Movie(tmdb_id=3, title="Old download, never VOD", source="radarr",
                     date_added=now - timedelta(days=90), downloaded_at=None))
        db.commit()
        items = [{"Id": "j3", "ProviderIds": {"Tmdb": "3"}},
                 {"Id": "j2", "ProviderIds": {"Tmdb": "2"}},
                 {"Id": "j1", "ProviderIds": {"Tmdb": "1"}}]
        config = {"MediaTypes": ["Movie"], "Order": {"SortOptions": [{"SortBy": "DateCreated", "SortOrder": "Descending"}]}}
        out = _resort_by_db_date(items, config, db)
        self.assertEqual([i["Id"] for i in out], ["j1", "j2", "j3"])

    def test_other_sorts_are_untouched(self):
        from services.smartlists import _resort_by_db_date
        db = _db()
        items = [{"Id": "a"}, {"Id": "b"}]
        config = {"MediaTypes": ["Movie"], "Order": {"SortOptions": [{"SortBy": "ReleaseDate", "SortOrder": "Descending"}]}}
        self.assertEqual(_resort_by_db_date(items, config, db), items)


if __name__ == "__main__":
    unittest.main()
