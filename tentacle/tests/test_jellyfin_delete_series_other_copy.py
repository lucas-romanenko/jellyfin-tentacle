"""#296 follow-up: deleting the Sonarr copy of a show in Jellyfin keeps the
VOD copy's row when the two are in separate libraries.

Sonarr and Tentacle both name a show's folder "<Title> (<Year>)", so the
download's folder (the path the plugin forwards) has the VOD folder's name,
and the one-part comparison took the deletion for the VOD copy's: the VOD
row went although its folder was still on disk.

The property test runs SEEDS random cases (films and shows, the download or
the VOD copy deleted, the same or another folder name, an older plugin with
no path, Tentacle's view of the VOD folder lagging Jellyfin's): the row goes
exactly when its own copy is gone from Tentacle's disk.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Duplicate, Movie, Series  # noqa: E402
import routers.library as library  # noqa: E402

SEEDS = int(os.environ.get("SEEDS", "1000"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.cleaned = []
        test = self

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, **kw):
                self.args = args

            def start(self):
                test.cleaned.append(self.args)

        for p in (mock.patch.object(library.threading, "Thread", _Thread),
                  mock.patch.object(library, "_deletion_authorised", lambda *a: True)):
            p.start()
            self.addCleanup(p.stop)

    def vod_show(self, tmdb_id, name="Show X (2020)"):
        show = self.tmp / "vod" / "shows" / name
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 01" / f"{name} S01E01.strm").write_text("http://p/series/u/p/1.mp4")
        self.db.add(Series(tmdb_id=tmdb_id, title="Show X", year="2020", source="provider_1",
                           provider_id=1, strm_path=str(show)))
        self.db.add(Duplicate(tmdb_id=tmdb_id, media_type="series", resolution="pending",
                              sources=[{"source": "provider_1", "path": str(show)},
                                       {"source": "sonarr", "path": f"/tv/{name}"}]))
        self.db.commit()
        return show


class TestSeparateFolders(_Base):
    def test_deleting_the_sonarr_copy_keeps_the_vod_row(self):
        show = self.vod_show(4242)
        r = library.delete_library_item("series", 4242, mock.Mock(), self.db,
                                        item_id="dl-show", path="/media/shows/Show X (2020)")
        self.assertEqual({"success": True, "deleted": False}, r)
        self.db.expire_all()
        self.assertTrue(show.exists())
        self.assertIsNotNone(self.db.query(Series).filter_by(tmdb_id=4242).first())
        self.assertEqual(0, self.db.query(Duplicate).count(), "the pending pairing goes")
        self.assertEqual([(4242, "series", "dl-show")], self.cleaned)

    def test_deleting_the_vod_copy_still_removes_the_row(self):
        show = self.vod_show(4243)
        shutil.rmtree(show)   # Jellyfin deleted the VOD show's folder
        r = library.delete_library_item("series", 4243, mock.Mock(), self.db,
                                        item_id="vod-show", path="/vod-shows/Show X (2020)")
        self.assertEqual({"success": True, "deleted": True}, r)
        self.assertIsNone(self.db.query(Series).filter_by(tmdb_id=4243).first())

    def test_a_folder_tentacle_still_sees_is_kept_and_logged(self):
        """Jellyfin's view of the VOD folder lost it, Tentacle's still has it
        (a lagging mount): the row stays, as with an older plugin."""
        self.vod_show(4244)
        with self.assertLogs("routers.library", level="INFO") as logs:
            r = library.delete_library_item("series", 4244, mock.Mock(), self.db,
                                            item_id="vod-show", path="/vod-shows/Show X (2020)")
        self.assertFalse(r["deleted"])
        self.assertTrue(any("is still on disk, so the row stays" in m for m in logs.output))

    def test_an_unreadable_path_decides_by_the_forwarded_path(self):
        self.vod_show(4245)
        with mock.patch.object(library.Path, "exists", side_effect=OSError("stale handle")):
            r = library.delete_library_item("series", 4245, mock.Mock(), self.db,
                                            item_id="vod-show", path="/vod-shows/Show X (2020)")
        self.assertTrue(r["deleted"])


class TestProperty(_Base):
    """The row goes iff its own copy is gone from Tentacle's disk."""

    def test_random_deletions(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        bad = []
        for seed in range(SEEDS):
            rnd = random.Random(seed)
            mt = rnd.choice(["movie", "series"])
            base = Path(tempfile.mkdtemp(dir=self.tmp))
            name = "Title (2001)"
            dl_name = name if rnd.random() < 0.7 else "Title (2001) {imdb-tt1}"
            vod_dir = base / "vod" / name
            vod_dir.mkdir(parents=True)
            tid = 10_000 + seed
            if mt == "movie":
                own = vod_dir / f"{name}.strm"
                own.write_text("http://p/movie/u/p/1.mp4")
                dl_path = f"/media/movies/{dl_name}/{dl_name} Bluray-1080p.mkv"
                vod_jf_path = f"/vod-movies/{name}/{name}.strm"
                self.db.add(Movie(tmdb_id=tid, title="Title", year="2001", source="provider_1",
                                  provider_id=1, strm_path=str(own)))
            else:
                (vod_dir / "Season 01").mkdir()
                dl_path = f"/media/shows/{dl_name}"
                vod_jf_path = f"/vod-shows/{name}"
                self.db.add(Series(tmdb_id=tid, title="Title", year="2001", source="provider_1",
                                   provider_id=1, strm_path=str(vod_dir)))
            self.db.commit()
            deleted_vod = rnd.random() < 0.4
            lagging = deleted_vod and rnd.random() < 0.2   # Tentacle's view still has it
            if deleted_vod and not lagging:
                shutil.rmtree(vod_dir)
            path = (vod_jf_path if deleted_vod else dl_path) if rnd.random() < 0.8 else None
            library.delete_library_item(mt, tid, mock.Mock(), self.db, item_id="x", path=path)
            self.db.expire_all()
            Model = Movie if mt == "movie" else Series
            kept = self.db.query(Model).filter_by(tmdb_id=tid).first() is not None
            gone_from_disk = deleted_vod and not lagging
            if kept == gone_from_disk:
                bad.append(f"seed {seed}: {mt} deleted={'vod' if deleted_vod else 'download'} "
                           f"lagging={lagging} path={path!r} -> row {'kept' if kept else 'deleted'}")
        print(f"\n#296 series property: {SEEDS} seeds, {len(bad)} violations")
        self.assertEqual([], bad[:5])


if __name__ == "__main__":
    unittest.main()
