"""Tests for services.stream_health's alive/dead/inconclusive classification.

The module's docstring promises that "Inconclusive results (network error,
malformed URL, provider down) are NEVER treated as dead — only a definitive
negative marks an entry bad". Two paths break that promise:

  * _probe_url() maps every 4xx to a definitive False, so 401/403/429 —
    exactly what an IPTV provider returns when the account's max_connections
    is already in use or it is rate-limiting the probe — marks the title dead.
  * check_stream() returns bool(data.get("info")) for any dict, so an Xtream
    auth-failure body ({"user_info": {"auth": 0, ...}}) is read as
    "the catalog says this vod_id is gone".

The daily sweep probes a rotating batch of up to 100 titles back to back, so a
single provider hiccup can fill the Health page with dead-stream entries whose
one-click action deletes the .strm, the .nfo and the movie's library row.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

import requests

import services.stream_health as sh


class _Resp:
    def __init__(self, status_code, body=b"\x00" * 1024):
        self.status_code = status_code
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        if self._body:
            yield self._body


def _patch_get(testcase, resp):
    orig = requests.get
    requests.get = lambda *a, **kw: resp
    testcase.addCleanup(setattr, requests, "get", orig)


class ProbeClassificationTests(unittest.TestCase):
    def test_403_is_inconclusive(self):
        """403 = max_connections in use / geo block, not proof of deletion."""
        _patch_get(self, _Resp(403))
        self.assertIsNone(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"))

    def test_429_is_inconclusive(self):
        """429 = the provider is rate-limiting our own sweep."""
        _patch_get(self, _Resp(429))
        self.assertIsNone(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"))

    def test_401_is_inconclusive(self):
        _patch_get(self, _Resp(401))
        self.assertIsNone(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"))

    # ── controls: these classifications must not change ──────────────────

    def test_404_is_still_definitively_dead(self):
        _patch_get(self, _Resp(404))
        self.assertIs(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"), False)

    def test_410_is_still_definitively_dead(self):
        _patch_get(self, _Resp(410))
        self.assertIs(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"), False)

    def test_200_with_bytes_is_alive(self):
        _patch_get(self, _Resp(206))
        self.assertIs(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"), True)

    def test_500_is_inconclusive(self):
        _patch_get(self, _Resp(503))
        self.assertIsNone(sh._probe_url("http://p.example/movie/u/p/1.mp4", "UA"))


class _Provider:
    provider_type = "xtream"
    server_url = "http://p.example"
    username = "u"
    password = "p"
    user_agent = "TiviMate/4.7.0"


def _patch_client(testcase, payload):
    import services.xtream_client as xc

    class _C:
        def __init__(self, *a, **kw):
            pass

        def get_vod_info(self, vod_id):
            return payload

        def close(self):
            pass

    orig = xc.XtreamClient
    xc.XtreamClient = _C
    testcase.addCleanup(setattr, xc, "XtreamClient", orig)


class CatalogClassificationTests(unittest.TestCase):
    def setUp(self):
        # Any fall-through probe is inconclusive, so the assertions below are
        # about the catalog decision alone.
        _patch_get(self, _Resp(503))

    def test_auth_failure_body_is_not_a_deletion(self):
        """{"user_info": {"auth": 0}} means our credentials were refused."""
        _patch_client(self, {"user_info": {"auth": 0, "status": "Banned"}})
        self.assertIsNot(
            sh.check_stream(None, "movie", "movie", 1, "http://p.example/movie/u/p/1.mp4", _Provider()),
            False, "an auth failure was read as 'the provider removed this title'")

    def test_empty_json_object_is_not_a_deletion(self):
        _patch_client(self, {})
        self.assertIsNot(
            sh.check_stream(None, "movie", "movie", 1, "http://p.example/movie/u/p/1.mp4", _Provider()),
            False, "an empty JSON body was read as a deletion")

    # ── controls ─────────────────────────────────────────────────────────

    def test_present_info_block_is_alive(self):
        _patch_client(self, {"info": {"tmdb_id": "1"}, "movie_data": {"stream_id": 1}})
        self.assertIs(
            sh.check_stream(None, "movie", "movie", 1, "http://p.example/movie/u/p/1.mp4", _Provider()),
            True)

    def test_explicitly_empty_info_block_is_still_dead(self):
        """The documented 'unknown vod_id' signal must keep working."""
        _patch_client(self, {"info": {}, "movie_data": {}})
        self.assertIs(
            sh.check_stream(None, "movie", "movie", 1, "http://p.example/movie/u/p/1.mp4", _Provider()),
            False)


if __name__ == "__main__":
    unittest.main()
