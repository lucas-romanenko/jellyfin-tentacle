"""Router-level regression cover for the original Add to Radarr/Sonarr reports.

#4  a series Sonarr already has ("This series has already been added") was a failure
#13 a root-folder read that failed fell back to a hardcoded /data/movies or /data/tv
#14 a failed add carried no reason in the response
#15 add_missing_to_radarr counted a movie Radarr already owns as a failure

The existing tests (test_arr_add.py, test_sonarr_add.py) pin the helpers the fix
introduced. These drive the four route functions themselves against a fake *arr over
real HTTP, so they exercise the same entry points before and after that refactor:
they fail on ffffd26^ and pass from ffffd26 on.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


class _FakeArr(BaseHTTPRequestHandler):
    """Radarr and Sonarr in one: answers are set per test in `routes`."""
    routes = {}
    posts = []

    def log_message(self, *a):
        pass

    def _answer(self, status, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        status, body = _FakeArr.routes.get(("GET", path), (200, []))
        self._answer(status, body)

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            _FakeArr.posts.append((path, json.loads(raw or b"{}")))
        except ValueError:
            _FakeArr.posts.append((path, raw))
        status, body = _FakeArr.routes.get(("POST", path), (201, {"id": 1}))
        self._answer(status, body)


SERIES_LOOKUP = [{"title": "Show", "tvdbId": 5, "tmdbId": 7, "year": 2020,
                  "seasons": [{"seasonNumber": 1, "monitored": True}],
                  "images": [], "titleSlug": "show"}]


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import TentacleUser, set_setting
        _FakeArr.routes = {}
        _FakeArr.posts = []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeArr)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        url = f"http://127.0.0.1:{self.srv.server_port}"

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(self.user)
        self.db.commit()
        for k, v in (("radarr_url", url), ("radarr_api_key", "k"),
                     ("sonarr_url", url), ("sonarr_api_key", "k"), ("data_dir", tmp.name)):
            set_setting(self.db, k, v)

    def lists(self):
        import routers.lists as lists
        return lists

    def call(self, fn, *args):
        """Run a route; an HTTPException is a refusal, returned as {'_http': status}."""
        from fastapi import HTTPException
        try:
            return fn(*args)
        except HTTPException as e:
            return {"_http": e.status_code, "detail": e.detail}


class TestIssue4SonarrAlreadyAdded(_Base):
    def test_series_sonarr_already_has_is_not_a_failure(self):
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"path": "/tv", "id": 1}])
        _FakeArr.routes[("GET", "/api/v3/series/lookup")] = (200, SERIES_LOOKUP)
        _FakeArr.routes[("POST", "/api/v3/series")] = (
            400, [{"propertyName": "TvdbId", "errorMessage": "This series has already been added",
                   "attemptedValue": 5, "severity": "error"}])
        lists = self.lists()
        resp = self.call(lists.add_to_sonarr, lists.AddMissingBody(tmdb_ids=[7]), self.db, self.user)
        self.assertEqual(resp.get("failed"), 0, f"#4: 'already been added' counted as a failure: {resp}")
        self.assertEqual(resp.get("already_exists"), 1, resp)


class TestIssue13NoGuessedRootFolder(_Base):
    def test_radarr_root_folder_failure_never_posts_a_guessed_path(self):
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (500, {"message": "busy"})
        lists = self.lists()
        resp = self.call(lists.add_to_radarr, lists.AddMissingBody(tmdb_ids=[949]), self.db, self.user)
        guessed = [p for p in _FakeArr.posts if p[0] == "/api/v3/movie"]
        self.assertEqual(guessed, [], f"#13: add sent with a guessed root folder: {guessed}")
        self.assertNotEqual(resp.get("added"), 1, resp)

    def test_sonarr_root_folder_failure_never_posts_a_guessed_path(self):
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (500, {"message": "busy"})
        _FakeArr.routes[("GET", "/api/v3/series/lookup")] = (200, SERIES_LOOKUP)
        lists = self.lists()
        resp = self.call(lists.add_to_sonarr, lists.AddMissingBody(tmdb_ids=[7]), self.db, self.user)
        guessed = [p for p in _FakeArr.posts if p[0] == "/api/v3/series"]
        self.assertEqual(guessed, [], f"#13: add sent with a guessed root folder: {guessed}")
        self.assertNotEqual(resp.get("added"), 1, resp)


class TestIssue14FailuresCarryAReason(_Base):
    def test_radarr_rejection_reason_reaches_the_response(self):
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"path": "/movies", "id": 1}])
        _FakeArr.routes[("POST", "/api/v3/movie")] = (
            400, [{"propertyName": "RootFolderPath", "errorMessage": "Folder is not writable by user abc",
                   "errorCode": "FolderWritableValidator"}])
        lists = self.lists()
        resp = self.call(lists.add_to_radarr, lists.AddMissingBody(tmdb_ids=[949]), self.db, self.user)
        self.assertEqual(resp.get("failed"), 1, resp)
        self.assertTrue(resp.get("detail"), f"#14: failure without a reason: {resp}")

    def test_sonarr_rejection_reason_reaches_the_response(self):
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"path": "/tv", "id": 1}])
        _FakeArr.routes[("GET", "/api/v3/series/lookup")] = (200, SERIES_LOOKUP)
        _FakeArr.routes[("POST", "/api/v3/series")] = (
            400, [{"propertyName": "QualityProfileId", "errorMessage": "Quality profile does not exist",
                   "errorCode": "QualityProfileExistsValidator"}])
        lists = self.lists()
        resp = self.call(lists.add_to_sonarr, lists.AddMissingBody(tmdb_ids=[7]), self.db, self.user)
        self.assertEqual(resp.get("failed"), 1, resp)
        self.assertTrue(resp.get("detail"), f"#14: failure without a reason: {resp}")


class TestIssue15AddMissingMovieExists(_Base):
    def test_movie_radarr_already_owns_is_not_a_failure(self):
        from models.database import ListSubscription, ListItem
        self.db.add(ListSubscription(id=1, user_id=1, name="L", type="trakt", url="x", tag="t"))
        self.db.add(ListItem(list_id=1, tmdb_id=949, media_type="movie", title="Heat"))
        self.db.commit()
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"path": "/movies", "id": 1}])
        _FakeArr.routes[("POST", "/api/v3/movie")] = (
            400, [{"propertyName": "TmdbId", "errorMessage": "This movie has already been added",
                   "errorCode": "MovieExistsValidator"}])
        lists = self.lists()
        resp = self.call(lists.add_missing_to_radarr, 1, None, self.db, self.user)
        self.assertEqual(resp.get("failed"), 0, f"#15: MovieExistsValidator counted as a failure: {resp}")
        self.assertEqual(resp.get("already_exists"), 1, resp)


if __name__ == "__main__":
    unittest.main()
