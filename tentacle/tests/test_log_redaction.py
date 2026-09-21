"""Credentials must not reach the log.

Two kinds did, on every request of their kind:
  - the IPTV provider's username and password, which Xtream puts in the
    stream path (/live/<user>/<pass>/<id>.m3u8) — logged by the Live TV proxy
    and again by httpx's request logging on every channel tune;
  - each user's Jellyfin access token, which the plugin and the Android TV app
    send as ?api_key= — logged by uvicorn's access log on every poll.
`docker logs tentacle` is what the troubleshooting docs ask people to read
and share, and what log shippers and web log viewers collect.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import io
import logging
import unittest


class _Capture:
    def __init__(self, name):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.logger = logging.getLogger(name)

    def __enter__(self):
        self.logger.addHandler(self.handler)
        self.old = self.logger.level
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *a):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old)

    @property
    def text(self):
        return self.stream.getvalue()


class TestTheAppKeepsCredentialsOutOfTheLog(unittest.TestCase):
    """End to end: only imports the app, as it runs in the container."""

    def test_provider_password_and_access_token_never_reach_a_handler(self):
        import main  # noqa: F401  (configures logging as the container does)
        with _Capture("routers.livetv") as a, _Capture("httpx") as b:
            logging.getLogger("routers.livetv").info(
                f"[LiveTV] Stream request for channel 53 (CP24): "
                f"http://prov.example/live/alice/Hunter2pw/414149.m3u8")
            logging.getLogger("httpx").info(
                'HTTP Request: %s %s "%s %d %s"', "GET",
                "http://prov.example/live/alice/Hunter2pw/414149.m3u8", "HTTP/1.1", 302, "Found")
        self.assertNotIn("Hunter2pw", a.text + b.text)
        seen = []

        class Keep(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        acc = logging.getLogger("uvicorn.access")
        h = Keep(); acc.addHandler(h); old = acc.level; acc.setLevel(logging.INFO)
        try:
            acc.info('%s - "%s %s HTTP/%s" %d', "10.0.0.5:5000", "GET",
                     "/api/activity?userId=abc&api_key=JellyfinTok3n", "1.1", 200)
        finally:
            acc.removeHandler(h); acc.setLevel(old)
        self.assertNotIn("JellyfinTok3n", " ".join(seen))


class TestLogRedaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global log_redaction
        from services import log_redaction
        log_redaction.install()

    def test_xtream_credentials_in_a_stream_path(self):
        with _Capture("routers.livetv") as c:
            logging.getLogger("routers.livetv").info(
                "[LiveTV] Stream request for channel 53 (CP24): %s",
                "http://prov.example:8080/live/alice/Hunter2pw/414149.m3u8")
        self.assertNotIn("Hunter2pw", c.text)
        self.assertNotIn("alice", c.text)
        self.assertIn("414149.m3u8", c.text)  # still useful for debugging

    def test_an_fstring_message_is_redacted_too(self):
        url = "http://prov.example/movie/alice/Hunter2pw/99.mkv"
        with _Capture("services.sync") as c:
            logging.getLogger("services.sync").info(f"fetching {url}")
        self.assertNotIn("Hunter2pw", c.text)

    def test_httpx_request_log(self):
        with _Capture("httpx") as c:
            logging.getLogger("httpx").info(
                'HTTP Request: %s %s "%s %d %s"', "GET",
                "http://prov.example/live/alice/Hunter2pw/1.m3u8", "HTTP/1.1", 302, "Found")
        self.assertNotIn("Hunter2pw", c.text)
        self.assertIn("302", c.text)

    def test_query_string_secrets(self):
        for q in ("password=Hunter2pw", "api_key=Hunter2pw", "token=Hunter2pw",
                  "X-Emby-Token=Hunter2pw", "secret=Hunter2pw"):
            with self.subTest(q=q), _Capture("services.m3u_parser") as c:
                logging.getLogger("services.m3u_parser").info(
                    "Downloading %s", f"http://p/get.php?username=alice&{q}&type=m3u_plus")
                self.assertNotIn("Hunter2pw", c.text)
                self.assertIn("type=m3u_plus", c.text)

    def test_uvicorn_access_log_keeps_its_argument_shape(self):
        # uvicorn's AccessFormatter unpacks record.args into five names, so the
        # path is redacted in place rather than the message being flattened.
        logger = logging.getLogger("uvicorn.access")
        seen = []

        class Keep(logging.Handler):
            def emit(self, record):
                seen.append(record)

        h = Keep()
        logger.addHandler(h)
        try:
            logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.5:5000", "GET",
                        "/api/activity?userId=abc&api_key=Hunter2pw", "1.1", 200)
        finally:
            logger.removeHandler(h)
        rec = seen[0]
        self.assertEqual(len(rec.args), 5)
        self.assertNotIn("Hunter2pw", rec.getMessage())
        self.assertIn("userId=abc", rec.getMessage())

    def test_a_traceback_is_redacted_too(self):
        # requests/httpx put the full URL in the exception text, and
        # logger.error(..., exc_info=True) / logger.exception() render that
        # text at HANDLER time, after the record's message was cleaned. Seen
        # live: a provider answering 500 put "password=<pw>" into the log on
        # the traceback's last line while the message line above it was clean.
        def fail():
            raise RuntimeError(
                "500 Server Error for url: http://p.example/player_api.php"
                "?username=alice&password=Hunter2pw&action=get_live_categories")

        with _Capture("routers.livetv") as c:
            try:
                fail()
            except RuntimeError as e:
                logging.getLogger("routers.livetv").error(
                    f"[LiveTV] Group sync failed for provider 1: {e}", exc_info=True)
        self.assertIn("Traceback", c.text)          # the traceback is still there
        self.assertIn("get_live_categories", c.text)  # and still useful
        self.assertNotIn("Hunter2pw", c.text)

    def test_a_stream_path_in_a_traceback_is_redacted(self):
        with _Capture("routers.livetv") as c:
            try:
                raise ValueError("cannot open http://p.example/live/alice/Hunter2pw/5.ts")
            except ValueError:
                logging.getLogger("routers.livetv").exception("tune failed")
        self.assertNotIn("Hunter2pw", c.text)
        self.assertIn("5.ts", c.text)

    def test_ordinary_paths_are_left_alone(self):
        for s in ("/api/live/stream/123", "/api/live/sync-channels/5",
                  "http://jellyfin:8096/Items/abc", "Synced 42 movies"):
            with self.subTest(s=s):
                self.assertEqual(log_redaction.redact(s), s)

    def test_install_twice_does_not_stack(self):
        log_redaction.install()
        with _Capture("x.y") as c:
            logging.getLogger("x.y").info("n=%d", 5)
        self.assertEqual(c.text.strip(), "n=5")

    def test_main_installs_it(self):
        import main  # noqa: F401
        self.assertTrue(log_redaction.installed())


log_redaction = None


if __name__ == "__main__":
    unittest.main()
