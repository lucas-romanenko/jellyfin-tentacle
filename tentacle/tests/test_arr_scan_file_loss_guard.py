"""An *arr whose files all went missing at once has lost its STORAGE, not its library.

Run from the tentacle/ directory:  python -m unittest discover -s tests

scan_radarr_library() / scan_sonarr_library() keep only titles that currently
have a file (`hasFile` / `episodeFileCount > 0`) and then delete every
radarr-/sonarr-sourced row that is not among them. When the *arr's media mount
drops, it reports every title as having no file; the response is not empty, so
the one guard ("No movies found in Radarr") does not fire, and the scan deleted
every downloaded row in one pass -- no cap, no deletion-log entry -- taking
date_added and tags with them and firing a removal event per title. This is the
same shape as the provider-outage prune (#25/#26), on the downloaded side.
"""
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _radarr_movie(i, has_file=True):
    return {"tmdbId": 1000 + i, "title": f"Film {i}", "year": 2000, "hasFile": has_file,
            "path": f"/movies/Film {i} (2000)", "movieFile": {"path": f"/movies/Film {i} (2000)/f.mkv"}}


def _sonarr_show(i, files=3):
    return {"tmdbId": 3000 + i, "tvdbId": i, "title": f"Show {i}", "path": f"/tv/Show {i}",
            "monitorNewItems": "none", "statistics": {"episodeFileCount": files}}


class _Base(unittest.TestCase):
    N = 12

    def setUp(self):
        import models.database as mdb
        self.mdb = mdb
        self.tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", self.tmp)):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        # The scan enriches new titles from TMDB with the built-in token; keep the
        # test offline instead of sending real requests to api.themoviedb.org.
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)


class RadarrScan(_Base):
    def _scan(self, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return movies

        with mock.patch.object(radarr, "RadarrService", Fake), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            out = radarr.scan_radarr_library(self.db)
        self.db.commit()
        return out

    def _seed(self):
        for i in range(self.N):
            self.db.add(self.mdb.Movie(tmdb_id=1000 + i, title=f"Film {i}", year="2000", source="radarr",
                                       radarr_path=f"/movies/Film {i} (2000)"))
        self.db.commit()

    def _rows(self):
        return self.db.query(self.mdb.Movie).filter_by(source="radarr").count()

    def test_every_file_missing_at_once_deletes_nothing(self):
        self._seed()
        out = self._scan([_radarr_movie(i, has_file=False) for i in range(self.N)])
        self.assertEqual(self.N, self._rows(), "a storage outage in Radarr wiped every downloaded movie row")
        self.assertEqual(self.N, out.get("removals_refused"))

    def test_a_few_files_really_deleted_are_still_removed(self):
        self._seed()
        self._scan([_radarr_movie(i, has_file=(i >= 2)) for i in range(self.N)])
        self.assertEqual(self.N - 2, self._rows())

    def test_titles_removed_from_radarr_itself_are_still_removed(self):
        """Gone from Radarr altogether is a decision somebody made, however many."""
        self._seed()
        self._scan([_radarr_movie(i) for i in range(3)])
        self.assertEqual(3, self._rows())

    def test_a_removal_is_written_to_the_deletion_log(self):
        self._seed()
        self._scan([_radarr_movie(i) for i in range(self.N - 1)])
        log = self.db.query(self.mdb.DeletionLog).all()
        self.assertEqual(1, len(log))
        self.assertIn("Film", log[0].name)


class SonarrScan(_Base):
    def _scan(self, shows):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self):
                return shows

        with mock.patch.object(sonarr, "SonarrService", Fake), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.commit()
        return out

    def _seed(self):
        for i in range(self.N):
            self.db.add(self.mdb.Series(tmdb_id=3000 + i, title=f"Show {i}", source="sonarr",
                                        sonarr_path=f"/tv/Show {i}"))
        self.db.commit()

    def _rows(self):
        return self.db.query(self.mdb.Series).filter_by(source="sonarr").count()

    def test_every_file_missing_at_once_deletes_nothing(self):
        self._seed()
        # one show still has files so the scan does not stop at "No series found"
        shows = [_sonarr_show(i, files=0) for i in range(self.N)] + [_sonarr_show(99)]
        out = self._scan(shows)
        self.assertEqual(self.N, self.db.query(self.mdb.Series).filter(
            self.mdb.Series.tmdb_id < 3099, self.mdb.Series.source == "sonarr").count(),
            "a storage outage in Sonarr wiped every downloaded series row")
        self.assertEqual(self.N, out.get("removals_refused"))

    def test_a_few_shows_really_emptied_are_still_removed(self):
        self._seed()
        self._scan([_sonarr_show(i, files=(0 if i < 2 else 3)) for i in range(self.N)])
        self.assertEqual(self.N - 2, self._rows())

    def test_shows_removed_from_sonarr_itself_are_still_removed(self):
        self._seed()
        self._scan([_sonarr_show(i) for i in range(3)])
        self.assertEqual(3, self._rows())



class DownloadRequestsFollowTheirTitle(_Base):
    """A title that leaves Radarr/Sonarr takes its download request with it, as
    the orphan sweep already does (#107)."""

    def _user(self):
        u = self.mdb.TentacleUser(jellyfin_user_id="u1", display_name="u")
        self.db.add(u); self.db.commit()
        return u.id

    def test_radarr_removal_also_removes_the_download_request(self):
        uid = self._user()
        for i in range(3):
            self.db.add(self.mdb.Movie(tmdb_id=1000 + i, title=f"Film {i}", year="2000", source="radarr"))
            self.db.add(self.mdb.DownloadRequest(tmdb_id=1000 + i, media_type="movie", user_id=uid))
        self.db.commit()
        RadarrScan._scan(self, [_radarr_movie(i) for i in (0, 1)])  # Film 2 left Radarr
        self.assertEqual({1000, 1001}, {m.tmdb_id for m in self.db.query(self.mdb.Movie).all()})
        self.assertEqual({1000, 1001}, {r.tmdb_id for r in self.db.query(self.mdb.DownloadRequest).all()})

    def test_sonarr_removal_also_removes_the_download_request(self):
        uid = self._user()
        for i in range(3):
            self.db.add(self.mdb.Series(tmdb_id=3000 + i, title=f"Show {i}", source="sonarr",
                                        sonarr_path=f"/tv/Show {i}"))
            self.db.add(self.mdb.DownloadRequest(tmdb_id=3000 + i, media_type="series", user_id=uid))
        self.db.commit()
        SonarrScan._scan(self, [_sonarr_show(i) for i in (0, 1)])  # Show 2 left Sonarr
        self.assertEqual({3000, 3001}, {s.tmdb_id for s in self.db.query(self.mdb.Series).all()})
        self.assertEqual({3000, 3001}, {r.tmdb_id for r in self.db.query(self.mdb.DownloadRequest).all()})


if __name__ == "__main__":
    unittest.main()
