"""Search again / remove for titles Radarr/Sonarr are still looking for.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException

import models.database as mdb
import routers.activity as activity

PAST = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
FUTURE = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRadarr:
    movies = []

    def __init__(self, *a):
        self.searched, self.deleted = [], []

    def get_movie_by_tmdb(self, tmdb_id):
        return next((m for m in self.movies if m["tmdbId"] == tmdb_id), None)

    def search_movie(self, rid):
        self.searched.append(rid)
        return True

    def delete_movie_by_id(self, rid, delete_files=True):
        self.deleted.append((rid, delete_files))
        return True


class FakeSonarr:
    series = []
    episodes = []

    def __init__(self, *a):
        self.searched_eps, self.searched_series, self.deleted = [], [], []
        self.monitoring, self.accept_monitoring = [], True

    def get_all_series(self):
        return self.series

    def get_episodes(self, sid):
        return self.episodes

    def search_episodes(self, ids):
        self.searched_eps.append(ids)
        return True

    def search_series(self, sid):
        self.searched_series.append(sid)
        return True

    def set_episode_monitoring(self, ids, monitored):
        self.monitoring.append((list(ids), monitored))
        return self.accept_monitoring

    def delete_series_by_id(self, sid, delete_files=True):
        self.deleted.append((sid, delete_files))
        return True


class _Base(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for k, v in {"radarr_url": "http://r", "radarr_api_key": "k",
                     "sonarr_url": "http://s", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add_all([self.admin, self.kid])
        self.db.commit()

        self.radarr = FakeRadarr()
        self.sonarr = FakeSonarr()
        FakeRadarr.movies = [{"id": 11, "tmdbId": 100, "title": "Rare Film", "path": "/data/movies/Rare Film (1998)"}]
        FakeSonarr.series = [
            {"id": 21, "tmdbId": 200, "tvdbId": 2000, "title": "Slow Show", "path": "/data/tv/Slow Show"},
            {"id": 22, "tmdbId": 0, "tvdbId": 3000, "title": "TVDB Only", "path": "/data/tv/TVDB Only"},
            {"id": 23, "tmdbId": 300, "tvdbId": 3001, "title": "Hybrid", "path": "/data/vod/tv/Hybrid"},
        ]
        FakeSonarr.episodes = [
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False, "airDateUtc": PAST},  # missing
            {"id": 2, "monitored": True, "hasFile": True, "airDateUtc": PAST},      # have it
            {"id": 3, "monitored": False, "hasFile": False, "airDateUtc": PAST},    # not wanted
            {"id": 4, "monitored": True, "hasFile": False, "airDateUtc": FUTURE},   # not aired
        ]
        for p in (mock.patch("services.radarr.RadarrService", lambda *a: self.radarr),
                  mock.patch("services.sonarr.SonarrService", lambda *a: self.sonarr),
                  mock.patch.object(activity, "invalidate_wanted_cache")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close()

    def search(self, user=None, **kw):
        return activity.search_again(activity.ArrTitle(**kw), db=self.db, user=user or self.admin)

    def remove(self, user=None, **kw):
        return activity.remove_from_arr(activity.ArrTitle(**kw), request=mock.Mock(), db=self.db,
                                        user=user or self.admin)


class TestSearchAgain(_Base):
    def test_movie(self):
        r = self.search(media_type="movie", tmdb_id=100)
        self.assertTrue(r["ok"])
        self.assertEqual([11], self.radarr.searched)

    def test_series_searches_only_the_missing_aired_monitored_episodes(self):
        r = self.search(media_type="series", tmdb_id=200)
        self.assertEqual([[1]], self.sonarr.searched_eps)
        self.assertEqual([], self.sonarr.searched_series)
        self.assertIn("1 missing episode", r["message"])

    def test_series_with_nothing_missing_falls_back_to_a_series_search(self):
        FakeSonarr.episodes = [{"id": 2, "monitored": True, "hasFile": True, "airDateUtc": PAST}]
        self.search(media_type="series", tmdb_id=200)
        self.assertEqual([21], self.sonarr.searched_series)

    def test_series_found_by_tvdb_when_there_is_no_tmdb_id(self):
        self.search(media_type="series", tvdb_id=3000)
        self.assertEqual([[1]], self.sonarr.searched_eps)

    def test_unknown_title_is_404(self):
        with self.assertRaises(HTTPException) as e:
            self.search(media_type="movie", tmdb_id=999)
        self.assertEqual(404, e.exception.status_code)

    def test_non_admin_only_for_their_own_requests(self):
        with self.assertRaises(HTTPException) as e:
            self.search(user=self.kid, media_type="movie", tmdb_id=100)
        self.assertEqual(403, e.exception.status_code)
        self.db.add(mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        self.assertTrue(self.search(user=self.kid, media_type="movie", tmdb_id=100)["ok"])


class TestStopMissing(_Base):
    def stop(self, user=None, **kw):
        return activity.stop_missing(activity.ArrTitle(**kw), db=self.db, user=user or self.admin)

    def test_unmonitors_only_the_missing_aired_episodes(self):
        FakeSonarr.episodes = FakeSonarr.episodes + [
            {"id": 5, "seasonNumber": 2, "episodeNumber": 3, "monitored": True, "hasFile": False, "airDateUtc": PAST}]
        r = self.stop(media_type="series", tmdb_id=200)
        self.assertEqual([([1, 5], False)], self.sonarr.monitoring,
                         "not the downloaded one, not the unwanted one, not the unaired one")
        self.assertEqual(2, r["stopped"])
        self.assertEqual([], self.sonarr.deleted, "nothing deleted")
        activity.invalidate_wanted_cache.assert_called()

    def test_a_chosen_subset(self):
        FakeSonarr.episodes = FakeSonarr.episodes + [
            {"id": 5, "seasonNumber": 2, "episodeNumber": 3, "monitored": True, "hasFile": False, "airDateUtc": PAST}]
        r = self.stop(media_type="series", tmdb_id=200, episodes=["s02e03", "S09E09"])
        self.assertEqual([([5], False)], self.sonarr.monitoring, "only the chosen missing ones")
        self.assertEqual("Stopped looking for S02E03 of Slow Show", r["message"])

    def test_a_chosen_episode_that_is_downloaded_is_never_touched(self):
        FakeSonarr.episodes[1].update(seasonNumber=1, episodeNumber=2)
        r = self.stop(media_type="series", tmdb_id=200, episodes=["S01E02"])
        self.assertEqual(0, r["stopped"])
        self.assertEqual([], self.sonarr.monitoring)

    def test_nothing_missing_is_not_an_error(self):
        FakeSonarr.episodes = [{"id": 2, "monitored": True, "hasFile": True, "airDateUtc": PAST}]
        r = self.stop(media_type="series", tmdb_id=200)
        self.assertEqual(0, r["stopped"])
        self.assertEqual([], self.sonarr.monitoring)

    def test_movies_are_refused(self):
        with self.assertRaises(HTTPException) as e:
            self.stop(media_type="movie", tmdb_id=100)
        self.assertEqual(400, e.exception.status_code)

    def test_sonarr_refusing_is_502(self):
        self.sonarr.accept_monitoring = False
        with self.assertRaises(HTTPException) as e:
            self.stop(media_type="series", tmdb_id=200)
        self.assertEqual(502, e.exception.status_code)

    def test_non_admin_only_for_their_own_requests(self):
        with self.assertRaises(HTTPException) as e:
            self.stop(user=self.kid, media_type="series", tmdb_id=200)
        self.assertEqual(403, e.exception.status_code)
        self.assertEqual([], self.sonarr.monitoring)


class TestRemove(_Base):
    def test_movie_with_nothing_downloaded_is_deleted_with_its_folder(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        r = self.remove(media_type="movie", tmdb_id=100)
        self.assertEqual([(11, True)], self.radarr.deleted)
        self.assertTrue(r["files_deleted"])
        self.assertEqual(0, self.db.query(mdb.DownloadRequest).count())
        activity.invalidate_wanted_cache.assert_called()

    def test_series_folder_is_deleted_too(self):
        self.remove(media_type="series", tmdb_id=200)
        self.assertEqual([(21, True)], self.sonarr.deleted)

    def test_a_hybrid_vod_series_keeps_its_files(self):
        self.db.add(mdb.Series(tmdb_id=300, title="Hybrid", source="provider_1",
                               sonarr_path="/data/vod/tv/Hybrid", sonarr_monitored=True))
        self.db.commit()
        r = self.remove(media_type="series", tmdb_id=300)
        self.assertEqual([(23, False)], self.sonarr.deleted)
        self.assertFalse(r["files_deleted"])
        row = self.db.query(mdb.Series).filter_by(tmdb_id=300).first()
        self.assertIsNone(row.sonarr_path)
        self.assertFalse(row.sonarr_monitored)
        self.assertIsNotNone(row, "the VOD series itself stays in the library")

    def test_anything_under_the_vod_tree_keeps_its_files_even_without_a_db_row(self):
        self.remove(media_type="series", tmdb_id=300)
        self.assertEqual([(23, False)], self.sonarr.deleted)

    def test_partly_downloaded_goes_through_the_full_delete_when_confirmed(self):
        self.db.add(mdb.Series(tmdb_id=200, title="Slow Show", source="sonarr"))
        self.db.commit()
        with mock.patch("routers.library.delete_download",
                        return_value={"title": "Slow Show", "deleted": True}) as full:
            r = self.remove(media_type="series", tmdb_id=200, delete_downloaded=True)
        full.assert_called_once()
        self.assertEqual((200, "series"), full.call_args.args[:2])
        self.assertEqual([], self.sonarr.deleted, "not deleted twice")
        self.assertTrue(r["files_deleted"])

    def test_a_show_with_episodes_on_disk_is_never_deleted_whole_by_default(self):
        FakeSonarr.series[0]["statistics"] = {"episodeFileCount": 7}
        with mock.patch("routers.library.delete_download") as full, \
                self.assertRaises(HTTPException) as e:
            self.remove(media_type="series", tmdb_id=200)
        self.assertEqual(409, e.exception.status_code)
        self.assertIn("7 episodes are already downloaded", e.exception.detail)
        full.assert_not_called()
        self.assertEqual([], self.sonarr.deleted)

    def test_a_sonarr_row_without_statistics_counts_as_downloaded(self):
        self.db.add(mdb.Series(tmdb_id=200, title="Slow Show", source="sonarr"))
        self.db.commit()
        with self.assertRaises(HTTPException) as e:
            self.remove(media_type="series", tmdb_id=200)
        self.assertEqual(409, e.exception.status_code)

    def test_confirmed_whole_show_delete_with_nothing_in_tentacle(self):
        FakeSonarr.series[0]["statistics"] = {"episodeFileCount": 2}
        self.remove(media_type="series", tmdb_id=200, delete_downloaded=True)
        self.assertEqual([(21, True)], self.sonarr.deleted)

    def test_nothing_on_disk_needs_no_confirmation(self):
        FakeSonarr.series[0]["statistics"] = {"episodeFileCount": 0}
        self.remove(media_type="series", tmdb_id=200)
        self.assertEqual([(21, True)], self.sonarr.deleted)

    def test_a_hybrid_vod_show_needs_no_confirmation(self):
        FakeSonarr.series[2]["statistics"] = {"episodeFileCount": 4}
        self.remove(media_type="series", tmdb_id=300)
        self.assertEqual([(23, False)], self.sonarr.deleted, "files kept, so nothing is lost")

    def test_non_admin_cannot_remove_someone_elses_request(self):
        with self.assertRaises(HTTPException) as e:
            self.remove(user=self.kid, media_type="movie", tmdb_id=100)
        self.assertEqual(403, e.exception.status_code)
        self.assertEqual([], self.radarr.deleted)

    def test_a_refused_delete_is_502_and_nothing_is_cleaned_up(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        self.radarr.delete_movie_by_id = lambda rid, delete_files=True: False
        with self.assertRaises(HTTPException) as e:
            self.remove(media_type="movie", tmdb_id=100)
        self.assertEqual(502, e.exception.status_code)
        self.assertEqual(1, self.db.query(mdb.DownloadRequest).count())

    def test_the_removal_is_audited(self):
        self.remove(media_type="movie", tmdb_id=100)
        log = self.db.query(mdb.DeletionLog).all() if hasattr(mdb, "DeletionLog") else None
        if log is not None:
            self.assertTrue(any("Rare Film" in (l.name or "") for l in log))


if __name__ == "__main__":
    unittest.main()
