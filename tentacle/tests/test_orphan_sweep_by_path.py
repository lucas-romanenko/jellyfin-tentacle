"""#261: the orphan sweep keeps a download Jellyfin still lists under another TMDB id.

The sweep deleted every radarr/sonarr row whose TMDB id Jellyfin didn't
report. When Radarr/Sonarr and Jellyfin matched the same folder to different
TMDB entries (or Jellyfin had none), the row was swept (with its download
requests) and re-imported by the next scan, night after night. A row whose
file/folder Jellyfin still lists is not an orphan.
"""
import logging
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, DownloadRequest, Setting, TentacleUser
import services.jellyfin as jellyfin
from services.jellyfin import JellyfinService, sweep_orphaned_downloads


class SweepByPath(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(Setting(key="jellyfin_url", value="http://jf"))
        self.db.add(Setting(key="jellyfin_api_key", value="k"))
        user = TentacleUser(jellyfin_user_id="u2", display_name="u")
        self.db.add(user)
        self.db.flush()
        # Radarr/Sonarr see their libraries at /data/..., Jellyfin at /media/...
        self.db.add(Series(tmdb_id=1001, title="Show A", source="sonarr", sonarr_path="/data/shows/Show A (2011)"))
        self.db.add(Movie(tmdb_id=2001, title="Film B", source="radarr",
                          radarr_path="/data/movies/Film B (2015)/Film B (2015) Bluray-1080p.mkv"))
        self.db.add(Movie(tmdb_id=2002, title="Gone Film", source="radarr",
                          radarr_path="/data/movies/Gone Film (2001)/Gone Film (2001).mkv"))
        self.db.add(DownloadRequest(tmdb_id=1001, media_type="series", user_id=user.id))
        self.db.commit()

    def _sweep(self, movies, series):
        seen = []

        def listing(self_, kind="Movie", user_scoped=False, ids_only=False):
            seen.append(ids_only)
            return (movies if kind == "Movie" else series), True

        with mock.patch.object(JellyfinService, "_fetch_all_items_checked", listing), \
             mock.patch.object(jellyfin, "SWEEP_MIN_ALLOWANCE", 50):
            return sweep_orphaned_downloads(self.db)

    def test_rows_whose_files_jellyfin_lists_under_other_ids_stay(self):
        movies = [
            {"ProviderIds": {"Tmdb": "2999"}, "Path": "/media/movies/Film B (2015)/Film B (2015) Bluray-1080p.mkv"},
            # A VOD .strm of the gone film under the same folder name is not the download.
            {"ProviderIds": {"Tmdb": "5"}, "Path": "/media/vod/movies/Gone Film (2001)/Gone Film (2001).strm"},
        ]
        series = [{"ProviderIds": {}, "Path": "/media/shows/Show A (2011)"},
                  {"ProviderIds": {"Tmdb": "6"}, "Path": "/media/shows/Other"}]
        removed = self._sweep(movies, series)
        self.assertEqual(1, removed)  # only the film that is really gone
        self.assertEqual({2001}, {m.tmdb_id for m in self.db.query(Movie).all()})
        self.assertEqual(1, self.db.query(Series).count())
        self.assertEqual(1, self.db.query(DownloadRequest).count())

    def test_listing_asks_for_paths(self):
        params = []

        def get(self_, path, params=None, **kw):
            params_seen.append(dict(params or {}))
            return {"Items": [], "TotalRecordCount": 0}

        params_seen = params
        with mock.patch.object(JellyfinService, "_get", get):
            JellyfinService("http://jf", "k")._fetch_all_items_checked("Movie", ids_only=True)
        self.assertIn("Path", params[0]["Fields"].split(","))


if __name__ == "__main__":
    unittest.main()
