"""Resolve All keeps each duplicate it resolved, even when a later one fails.

Keep VOD rolls the session back when the arr can't delete a title (so nothing
half-done is saved for THAT duplicate). But Resolve All marked each resolved
duplicate in the session and committed only after the loop, so a later
failure's rollback also undid the "resolved" mark of every duplicate before
it, whose download was already deleted: they came back as pending, and a
retry fails for good because the arr no longer has the title.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pathlib import Path

from models.database import Base, Duplicate, Movie, Series, Setting
from tmp_dirs import temp_dir
from routers import duplicates


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class ResolveAllKeepsEarlierSuccesses(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)(); self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r")):
            self.db.add(Setting(key=k, value=v))
        for tmdb, name in ((603, "Film"), (949, "Heat")):
            strm = Path(tmp) / "vod" / name / f"{name}.strm"   # the VOD copy Keep VOD keeps
            strm.parent.mkdir(parents=True)
            strm.write_text("http://p/movie/1.mp4")
            self.db.add(Movie(tmdb_id=tmdb, title=name, source="provider_1",
                              strm_path=f"{tmp}/vod/{name}/{name}.strm", radarr_path=f"/movies/{name} [1080p]"))
            self.db.add(Duplicate(tmdb_id=tmdb, media_type="movie", resolution="pending",
                                  sources=[{"source": "radarr", "path": f"/movies/{name} [1080p]/{name}.mkv"},
                                           {"source": "provider_1", "path": f"{tmp}/vod/{name}/{name}.strm"}]))
        self.db.commit()
        p = mock.patch("services.radarr.RadarrService"); radarr = p.start(); self.addCleanup(p.stop)
        # Radarr has the first film only: the second one fails with a 502
        radarr.return_value.get_movie_by_tmdb.side_effect = \
            lambda t: {"id": 1, "path": "/movies/Film [1080p]"} if t == 603 else None
        radarr.return_value.get_movie_files.return_value = [{"id": 10, "path": "/movies/Film [1080p]/Film.mkv"}]
        radarr.return_value.delete_movie_by_id.return_value = True

    def test_a_later_failure_does_not_reopen_an_earlier_resolution(self):
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((1, 1), (r["count"], r["failed"]))
        self.db.expire_all()
        states = {d.tmdb_id: d.resolution for d in self.db.query(Duplicate)}
        self.assertEqual({603: "keep_vod", 949: "pending"}, states,
                         "the film whose download was deleted is pending again")


class ResolveAllKeepDownloaded(unittest.TestCase):
    """The data-loss form: Keep All Downloaded deletes the first film's .strm,
    then the show fails because Jellyfin doesn't answer while its users'
    watched state is being carried over (502, rolled back). That reopened the film,
    whose VOD copy is already gone; "Keep VOD" on it then deletes the download
    too, and the film is gone completely."""
    def setUp(self):
        root = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{root}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)(); self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s")):
            self.db.add(Setting(key=k, value=v))
        film = root / "vod" / "movies" / "Film (1999)"; film.mkdir(parents=True)
        self.strm = film / "Film (1999).strm"; self.strm.write_text("x")
        show = root / "vod" / "shows" / "Show (2014)" / "Season 01"; show.mkdir(parents=True)
        (show / "Show S01E01.strm").write_text("x")
        self.db.add(Movie(tmdb_id=603, title="Film", source="provider_1", strm_path=str(self.strm)))
        self.db.add(Duplicate(tmdb_id=603, media_type="movie", resolution="pending",
                              sources=[{"source": "radarr", "path": "/movies/Film (1999) [1080p]/Film.mkv"},
                                       {"source": "provider_1", "path": str(self.strm)}]))
        self.db.add(Series(tmdb_id=555, title="Show", source="provider_1", strm_path=str(show.parent)))
        self.db.add(Duplicate(tmdb_id=555, media_type="series", resolution="pending",
                              sources=[{"source": "sonarr", "path": "/tv/Show (2014)"},
                                       {"source": "provider_1", "path": str(show.parent)}]))
        self.db.commit()
        p = mock.patch("services.radarr.RadarrService"); radarr = p.start(); self.addCleanup(p.stop)
        radarr.return_value.get_movie_by_tmdb.return_value = {"id": 3, "path": "/movies/Film (1999) [1080p]"}
        radarr.return_value.get_movie_files.return_value = [{"id": 30, "path": "/movies/Film (1999) [1080p]/Film.mkv"}]
        p = mock.patch("services.sonarr.SonarrService"); sonarr = p.start(); self.addCleanup(p.stop)
        sonarr.return_value.get_series_by_tmdb.return_value = {"id": 7, "path": "/tv/Show (2014)"}
        sonarr.return_value.get_episode_files.return_value = [{"id": 2, "path": "/tv/Show (2014)/Season 01/Show S01E02.mkv"}]
        from services.duplicates import UserDataCarryError

        def carry(db, dup, record, keep):
            if dup.tmdb_id == 555:
                raise UserDataCarryError(502, "Jellyfin didn't answer")
            return 0
        p = mock.patch("routers.duplicates.carry_user_data", side_effect=carry); p.start(); self.addCleanup(p.stop)

    def test_the_film_whose_vod_copy_was_deleted_stays_resolved(self):
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assertEqual((1, 1), (r["count"], r["failed"]))
        self.assertFalse(self.strm.exists())
        self.db.expire_all()
        states = {d.tmdb_id: d.resolution for d in self.db.query(Duplicate)}
        self.assertEqual({603: "keep_radarr", 555: "pending"}, states,
                         "the film's VOD copy is deleted but its duplicate is pending again")


if __name__ == "__main__":
    unittest.main()
