"""Shared harness for the multi-night deletion-guard tests (not a test module).

A "night" runs the real functions in the order run_scheduled_sync() in main.py
calls them: sync_provider(provider, "full", db), then
sweep_orphaned_vod_records(db). Only the outside world is faked: the Xtream
client, TMDB, and the /media/vod roots (redirected to a temp dir).
"""
import logging
import shutil
import tempfile
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, Provider, ProviderCategory
import services.sync as sync



# ── Harness ────────────────────────────────────────────────────────────────

class FakeTMDB:
    """Resolves "Title (Year)" to a fixed tmdb id. ``fail`` holds titles whose
    lookup errors out (TMDB 429/timeout)."""
    ids = {}
    fail = set()

    def __init__(self, *a, **k):
        pass

    def _meta(self, name, year):
        if name in FakeTMDB.fail or name not in FakeTMDB.ids:
            return None
        return {"tmdb_id": FakeTMDB.ids[name], "title": name, "year": year,
                "overview": "", "genres": [], "poster_path": None,
                "backdrop_path": None, "rating": None, "runtime": None}

    def search_movie(self, name, year=None, **kwargs):
        return self._meta(name, year)

    def search_series(self, name, year=None, **kwargs):
        return self._meta(name, year)


class FakeClient:
    """Per-category catalogue. ``raise_for`` holds category ids whose fetch
    raises this night (a transient provider error)."""

    def __init__(self, movies=None, series=None):
        self.movies = movies or {}   # cat_id -> [(title, stream_id)]
        self.series = series or {}   # cat_id -> [(title, series_id)]
        self.raise_for = set()

    def get_vod_streams(self, cat_id):
        if cat_id in self.raise_for:
            raise RuntimeError("provider timeout")
        return [{"name": f"{t} (2010)", "stream_id": sid, "container_extension": "mp4"}
                for t, sid in self.movies.get(cat_id, [])]

    def movie_stream_url(self, stream_id, ext):
        return f"http://provider/movie/u/p/{stream_id}.{ext}"

    def get_series_list(self, cat_id):
        if cat_id in self.raise_for:
            raise RuntimeError("provider timeout")
        return [{"name": f"{t} (2010)", "series_id": sid}
                for t, sid in self.series.get(cat_id, [])]

    def get_series_info(self, series_id):
        return {"episodes": {"1": [{"id": series_id * 10 + 1, "episode_num": 1,
                                    "container_extension": "mp4"}]}}

    def episode_stream_url(self, ep_id, ext):
        return f"http://provider/series/u/p/{ep_id}.{ext}"


class NightlyHarness(unittest.TestCase):
    """In-memory-ish SQLite DB + temp /media/vod + patched sync module."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.vod = _RealPath(tmp) / "vod"
        (self.vod / "movies").mkdir(parents=True)
        (self.vod / "shows").mkdir(parents=True)

        vod = self.vod

        def mapped_path(*parts):
            p = _RealPath(*parts)
            s = str(p)
            if s.startswith("/media/vod"):
                return _RealPath(str(vod) + s[len("/media/vod"):])
            return p

        self._saved = {k: getattr(sync, k, None) for k in
                       ("Path", "TMDBService", "make_provider_client",
                        "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT")}
        sync.Path = mapped_path
        sync.TMDBService = FakeTMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        self.client = FakeClient()
        sync.make_provider_client = lambda provider: self.client
        FakeTMDB.ids = {}
        FakeTMDB.fail = set()

        self.provider = Provider(name="P", server_url="http://provider", username="u",
                                 password="p", active=True)
        self.db.add(self.provider)
        self.db.commit()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(sync, k, v)
        self.db.close()
        self.db.bind.dispose()

    # helpers
    def add_category(self, cat_id, type_="movie", name=None):
        cat = ProviderCategory(provider_id=self.provider.id, category_id=cat_id,
                               category_name=name or f"CAT {cat_id}", type=type_,
                               whitelisted=True, source_tag=f"Tag{cat_id}")
        self.db.add(cat)
        self.db.commit()
        return cat

    def catalogue_movies(self, cat_id, titles, first_tmdb=1000):
        self.client.movies[cat_id] = [(t, first_tmdb + i) for i, t in enumerate(titles)]
        for i, t in enumerate(titles):
            FakeTMDB.ids[t] = first_tmdb + i

    def catalogue_series(self, cat_id, titles, first_tmdb=5000):
        self.client.series[cat_id] = [(t, first_tmdb + i) for i, t in enumerate(titles)]
        for i, t in enumerate(titles):
            FakeTMDB.ids[t] = first_tmdb + i

    def sync_only(self):
        """A manual "Sync now" (routers/sync.py) — prune, no sweep."""
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual(run.status, "completed", run.error_message)
        self.db.expire_all()

    def night(self):
        """One run_scheduled_sync: provider sync (with prune), then the VOD sweep."""
        self.sync_only()
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()

    def movie(self, tmdb_id):
        return self.db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()

    def series_row(self, tmdb_id):
        return self.db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
