"""POST /api/discover/manage-episodes must not report success it did not achieve.

Bugs (unchanged at 0e1805f), routers/discover.py `manage_episodes`:

1. `sonarr.get_series_by_tmdb()` downloads the whole series list and returns
   None on ANY error (`get_all_series()` returns []), so "Sonarr is down / slow"
   becomes `404 Series not found in Sonarr`.
2. The return values of `set_episode_monitoring()` are ignored: if Sonarr
   rejects the PUT the endpoint still answers `{"success": true, "monitored": N}`
   and every client shows "Updated - monitoring N episodes".
3. It first UNmonitors every episode and only then monitors the selection, so a
   failure between the two PUTs leaves the whole series unmonitored.

Expected: read failures -> 503 with a reason; a rejected monitoring change ->
502 with a reason; monitor the selection first, then unmonitor only the
episodes that are not selected.
"""
import contextlib
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import requests


def _install_web_stubs():
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        return
    except ImportError:
        pass

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, *a, **k):
            pass

        def _deco(self, *a, **k):
            return lambda fn: fn

        get = post = put = delete = patch = api_route = _deco

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda *a, **k: None
    fastapi.Query = lambda default=None, *a, **k: default
    fastapi.Body = lambda default=None, *a, **k: default
    for name in ("Request", "Response", "BackgroundTasks", "UploadFile", "File", "Form"):
        setattr(fastapi, name, type(name, (), {}))
    responses = types.ModuleType("fastapi.responses")
    for name in ("Response", "JSONResponse", "FileResponse", "StreamingResponse", "RedirectResponse", "HTMLResponse"):
        setattr(responses, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    fastapi.responses = responses

    class BaseModel:
        def __init__(self, **kw):
            fields = {}
            for klass in reversed(type(self).__mro__):
                fields.update(getattr(klass, "__annotations__", {}))
            for name in fields:
                setattr(self, name, kw.get(name, getattr(type(self), name, None)))

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = BaseModel
    sys.modules.update({"fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic})
    if "httpx" not in sys.modules:
        try:
            import httpx  # noqa: F401
        except ImportError:
            sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        import routers.auth  # noqa: F401
    except Exception:
        auth = types.ModuleType("routers.auth")
        auth.get_user_from_request = lambda *a, **k: None
        auth.require_admin = lambda *a, **k: None
        sys.modules["routers.auth"] = auth


_install_web_stubs()

from fastapi import HTTPException  # noqa: E402
import routers.discover as discover  # noqa: E402

SERIES = {"id": 5, "title": "Lanterns", "tvdbId": 424242, "tmdbId": 5555}
EPISODES = [{"id": 100 + n, "seasonNumber": 1, "episodeNumber": n, "monitored": n <= 2, "hasFile": False}
            for n in range(1, 5)]


def _resp(status, payload):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    return r


class FakeSonarr:
    def __init__(self, series_error=None, put_status=202):
        self.series_error = series_error
        self.put_status = put_status
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params, None))
        if url.endswith("/api/v3/series"):
            if self.series_error:
                raise self.series_error
            return _resp(200, [SERIES])
        if url.endswith("/api/v3/episode"):
            return _resp(200, EPISODES)
        raise AssertionError(url)

    def put(self, url, json=None, **kw):
        self.calls.append(("PUT", url, None, json))
        return _resp(self.put_status, {})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, None, json))
        return _resp(201, {"id": 1})


def _sonarr(fake):
    settings = {"sonarr_url": "http://sonarr:8989", "sonarr_api_key": "k"}
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(discover, "get_setting",
                                          side_effect=lambda db, k, d="": settings.get(k, d)))
    for verb in ("get", "put", "post"):
        stack.enter_context(mock.patch.object(requests.Session, verb,
                                              lambda s, url, _v=verb, **kw: getattr(fake, _v)(url, **kw)))
    return stack


