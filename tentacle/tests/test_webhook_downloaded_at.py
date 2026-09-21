"""A Download webhook stamps downloaded_at so the title is instantly 'recently added'.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import pathlib
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
    for k, v in (("radarr_url", "http://r"), ("radarr_api_key", "k"), ("data_dir", tempfile.mkdtemp())):
        mdb.set_setting(db, k, v)
    db.commit()
    return db


class TestRadarrWebhookStampsDownloadDate(unittest.TestCase):
    """Source-level: the Download branch sets downloaded_at (the worker is a
    nested closure with its own SessionLocal, awkward to drive in isolation)."""

    def test_download_branch_sets_downloaded_at(self):
        src = pathlib.Path("routers/radarr.py").read_text()
        i = src.index('if event_type == "Download":')
        block = src[i:i + 400]
        self.assertIn("db_movie.downloaded_at = datetime.utcnow()", block)


class TestEffectiveDateIsTheMostRecentArrival(unittest.TestCase):
    def test_max_of_date_added_and_downloaded_at(self):
        from services.smartlists import _resort_by_db_date
        from models.database import Movie
        db = _db()
        now = datetime(2026, 9, 21, 12, 0)
        # VOD title from January, downloaded today → newest.
        db.add(Movie(tmdb_id=1, title="Tony", source="provider_1",
                     date_added=now - timedelta(days=250), downloaded_at=now))
        # Pure VOD added last week.
        db.add(Movie(tmdb_id=2, title="Old VOD", source="provider_1",
                     date_added=now - timedelta(days=7), downloaded_at=None))
        # Download from a month ago.
        db.add(Movie(tmdb_id=3, title="Older download", source="radarr",
                     date_added=now - timedelta(days=30), downloaded_at=now - timedelta(days=30)))
        db.commit()
        items = [{"Id": f"j{t}", "ProviderIds": {"Tmdb": str(t)}} for t in (3, 2, 1)]
        cfg = {"MediaTypes": ["Movie"], "Order": {"SortOptions": [{"SortBy": "DateCreated", "SortOrder": "Descending"}]}}
        out = _resort_by_db_date(items, cfg, db)
        self.assertEqual([i["Id"] for i in out], ["j1", "j2", "j3"], "newest arrival (VOD or download) must be first")


if __name__ == "__main__":
    unittest.main()
