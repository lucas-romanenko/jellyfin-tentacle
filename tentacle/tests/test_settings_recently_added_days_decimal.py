"""A decimal "Recently added days" no longer stops every sync (#157 was the blank).

The field is <input type="number" min="1" max="365">, and saveSettings() posts
el.value as typed, so "14.5", "7.0" or "1e2" were saved as they are. Every
reader int()ed the setting, and int() refuses those: each provider sync failed
before its run was recorded, and the nightly tag refresh, the auto-playlist
list and the Radarr/Sonarr tag pass failed too, every night, until a whole
number was typed back in. They all read it through get_recently_added_days()
now: a fraction of a day is dropped, a value that is no number is the default.

Run from tentacle/:  python -m unittest discover -s tests -p "test_settings_recently_added_days_decimal.py"
"""
import logging
import random
import re
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, SyncRun, get_setting
from tmp_dirs import temp_dir

# Values a number input posts as typed (all valid floating-point numbers), and
# the whole days each one means.
TYPED = {"14.5": 14, "7.0": 7, "1e2": 100}


class _RunStarted(BaseException):
    """Raised once sync_provider is past its settings reads (not an Exception,
    so the sync's own except-Exception blocks don't swallow it)."""


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.seed_defaults(self.db)

    def _save(self, value):
        """Save the field as the Settings page does."""
        from routers import settings as r
        r.update_settings(r.SettingsUpdate(settings={"recently_added_days": value}), db=self.db)
        self.db.commit()


class DecimalWindow(_Base):
    def _movie(self, tmdb_id, days_ago):
        self.db.add(Movie(tmdb_id=tmdb_id, title=f"Film {tmdb_id}", source="provider_1", tags=[],
                          date_added=datetime.utcnow() - timedelta(days=days_ago)))
        self.db.commit()

    def test_the_nightly_tag_refresh_runs_on_the_whole_days(self):
        from services.tagger import refresh_recently_added_tags
        self._movie(1, days_ago=10)
        self._movie(2, days_ago=20)
        for value, recent in (("14.5", {1}), ("1e2", {1, 2}), ("7.0", set())):
            with self.subTest(value=value):
                self._save(value)
                refresh_recently_added_tags(self.db)           # raised ValueError
                tagged = {m.tmdb_id for m in self.db.query(Movie) if "Recently Added Movies" in (m.tags or [])}
                self.assertEqual(tagged, recent)

    def test_a_provider_sync_gets_past_its_settings(self):
        from services import sync as sync_mod
        p = Provider(name="p", server_url="http://127.0.0.1:9", username="u", password="p", active=True)
        self.db.add(p)
        self.db.commit()
        for value in TYPED:
            with self.subTest(value=value):
                self._save(value)
                with mock.patch.object(sync_mod, "unhide_vod_paths", side_effect=_RunStarted):
                    with self.assertRaises(_RunStarted):    # raised ValueError before the run existed
                        sync_mod.sync_provider(p, "full", self.db)
                self.db.rollback()
        self.assertEqual(self.db.query(SyncRun).count(), len(TYPED))

    def test_the_auto_playlist_list_counts_the_whole_days(self):
        from routers.smartlists import _compute_auto_playlists
        self._movie(1, days_ago=10)
        self._movie(2, days_ago=20)
        for value, days in TYPED.items():
            with self.subTest(value=value):
                self._save(value)
                rows = {r["key"]: r for r in _compute_auto_playlists(self.db)}   # raised ValueError
                recent = rows["builtin:recently_added_movies"]
                self.assertEqual(recent["origin"], f"Last {days} days")
                self.assertEqual(recent["item_count"], sum(1 for d in (10, 20) if d < days))

    def test_a_whole_number_is_stored_and_read_as_typed(self):
        from models.database import get_recently_added_days
        self._save("14")
        self.assertEqual(get_setting(self.db, "recently_added_days"), "14")
        self.assertEqual(get_recently_added_days(self.db), 14)

    def test_a_blank_still_reads_as_the_default(self):
        from models.database import get_recently_added_days
        self._save("")
        self.assertEqual(get_recently_added_days(self.db), 30)


class ArrScanTagsWithADecimalWindow(_Base):
    """The Radarr/Sonarr scans read the window for every title: each one raised,
    and no downloaded title got its tags."""

    def setUp(self):
        super().setUp()
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", str(self.tmp))):
            mdb.set_setting(self.db, k, v)
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self._save("14.5")

    def test_a_radarr_scan_tags_a_recent_download(self):
        import services.radarr as radarr
        root = self.tmp / "movies" / "Heat (1995)"
        root.mkdir(parents=True)
        (root / "Heat (1995).mkv").write_bytes(b"x")
        added = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        movies = [{"tmdbId": 949, "title": "Heat", "year": 1995, "hasFile": True, "path": str(root),
                   "movieFile": {"path": str(root / "Heat (1995).mkv"), "dateAdded": added}}]

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return [dict(m) for m in movies]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            radarr.scan_radarr_library(self.db)
        tags = self.db.query(Movie).filter_by(tmdb_id=949).one().tags or []
        self.assertIn("Downloaded Movies", tags)
        self.assertIn("Recently Added Movies", tags)

    def test_a_sonarr_scan_tags_a_recent_download(self):
        import services.sonarr as sonarr
        root = self.tmp / "tv" / "Breaking Bad (2008)"
        root.mkdir(parents=True)
        shows = [{"tmdbId": 1396, "tvdbId": 81189, "title": "Breaking Bad", "year": 2008,
                  "path": str(root), "monitorNewItems": "none", "statistics": {"episodeFileCount": 3}}]

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
        tags = self.db.query(mdb.Series).filter_by(tmdb_id=1396).one().tags or []
        self.assertIn("Downloaded TV", tags)
        self.assertIn("Recently Added TV", tags)


class EveryStoredValueIsAUsableWindow(_Base):
    """Whatever is stored (typed, posted to the API, or saved before this fix),
    the readers get a whole number of days the date arithmetic takes, and a
    whole number that worked before reads exactly as it did."""

    @staticmethod
    def _value(rnd):
        kind = rnd.randrange(6)
        if kind == 0:
            return str(rnd.randint(-1000, 40000))
        if kind == 1:
            return f"{rnd.uniform(-50, 1000):.{rnd.randint(1, 3)}f}"
        if kind == 2:
            return f"{rnd.randint(-9, 9)}e{rnd.randint(-3, 12)}"
        if kind == 3:
            return str(rnd.randint(-10 ** 12, 10 ** 12))
        if kind == 4:
            return rnd.choice(["", " ", "abc", "nan", "inf", "-inf", "1e309", "14,5", "0x10", "١٤", " 14 ", "+7"])
        return "".join(rnd.choice("0123456789.eE+- _x") for _ in range(rnd.randint(1, 6)))

    def test_seeded_values(self):
        from models.database import get_recently_added_days
        for seed in range(2000):
            value = self._value(random.Random(seed))
            with self.subTest(seed=seed, value=value):
                mdb.set_setting(self.db, "recently_added_days", value)
                days = get_recently_added_days(self.db)
                self.assertIsInstance(days, int)
                self.assertTrue(0 <= days <= 36500, days)
                datetime.utcnow() - timedelta(days=days)        # the readers' cutoff
                if re.fullmatch(r"\s*[+-]?\d+\s*", value):
                    self.assertEqual(days, min(max(int(value), 0), 36500))
                elif not value.strip():
                    self.assertEqual(days, 30)


if __name__ == "__main__":
    unittest.main()
