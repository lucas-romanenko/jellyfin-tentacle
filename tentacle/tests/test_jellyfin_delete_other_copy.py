"""#296: deleting one copy of a title in Jellyfin affects only that copy.

A film is in Jellyfin twice: the VOD .strm and a Radarr download in another
library. Deleting the download in Jellyfin's own UI made the plugin forward
"movie tmdb:N deleted"; the backend then deleted the VOD row (whose .strm was
still there) and removed "the first Jellyfin movie with tmdb N" -- by then the
surviving VOD item -- from every user's playlists.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import DownloadRequest, Duplicate, Movie, Series  # noqa: E402
import routers.library as library  # noqa: E402
from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.threads = []
        test = self

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, **kw):
                self.args = args

            def start(self):
                test.threads.append(self.args)

        p = mock.patch.object(library.threading, "Thread", _Thread)
        p.start()
        self.addCleanup(p.stop)
        a = mock.patch.object(library, "_deletion_authorised", lambda *a: True)
        a.start()
        self.addCleanup(a.stop)
        # Never "the first item with this TMDB id": the survivor would be found.
        s = mock.patch("services.jellyfin.JellyfinService.search_by_tmdb_id",
                       side_effect=AssertionError("looked the item up by TMDB id"))
        s.start()
        self.addCleanup(s.stop)
        user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="u1", is_admin=True)
        self.db.add(user)
        self.db.commit()
        self.user = user

    def delete(self, media_type="movie", tmdb_id=999003, item_id=None, path=None):
        extra = {k: v for k, v in (("item_id", item_id), ("path", path)) if v is not None}
        return library.delete_library_item(media_type, tmdb_id, None, self.db, **extra)


class TestMovieCopies(_Base):
    def setUp(self):
        super().setUp()
        folder = self.tmp / "vod" / "movies" / "Film (2001)"
        folder.mkdir(parents=True)
        self.strm = folder / "Film (2001).strm"
        self.strm.write_text("http://provider/movie/u/p/1.mp4")
        self.db.add(Movie(tmdb_id=999003, title="Film", year="2001", source="provider_1", provider_id=1,
                          strm_path=str(self.strm), nfo_path=str(self.strm.with_suffix(".nfo"))))
        self.db.add(Duplicate(tmdb_id=999003, media_type="movie", resolution="pending",
                              sources=[{"source": "provider_1", "path": str(self.strm)},
                                       {"source": "radarr", "path": "/data/movies/Film (2001)/Film (2001).mkv"}]))
        self.db.add(DownloadRequest(tmdb_id=999003, media_type="movie", user_id=self.user.id))
        self.db.commit()

    def row(self):
        self.db.expire_all()
        return self.db.query(Movie).filter(Movie.tmdb_id == 999003).first()

    def test_deleting_the_download_keeps_the_vod_row_and_its_playlist_entries(self):
        r = self.delete(item_id="dl0001", path="/media/movies/Film (2001)/Film (2001).mkv")
        self.assertFalse(r["deleted"])
        self.assertIsNotNone(self.row(), "the VOD copy's catalogue row was deleted")
        self.assertEqual(0, self.db.query(Duplicate).count(), "one copy left: no duplicate")
        self.assertEqual([(999003, "movie", "dl0001")], self.threads,
                         "only the deleted item's own (dead) entries are removed")

    def test_deleting_the_vod_copy_deletes_its_row(self):
        self.strm.unlink()
        r = self.delete(item_id="vod0001", path="/vod-movies/Film (2001)/Film (2001).strm")
        self.assertTrue(r["deleted"])
        self.assertIsNone(self.row())
        self.assertEqual([(999003, "movie", "vod0001")], self.threads)

    def test_an_older_plugin_that_sends_no_path_keeps_a_vod_row_whose_strm_is_there(self):
        r = self.delete()
        self.assertFalse(r["deleted"])
        self.assertIsNotNone(self.row())
        self.assertEqual([], self.threads, "no item id: never look one up by TMDB id")

    def test_an_older_plugin_still_deletes_a_row_whose_copy_is_gone(self):
        self.strm.unlink()
        r = self.delete()
        self.assertTrue(r["deleted"])
        self.assertIsNone(self.row())
        self.assertEqual([], self.threads, "a refresh prunes the dead entry; no TMDB lookup")

    def test_a_keep_downloaded_tombstone_survives_the_other_copys_delete(self):
        self.db.query(Duplicate).update({Duplicate.resolution: "keep_radarr"})
        self.db.commit()
        self.delete(item_id="dl0001", path="/media/movies/Film (2001)/Film (2001).mkv")
        self.assertEqual(1, self.db.query(Duplicate).count())


class TestDownloadRow(_Base):
    def test_deleting_the_download_itself_still_deletes_its_row(self):
        self.db.add(Movie(tmdb_id=999003, title="Film", year="2001", source="radarr",
                          radarr_path="/data/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"))
        self.db.commit()
        r = self.delete(item_id="dl0001", path="/media/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv")
        self.assertTrue(r["deleted"])
        self.assertEqual(0, self.db.query(Movie).count())


class TestSeriesCopies(_Base):
    def test_deleting_the_downloaded_show_keeps_the_vod_show(self):
        show = self.tmp / "vod" / "shows" / "Show (2010)"
        show.mkdir(parents=True)
        self.db.add(Series(tmdb_id=555, title="Show", year="2010", source="provider_1", provider_id=1,
                           strm_path=str(show)))
        self.db.commit()
        r = self.delete("series", 555, item_id="dlshow", path="/media/tv/Show (2010) [imdb-tt1]")
        self.assertFalse(r["deleted"])
        self.assertEqual(1, self.db.query(Series).count())

    def test_deleting_the_vod_show_deletes_its_row(self):
        show = self.tmp / "vod" / "shows" / "Show (2010)"
        self.db.add(Series(tmdb_id=555, title="Show", year="2010", source="provider_1", provider_id=1,
                           strm_path=str(show)))
        self.db.commit()
        r = self.delete("series", 555, item_id="vodshow", path="/vod-shows/Show (2010)")
        self.assertTrue(r["deleted"])
        self.assertEqual(0, self.db.query(Series).count())


if __name__ == "__main__":
    unittest.main()
