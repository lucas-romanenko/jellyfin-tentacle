"""Regression cover for the original reports #1, #3, #11 and #16.

Every test here passes at 97d25e1 (v2.241.0) and at 0e1805f (v2.242.0). The #1 and #16 tests fail at the
pre-fix baseline dd32f32, which shows they exercise the original bugs.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie, Series, Provider  # noqa: E402
import services.sync as sync  # noqa: E402
from nightly_harness import NightlyHarness, FakeTMDB  # noqa: E402


class TestIssue1ProviderDelete(unittest.TestCase):
    def test_delete_provider_keeps_downloaded_episodes(self):
        """#1: deleting a provider must not rmtree a merged show folder."""
        import routers.providers as providers
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        p = Provider(name="P", server_url="http://x", username="u", password="p")
        db.add(p)
        db.commit()
        show = _RealPath(tmp) / "Community (2009)"
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 02").mkdir(parents=True)
        strm = show / "Season 01" / "Community (2009) S01E01.strm"
        strm.write_text("http://x")
        mkv = show / "Season 02" / "Community - S02E01.mkv"
        srt = show / "Season 02" / "Community - S02E01.en.srt"
        mkv.write_text("video")
        srt.write_text("subs")
        db.add(Series(tmdb_id=18347, title="Community", source=f"provider_{p.id}",
                      provider_id=p.id, strm_path=str(show)))
        db.commit()
        providers.delete_provider(p.id, db=db)
        self.assertFalse(strm.exists())
        self.assertTrue(mkv.exists(), "downloaded episode destroyed (#1)")
        self.assertTrue(srt.exists())


class TestIssue16CategoryOutage(NightlyHarness):
    def test_category_empty_on_one_sync_prunes_nothing(self):
        """#16: a large category answering [] once must not delete its titles."""
        self.add_category("1", name="EN - NEW RELEASE")
        self.add_category("2", name="EN - ACTION")
        self.catalogue_movies("1", [f"Fresh {i}" for i in range(300)])
        self.catalogue_movies("2", [f"Action {i}" for i in range(100)], first_tmdb=5000)
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 400)
        full = list(self.client.movies["1"])
        self.client.movies["1"] = []
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 400, "#16 prune on a single empty category")
        files = list((self.vod / "movies").rglob("*.strm"))
        self.assertEqual(len(files), 400)
        self.client.movies["1"] = full
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 400)

    def test_partial_outage_over_two_syncs_is_capped(self):
        """#16 fix 1: 60% of a provider missing on two consecutive syncs deletes nothing."""
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(500)])
        self.sync_only()
        self.client.movies["1"] = self.client.movies["1"][:200]
        self.sync_only()
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 500)


class TestIssue11SweepOutage(NightlyHarness):
    def test_one_night_of_unreadable_files_deletes_no_rows(self):
        """#11: a single failed exists() no longer deletes a row."""
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        for i in range(10):
            strm = _RealPath(self.movie(FakeTMDB.ids[f"Movie {i}"]).strm_path)
            strm.rename(strm.with_name(strm.name + ".offline"))
        self.night()
        self.assertEqual(self.db.query(Movie).count(), 100)

    def test_unavailable_vod_root_deletes_nothing_over_two_nights(self):
        """#11: an empty mount root is skipped, however many nights it lasts."""
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        movies = self.vod / "movies"
        movies.rename(self.vod / "movies.offline")
        movies.mkdir()
        sync.sweep_orphaned_vod_records(self.db)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()
        self.assertEqual(self.db.query(Movie).count(), 100)


class TestIssue3OptOut(NightlyHarness):
    def test_opted_out_hybrid_series_is_not_regenerated(self):
        """#3: after opting a hybrid show out (with delete), normal nights never write its
        .strm files again and the download stays."""
        import routers.library as library
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(20)])
        self.night()
        tmdb = FakeTMDB.ids["Show 1"]
        show = _RealPath(self.series_row(tmdb).strm_path)
        mkv = show / "Season 01" / "Show 1 - S01E01 - Pilot.mkv"
        mkv.write_text("video")

        class _Admin:
            display_name = "admin"
        body = library.StrmManagedBody(enabled=False, delete_files=True)
        library.set_strm_managed("series", tmdb, body, db=self.db, user=_Admin())
        for _ in range(3):
            self.night()
        self.assertFalse(list(show.rglob("*.strm")), "opted-out series regenerated (#3)")
        self.assertTrue(mkv.exists())
        self.assertTrue(self.series_row(tmdb).strm_disabled)


if __name__ == "__main__":
    unittest.main()
