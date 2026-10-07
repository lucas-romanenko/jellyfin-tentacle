"""The storage-outage guard (#106) is not diluted by the other kind of download (#505).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_outage_guard_vod_dilution.py"

Since #378 the guard measures the loss against every row holding a Radarr
download: downloaded-only films and VOD titles Radarr downloaded too. With
downloads split over two shares, a share that drops out under the
downloaded-only films (3 of 4 report no file) was no longer judged an outage
when 10 VOD titles' downloads sat on the healthy share (3 of 14): the scan and
the missingFromDisk flush deleted the 3 rows and their requests. v1.9.0
refused (3 of 4). The same holds the other way round: VOD titles' downloads
lost under 10 healthy downloaded-only films were released. The loss is judged
over all downloads, the downloaded-only films and the VOD titles' downloads,
and refused if any of them looks like an outage.
"""
import logging
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie, DownloadRequest  # noqa: E402
import routers.radarr as radarr  # noqa: E402
import services.radarr as sr  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


class _Thread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{Path(temp_dir(self))}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for target, name, value in ((radarr.threading, "Thread", _Thread),
                                    (radarr, "emit_library_event", lambda *a, **k: None),
                                    (radarr, "log_activity", lambda *a, **k: None),
                                    (sr, "emit_library_event", lambda *a, **k: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        radarr._missing_pending.clear()
        self.addCleanup(radarr._missing_pending.clear)
        radarr._missing_recent.clear()
        self.addCleanup(radarr._missing_recent.clear)
        self.movies = []    # what Radarr lists
        self.lost = []      # tmdb ids whose file is gone (share B unmounted)
        mdb.set_setting(self.db, "radarr_url", "http://radarr")
        mdb.set_setting(self.db, "radarr_api_key", "k")

    def downloaded_only(self, n, lost=0, share="b"):
        for i in range(n):
            t = len(self.movies) + 1000
            has = i >= lost
            self.db.add(Movie(tmdb_id=t, title=f"R{t}", source="radarr", radarr_path=f"/movies-{share}/R{t}/r.mkv"))
            self.db.add(DownloadRequest(tmdb_id=t, media_type="movie", user_id=1))
            self.listed(t, has, share)

    def vod_downloads(self, n, lost=0, share="a"):
        for i in range(n):
            t = len(self.movies) + 1000
            has = i >= lost
            self.db.add(Movie(tmdb_id=t, title=f"V{t}", source="provider_1", provider_id=1,
                              strm_path=f"/vod/V{t}/V{t}.strm", radarr_path=f"/movies-{share}/V{t}/v.mkv",
                              tags=["Downloaded Movies"]))
            self.db.add(DownloadRequest(tmdb_id=t, media_type="movie", user_id=1))
            self.listed(t, has, share)

    def listed(self, t, has, share):
        self.movies.append({"tmdbId": t, "title": f"T{t}", "year": 2000, "hasFile": has,
                            "path": f"/movies-{share}/T{t}",
                            "movieFile": {"path": f"/movies-{share}/T{t}/t.mkv"} if has else None})
        if not has:
            self.lost.append(t)

    def scan(self):
        self.db.commit()
        svc = mock.MagicMock()
        svc.get_all_movies.return_value = self.movies
        with mock.patch.object(sr, "RadarrService", return_value=svc), \
                mock.patch("services.tmdb.get_tmdb_token", return_value=None):
            return sr.scan_radarr_library(self.db)

    def flush(self):
        self.db.commit()
        for t in self.lost:
            radarr._missing_pending[t] = (f"T{t}", f"/movies-b/T{t}")
        return radarr._flush_missing_from_disk(db=self.db)

    def downloads_kept(self):
        self.db.expire_all()
        rows = {m.tmdb_id for m in sr.downloaded_movie_rows(self.db)}
        requests = {r.tmdb_id for r in self.db.query(DownloadRequest)}
        return set(self.lost) <= rows and set(self.lost) <= requests


class DownloadedOnlyShareLost(_Base):
    """The reporter's case: 3 of 4 downloaded-only films lost, 10 VOD downloads fine."""

    def setUp(self):
        super().setUp()
        self.downloaded_only(4, lost=3, share="b")
        self.vod_downloads(10, share="a")

    def test_scan_refuses(self):
        out = self.scan()
        self.assertEqual(3, out["removals_refused"])
        self.assertEqual((0, 0), (out["removed"], out["released"]))
        self.assertTrue(self.downloads_kept())
        self.assertEqual(4, self.db.query(Movie).filter(Movie.source == "radarr").count())

    def test_missing_from_disk_flush_refuses(self):
        self.assertEqual({"status": "refused", "kept": 3}, self.flush())
        self.assertTrue(self.downloads_kept())


class VodDownloadShareLost(_Base):
    """The other way round: 3 of 4 VOD titles' downloads lost, 10 downloaded-only films fine."""

    def setUp(self):
        super().setUp()
        self.vod_downloads(4, lost=3, share="b")
        self.downloaded_only(10, share="a")

    def test_scan_refuses(self):
        out = self.scan()
        self.assertEqual(3, out["removals_refused"])
        self.assertEqual((0, 0), (out["removed"], out["released"]))
        self.assertTrue(self.downloads_kept())

    def test_missing_from_disk_flush_refuses(self):
        self.assertEqual({"status": "refused", "kept": 3}, self.flush())
        self.assertTrue(self.downloads_kept())


class OrdinaryCleanUpStillGoes(_Base):
    """Two lost files in each kind is housekeeping, not an outage: they go."""

    def setUp(self):
        super().setUp()
        self.downloaded_only(6, lost=2, share="b")
        self.vod_downloads(6, lost=2, share="b")

    def test_scan_removes(self):
        out = self.scan()
        self.assertEqual((2, 2, 0), (out["removed"], out["released"], out["removals_refused"]))

    def test_missing_from_disk_flush_removes(self):
        self.assertEqual({"status": "removed", "removed": 4}, self.flush())


if __name__ == "__main__":
    unittest.main()
