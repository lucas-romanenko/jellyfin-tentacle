"""'Bad copy? Get another one' (services/bad_copy.py + /api/library/replace).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException

import models.database as mdb
from services import bad_copy


class _Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status
        self.text = "" if data is None else json.dumps(data)

    def json(self):
        return self._data


class FakeArr:
    """Answers bad_copy's Radarr/Sonarr calls and records them."""

    def __init__(self):
        self.calls = []
        self.movie = {"id": 11, "tmdbId": 100, "title": "Dud Film", "hasFile": True, "monitored": True,
                      "movieFile": {"id": 501}}
        self.movie_history = [
            {"id": 1, "eventType": "grabbed", "date": "2026-01-01T00:00:00Z", "sourceTitle": "Old.Grab"},
            {"id": 2, "eventType": "grabbed", "date": "2026-02-01T00:00:00Z", "sourceTitle": "Dud.Film.GERMAN.1080p"},
            {"id": 3, "eventType": "downloadFolderImported", "date": "2026-02-01T01:00:00Z"},
        ]
        self.series = [{"id": 21, "tmdbId": 200, "title": "Show"}]
        self.episodes = [
            {"id": 71, "seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 901, "monitored": False},
            {"id": 72, "seasonNumber": 1, "episodeNumber": 3, "hasFile": False, "episodeFileId": 0},
        ]
        self.ep_history = {"records": [{"id": 9, "eventType": "grabbed", "date": "2026-03-01T00:00:00Z",
                                        "sourceTitle": "Show.S01E02.HC.SUBS"}]}
        self.fail = set()

    def __call__(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = url.split("/api/v3/")[1]
        self.calls.append((method, path, params, json))
        if path.split("?")[0] in self.fail:
            return _Resp({"message": "nope"}, 500)
        if method == "GET" and path == "movie":
            return _Resp([self.movie] if params.get("tmdbId") == self.movie["tmdbId"] else [])
        if method == "GET" and path == "history/movie":
            return _Resp(self.movie_history)
        if method == "GET" and path == "series":
            return _Resp(self.series)
        if method == "GET" and path == "episode":
            return _Resp(self.episodes)
        if method == "GET" and path == "history":
            return _Resp(self.ep_history)
        return _Resp(None)

    def writes(self):
        return [(m, p, j) for m, p, _, j in self.calls if m != "GET"]


class _Base(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in {"radarr_url": "http://r", "radarr_api_key": "k",
                     "sonarr_url": "http://s", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        self.arr = FakeArr()
        p = mock.patch.object(bad_copy.requests, "request", side_effect=self.arr)
        p.start()
        self.addCleanup(p.stop)


class TestMovie(_Base):
    def test_blocklists_the_latest_grab_then_deletes_and_searches(self):
        r = bad_copy.replace_movie(self.db, 100, user_name="Lucas")
        self.assertEqual([("POST", "history/failed/2", None),
                          ("DELETE", "moviefile/501", None),
                          ("POST", "command", {"name": "MoviesSearch", "movieIds": [11]})], self.arr.writes())
        self.assertTrue(r["blocklisted"])
        self.assertEqual("Dud.Film.GERMAN.1080p", r["release"])
        self.assertIn("won't come back", r["message"])
        self.assertTrue(bad_copy.is_replacing(self.db, "movie", 100))

    def test_an_unmonitored_movie_is_monitored_again(self):
        self.arr.movie["monitored"] = False
        bad_copy.replace_movie(self.db, 100)
        self.assertIn(("PUT", "movie/editor", {"movieIds": [11], "monitored": True}), self.arr.writes())

    def test_no_grab_history_still_replaces_but_says_so(self):
        self.arr.movie_history = []
        r = bad_copy.replace_movie(self.db, 100)
        self.assertFalse(r["blocklisted"])
        self.assertIn("same one could be picked again", r["message"])
        self.assertIn(("DELETE", "moviefile/501", None), self.arr.writes())

    def test_blocklisting_refused_still_replaces(self):
        self.arr.fail.add("history/failed/2")
        r = bad_copy.replace_movie(self.db, 100)
        self.assertFalse(r["blocklisted"])
        self.assertIn("couldn't be blocklisted", r["message"])

    def test_iptv_streams_are_refused(self):
        self.db.add(mdb.Movie(tmdb_id=100, title="Dud Film", source="provider_1"))
        self.db.commit()
        with self.assertRaises(bad_copy.BadCopyError) as e:
            bad_copy.replace_movie(self.db, 100)
        self.assertEqual(400, e.exception.status)
        self.assertEqual([], self.arr.writes())

    def test_no_file(self):
        self.arr.movie.update(hasFile=False, movieFile=None)
        with self.assertRaises(bad_copy.BadCopyError) as e:
            bad_copy.replace_movie(self.db, 100)
        self.assertEqual(409, e.exception.status)

    def test_not_in_radarr(self):
        with self.assertRaises(bad_copy.BadCopyError) as e:
            bad_copy.replace_movie(self.db, 999)
        self.assertEqual(404, e.exception.status)


class TestEpisode(_Base):
    def test_one_episode(self):
        r = bad_copy.replace_episode(self.db, 200, 1, 2, user_name="Lucas")
        self.assertEqual([("POST", "history/failed/9", None),
                          ("DELETE", "episodefile/901", None),
                          ("PUT", "episode/monitor", {"episodeIds": [71], "monitored": True}),
                          ("POST", "command", {"name": "EpisodeSearch", "episodeIds": [71]})], self.arr.writes())
        self.assertIn("Show S01E02", r["message"])
        history_call = next(c for c in self.arr.calls if c[1] == "history")
        self.assertEqual(71, history_call[2]["episodeId"])

    def test_episode_without_a_file(self):
        with self.assertRaises(bad_copy.BadCopyError) as e:
            bad_copy.replace_episode(self.db, 200, 1, 3)
        self.assertEqual(409, e.exception.status)
        self.assertIn("IPTV", str(e.exception))

    def test_unknown_episode(self):
        with self.assertRaises(bad_copy.BadCopyError) as e:
            bad_copy.replace_episode(self.db, 200, 5, 5)
        self.assertEqual(404, e.exception.status)


def _grab(i, when, title, download_id=None):
    return {"id": i, "eventType": "grabbed", "date": when, "sourceTitle": title, "downloadId": download_id}


def _imported(i, when, file_id, download_id=None, event="downloadFolderImported"):
    return {"id": i, "eventType": event, "date": when, "downloadId": download_id,
            "data": {} if file_id is None else {"fileId": str(file_id)}}


class TestTheGrabThatMadeTheFile(_Base):
    """#190: the release blocklisted is the one the file on disk came from,
    not simply the newest grab."""

    def _failed(self):
        return [p for m, p, _ in self.arr.writes() if p.startswith("history/failed/")]

    def test_an_upgrade_still_downloading_is_left_alone(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(701, "2026-09-16T10:00:00Z", "Movie.2024.720p.BAD", "A1"),
            _imported(702, "2026-09-16T11:00:00Z", 6, "A1"),
            _grab(703, "2026-09-25T08:00:00Z", "Movie.2024.1080p.UPGRADE", "C3"),
        ]
        r = bad_copy.replace_movie(self.db, 100)
        self.assertEqual(["history/failed/701"], self._failed())
        self.assertEqual("Movie.2024.720p.BAD", r["release"])
        self.assertTrue(r["blocklisted"])

    def test_an_upgrade_already_imported_is_the_one_blocklisted(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.720p", "A1"),
            _imported(2, "2026-09-01T11:00:00Z", 5, "A1"),
            _grab(3, "2026-09-10T10:00:00Z", "Movie.1080p.DUD", "C3"),
            _imported(4, "2026-09-10T11:00:00Z", 6, "C3"),
        ]
        bad_copy.replace_movie(self.db, 100)
        self.assertEqual(["history/failed/3"], self._failed())

    def test_a_file_a_rescan_found_blocklists_nothing_and_says_so(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.720p", "A1"),
            _imported(2, "2026-09-01T11:00:00Z", 5, "A1"),
            _imported(3, "2026-09-12T11:00:00Z", 6, event="movieFolderImported"),
        ]
        r = bad_copy.replace_movie(self.db, 100)
        self.assertEqual([], self._failed())
        self.assertFalse(r["blocklisted"])
        self.assertIn("Couldn't tell which release", r["message"])
        self.assertIn(("DELETE", "moviefile/6", None), self.arr.writes(), "the file is still replaced")

    def test_a_file_no_import_names_blocklists_nothing(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.720p", "A1"),
            _imported(2, "2026-09-01T11:00:00Z", 5, "A1"),
        ]
        self.assertFalse(bad_copy.replace_movie(self.db, 100)["blocklisted"])
        self.assertEqual([], self._failed())

    def test_a_client_without_download_ids_uses_the_last_grab_before_the_import(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.OLDER"),
            _grab(2, "2026-09-05T10:00:00Z", "Movie.BAD"),
            _imported(3, "2026-09-05T12:00:00Z", 6),
            _grab(4, "2026-09-20T10:00:00Z", "Movie.LATER"),
        ]
        bad_copy.replace_movie(self.db, 100)
        self.assertEqual(["history/failed/2"], self._failed())

    def test_a_download_added_by_hand_blames_no_earlier_grab(self):
        # The import's download id matches no grab (added to the client by hand):
        # an earlier grab of another release must not be blocklisted for it.
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.OTHER.RELEASE", "A1"),
            _imported(2, "2026-09-01T11:00:00Z", 5, "A1"),
            _imported(3, "2026-09-12T11:00:00Z", 6, "HAND9"),
        ]
        r = bad_copy.replace_movie(self.db, 100)
        self.assertEqual([], self._failed())
        self.assertFalse(r["blocklisted"])

    def test_an_older_radarr_without_file_ids_follows_the_newest_import(self):
        self.arr.movie["movieFile"] = {"id": 6}
        self.arr.movie_history = [
            _grab(1, "2026-09-01T10:00:00Z", "Movie.BAD", "A1"),
            _imported(2, "2026-09-01T11:00:00Z", None, "A1"),
            _grab(3, "2026-09-20T10:00:00Z", "Movie.UPGRADE", "C3"),
        ]
        bad_copy.replace_movie(self.db, 100)
        self.assertEqual(["history/failed/1"], self._failed())

    def test_the_episode_file_picks_its_own_grab(self):
        self.arr.ep_history = {"records": [
            _grab(12, "2026-09-26T10:00:00Z", "Show.S01E02.PROPER", "C3"),
            _imported(11, "2026-09-20T11:00:00Z", 901, "B2"),
            _grab(10, "2026-09-20T10:00:00Z", "Show.S01E02.HC.SUBS", "B2"),
        ]}
        r = bad_copy.replace_episode(self.db, 200, 1, 2)
        self.assertEqual(["history/failed/10"], self._failed())
        self.assertEqual("Show.S01E02.HC.SUBS", r["release"])


class TestReplacingKeepsTheRequest(_Base):
    def setUp(self):
        super().setUp()
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add(self.kid)
        self.db.commit()
        self.db.add_all([mdb.Movie(tmdb_id=100, title="Dud Film", source="radarr"),
                         mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id)])
        self.db.commit()

    def requests_left(self):
        return self.db.query(mdb.DownloadRequest).count()

    def test_jellyfin_delete_hook_keeps_it_while_replacing(self):
        from routers.library import delete_library_item
        bad_copy.mark_replacing(self.db, "movie", 100)
        with mock.patch("routers.library._cleanup_playlists_all_users"), mock.patch("threading.Thread"), \
                mock.patch("routers.library._deletion_authorised", lambda *a: True):
            delete_library_item("movie", 100, request=mock.Mock(), db=self.db)
        self.assertEqual(1, self.requests_left())

    def test_jellyfin_delete_hook_drops_it_otherwise(self):
        from routers.library import delete_library_item
        with mock.patch("routers.library._cleanup_playlists_all_users"), mock.patch("threading.Thread"), \
                mock.patch("routers.library._deletion_authorised", lambda *a: True):
            delete_library_item("movie", 100, request=mock.Mock(), db=self.db)
        self.assertEqual(0, self.requests_left())

    def test_the_mark_expires(self):
        mdb.set_setting(self.db, "replacing:movie:100", (datetime.utcnow() - timedelta(days=30)).isoformat())
        self.assertFalse(bad_copy.is_replacing(self.db, "movie", 100))
        bad_copy.mark_replacing(self.db, "movie", 100)
        self.assertTrue(bad_copy.is_replacing(self.db, "movie", 100))
        bad_copy.clear_replacing(self.db, "movie", 100)
        self.assertFalse(bad_copy.is_replacing(self.db, "movie", 100))


class TestEndpoint(_Base):
    def setUp(self):
        super().setUp()
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add_all([self.admin, self.kid])
        self.db.commit()

    def call(self, user, media_type="movie", tmdb_id=100, **body):
        from routers.library import ReplaceCopyBody, replace_copy
        return replace_copy(media_type, tmdb_id, ReplaceCopyBody(**body), db=self.db, user=user)

    def test_non_requester_refused(self):
        with self.assertRaises(HTTPException) as e:
            self.call(self.kid)
        self.assertEqual(403, e.exception.status_code)
        self.assertEqual([], self.arr.writes())

    def test_requester_allowed(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        self.assertTrue(self.call(self.kid)["ok"])

    def test_series_needs_an_episode(self):
        with self.assertRaises(HTTPException) as e:
            self.call(self.admin, "series", 200)
        self.assertEqual(400, e.exception.status_code)
        self.assertTrue(self.call(self.admin, "series", 200, season_number=1, episode_number=2)["ok"])

    def test_errors_pass_through(self):
        with self.assertRaises(HTTPException) as e:
            self.call(self.admin, "movie", 999)
        self.assertEqual(404, e.exception.status_code)


if __name__ == "__main__":
    unittest.main()