class ManageEpisodes(unittest.TestCase):
    def _call(self, fake, selected, **extra):
        body = discover.ManageEpisodesBody(tmdb_id=5555, selected_episodes=selected, **extra)
        with _sonarr(fake):
            return discover.manage_episodes(body, db=None, user=SimpleNamespace(id=1))

    def test_happy_path_shape(self):
        """Regression guard: response keys used by pages.js / tentacle-discover.js / Android TV."""
        r = self._call(FakeSonarr(), [{"season": 1, "episode": 3}])
        self.assertEqual(r, {"success": True, "monitored": 1, "searching": 1})

    def test_sonarr_unreachable_is_503_not_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(FakeSonarr(series_error=requests.exceptions.ConnectionError("refused")),
                       [{"season": 1, "episode": 3}])
        self.assertEqual(ctx.exception.status_code, 503, ctx.exception.detail)

    def test_rejected_monitoring_change_is_not_success(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(FakeSonarr(put_status=500), [{"season": 1, "episode": 3}])
        self.assertEqual(ctx.exception.status_code, 502, ctx.exception.detail)

    def test_selection_is_monitored_before_anything_is_unmonitored(self):
        fake = FakeSonarr()
        self._call(fake, [{"season": 1, "episode": 3}])
        puts = [c[3] for c in fake.calls if c[0] == "PUT"]
        self.assertTrue(puts)
        self.assertEqual(puts[0], {"episodeIds": [103], "monitored": True},
                         "unmonitored everything before monitoring the selection")
        unmonitor = [p for p in puts if p["monitored"] is False]
        self.assertTrue(unmonitor)
        self.assertNotIn(103, unmonitor[0]["episodeIds"])

    # #764: an empty selection from a list that never loaded unmonitored the whole show.
    def test_empty_selection_without_unmonitor_all_changes_nothing(self):
        fake = FakeSonarr()
        with self.assertRaises(HTTPException) as ctx:
            self._call(fake, [])
        self.assertEqual(ctx.exception.status_code, 400, ctx.exception.detail)
        self.assertEqual([], [c for c in fake.calls if c[0] != "GET"])

    def test_selection_matching_no_episode_changes_nothing(self):
        fake = FakeSonarr()
        with self.assertRaises(HTTPException) as ctx:
            self._call(fake, [{"season": 9, "episode": 9}])
        self.assertEqual(ctx.exception.status_code, 400, ctx.exception.detail)
        self.assertEqual([], [c for c in fake.calls if c[0] != "GET"])

    def test_unticking_everything_on_purpose_unmonitors_the_show(self):
        fake = FakeSonarr()
        r = self._call(fake, [], unmonitor_all=True)
        self.assertEqual(r["monitored"], 0)
        puts = [c[3] for c in fake.calls if c[0] == "PUT"]
        self.assertEqual(puts, [{"episodeIds": [101, 102, 103, 104], "monitored": False}])


class SonarrEpisodesRead(unittest.TestCase):
    """GET /api/discover/sonarr-episodes: a failed read is an error, not an empty list (#764)."""

    def _get(self, fake):
        with _sonarr(fake):
            return discover.get_sonarr_episodes(5555, db=None)

    def test_episodes_listed(self):
        r = self._get(FakeSonarr())
        self.assertEqual((r["in_sonarr"], len(r["episodes"])), (True, 4))

    def test_failed_series_read_is_503(self):
        with self.assertRaises(HTTPException) as ctx:
            self._get(FakeSonarr(series_error=requests.exceptions.ConnectionError("refused")))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_failed_episode_read_is_503_not_an_empty_list(self):
        fake = FakeSonarr()
        real_get = fake.get

        def get(url, **kw):
            if url.endswith("/api/v3/episode"):
                raise requests.exceptions.ReadTimeout("slow")
            return real_get(url, **kw)
        fake.get = get
        with self.assertRaises(HTTPException) as ctx:
            self._get(fake)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
