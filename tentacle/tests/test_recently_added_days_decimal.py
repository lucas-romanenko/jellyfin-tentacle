"""#536: a decimal "Recently added days" must not break every reader of it.

The field is <input type="number">: "14.5", "7.0" and "1e2" are posted as
typed ("7.0" and "1e2" even pass its validation), and update_settings stored
them. Every reader then called int() on the stored string and raised
ValueError: every provider sync (before its SyncRun row existed), the nightly
tag refresh, GET /api/smartlists/auto-playlists and the per-title tag pass of
the Radarr and Sonarr scans, every night until a whole number was typed back.
#157 only fixed the empty value. Installs that already stored such a value
must recover too, so the readers have to cope with whatever is stored: a
fraction counts as its whole days, a value no number can be read from is the
default, and the window is clamped (no negative window, nothing past 100
years: timedelta overflowed past year 1).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p "test_recently_added_days_decimal.py"
"""
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting
from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.seed_defaults(self.db)

    def _save(self, **settings):
        from routers import settings as r
        r.update_settings(r.SettingsUpdate(settings=settings), db=self.db)
        self.db.commit()

    def _store(self, value):
        # What an install that saved it before the fix holds, whatever the save does now
        self.db.query(Setting).filter_by(key="recently_added_days").one().value = value
        self.db.commit()

    def _window(self):
        from routers.smartlists import _compute_auto_playlists
        rows = {r["key"]: r for r in _compute_auto_playlists(self.db)}
        return rows["builtin:recently_added_movies"]["origin"]


class ReadersCopeWithWhatIsStored(_Base):
    def test_a_decimal_typed_in_settings_keeps_the_readers_working(self):
        from services.tagger import refresh_recently_added_tags
        for typed in ("14.5", "7.0", "1e2"):   # what the number field posts as typed
            with self.subTest(typed=typed):
                self._save(recently_added_days=typed)
                refresh_recently_added_tags(self.db)   # the nightly tag refresh: raised ValueError
                self._window()                          # GET /api/smartlists/auto-playlists: 500

    def test_the_window_is_whole_days(self):
        for stored, shown in (("14", "Last 14 days"), (" 14 ", "Last 14 days"),
                              ("14.5", "Last 14 days"), ("7.0", "Last 7 days"),
                              ("1e2", "Last 100 days")):
            with self.subTest(stored=stored):
                self._store(stored)
                self.assertEqual(shown, self._window())

    def test_a_value_no_number_can_be_read_from_is_the_default(self):
        for stored in ("abc", "nan", "inf", "-inf", "1e309"):
            with self.subTest(stored=stored):
                self._store(stored)
                self.assertEqual("Last 30 days", self._window())

    def test_the_window_is_clamped(self):
        for stored, shown in (("-5", "Last 0 days"), ("1000000", "Last 36500 days")):
            with self.subTest(stored=stored):
                self._store(stored)
                self.assertEqual(shown, self._window())   # 1000000 overflowed timedelta

    def test_the_tag_refresh_reads_a_fraction_as_its_whole_days(self):
        from services.tagger import refresh_recently_added_tags
        now = datetime.utcnow()
        self.db.add_all([
            mdb.Movie(tmdb_id=1, title="New", source="radarr", date_added=now - timedelta(days=13)),
            mdb.Movie(tmdb_id=2, title="Old", source="radarr", date_added=now - timedelta(days=15)),
        ])
        self.db.commit()
        self._store("14.5")
        refresh_recently_added_tags(self.db)
        tags = {m.title: m.tags or [] for m in self.db.query(mdb.Movie)}
        self.assertIn("Recently Added Movies", tags["New"])
        self.assertNotIn("Recently Added Movies", tags["Old"])


class ProviderSyncStarts(_Base):
    def test_a_provider_sync_gets_as_far_as_recording_its_run(self):
        from services import sync as sync_mod

        class RunStarted(BaseException):   # not caught by the sync's own except Exception
            pass
        p = mdb.Provider(name="p", server_url="http://127.0.0.1:9", username="u", password="p", active=True)
        self.db.add(p)
        self.db.commit()
        self._save(recently_added_days="14.5")
        # unhide_vod_paths is the sync's first step after its SyncRun row exists
        with mock.patch.object(sync_mod, "unhide_vod_paths", side_effect=RunStarted):
            with self.assertRaises(RunStarted):   # was ValueError, before any run was recorded
                sync_mod.sync_provider(p, "full", self.db)
        self.assertEqual(1, self.db.query(mdb.SyncRun).count())


class ArrScansTagRecentDownloads(_Base):
    def setUp(self):
        super().setUp()
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", str(self.tmp))):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self._store("14.5")
        self.yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_the_radarr_scan_tags_a_recent_download(self):
        import services.radarr as radarr
        root = self.tmp / "movies" / "Ronin (1998)"
        root.mkdir(parents=True)
        (root / "Ronin (1998).mkv").write_bytes(b"x")
        movies = [{"tmdbId": 8195, "title": "Ronin", "year": 1998, "hasFile": True, "path": str(root),
                   "movieFile": {"path": str(root / "Ronin (1998).mkv"), "dateAdded": self.yesterday}}]

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return [dict(m) for m in movies]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            stats = radarr.scan_radarr_library(self.db)
        self.assertEqual(1, stats["nfo_written"])   # the per-title pass raised for every title
        self.assertIn("<tag>Recently Added Movies</tag>", (root / "Ronin (1998).nfo").read_text())

    def test_the_sonarr_scan_tags_a_recent_download(self):
        import services.sonarr as sonarr
        root = self.tmp / "tv" / "Breaking Bad (2008)"
        root.mkdir(parents=True)
        shows = [{"tmdbId": 1396, "tvdbId": 81189, "title": "Breaking Bad", "year": 2008,
                  "path": str(root), "added": self.yesterday, "monitorNewItems": "none",
                  "statistics": {"episodeFileCount": 3}}]

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, raise_errors=False):
                return [dict(s) for s in shows]

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            sonarr.scan_sonarr_library(self.db)
        self.assertIn("<tag>Recently Added TV</tag>", (root / "tvshow.nfo").read_text())


if __name__ == "__main__":
    unittest.main()
