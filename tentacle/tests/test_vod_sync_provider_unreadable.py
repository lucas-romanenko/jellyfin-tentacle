"""A VOD sync whose provider could not be read is not "completed" (#267).

A provider that failed every category fetch (down, an HTML page, a 5xx) was
reported as a completed run with no message, and Activity said "no streams
found in enabled categories". Runs the real sync_provider, with the real
XtreamClient over a stubbed HTTP session.
"""
import json
import unittest

import requests

import services.sync as sync
from models.database import Movie
from nightly_harness import NightlyHarness


class _Resp:
    def __init__(self, status, body):
        self.status_code, self.text = status, body
        self.headers = {"Content-Type": "text/html" if body.startswith("<") else "application/json"}
        self.content = body.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error: busy for url: http://provider/player_api.php")

    def json(self):
        return json.loads(self.text)


class _Session:
    """Stands in for XtreamClient.session: every request gets `outcome`."""
    headers = {}

    def __init__(self, outcome):
        self.outcome = outcome

    def get(self, url, *a, **k):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


class ProviderFailureIsNotCompleted(NightlyHarness):
    """#267"""

    OUTCOMES = {
        "refused": requests.ConnectionError("refused"),
        "timeout": requests.ReadTimeout("read timed out"),
        "html": _Resp(200, "<html><body>Login</body></html>"),
        "503": _Resp(503, "busy"),
    }

    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("s1", type_="series")
        self.catalogue_movies("1", ["Heat"])
        self.catalogue_series("s1", ["Friends"])
        self.night()                                   # a library, synced
        self.assertIsNotNone(self.movie(1000))

    def _broken_client(self, outcome):
        client = sync.XtreamClient(self.provider)
        client.session = _Session(outcome)
        # no waiting between retries in a unit test
        for name in ("RETRY_DELAYS", "_retry_delays"):
            if hasattr(client, name):
                setattr(client, name, ())
        return client

    def test_every_category_failing_fails_the_run_with_the_reason(self):
        for name, outcome in self.OUTCOMES.items():
            with self.subTest(name):
                client = self._broken_client(outcome)
                sync.make_provider_client = lambda p: client
                run = sync.sync_provider(self.provider, "full", self.db)
                self.assertEqual("failed", run.status)
                self.assertTrue(run.error_message)
                self.assertIn("could not be read", run.error_message)
                self.assertEqual(1, self.db.query(Movie).count())   # nothing pruned

    def test_an_html_page_is_named_as_such(self):
        client = self._broken_client(self.OUTCOMES["html"])
        sync.make_provider_client = lambda p: client
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertIn("web page", run.error_message)

    def test_some_categories_failing_completes_with_a_warning(self):
        self.client.raise_for = {"s1"}
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("completed", run.status)
        self.assertIn("1 of 2 categories could not be read", run.error_message)
        self.assertIn("provider timeout", run.error_message)

    def test_a_clean_sync_has_no_message(self):
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("completed", run.status)
        self.assertIsNone(run.error_message)


class ActivitySaysWhatFailed(NightlyHarness):
    """#267: "Sync now" (routers/sync.py) writes the reason to Activity."""

    def _sync_now(self):
        from unittest import mock
        import routers.sync as rsync
        from models.database import ActivityLog
        from sqlalchemy.orm import sessionmaker
        make = sessionmaker(bind=self.db.bind)
        with mock.patch("models.database.SessionLocal", make), \
                mock.patch("services.jellyfin.run_full_jellyfin_pipeline", lambda *a, **k: {}), \
                mock.patch.object(rsync, "sync_provider", sync.sync_provider):
            rsync._run_sync_background(self.provider.id, "full")
        s = make()
        try:
            return [a.message for a in s.query(ActivityLog).filter(ActivityLog.event == "vod_sync").all()]
        finally:
            s.close()

    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("s1", type_="series")
        self.catalogue_movies("1", ["Heat"])
        self.catalogue_series("s1", ["Friends"])

    def test_a_provider_down_is_said(self):
        self.client.raise_for = {"1", "s1"}
        messages = self._sync_now()
        self.assertEqual(1, len(messages), messages)
        self.assertIn("failed", messages[0])
        self.assertIn("could not be read", messages[0])
        self.assertNotIn("no streams found", messages[0])

    def test_some_categories_failing_is_said(self):
        self.client.raise_for = {"s1"}
        messages = self._sync_now()
        self.assertEqual(1, len(messages), messages)
        self.assertIn("1 of 2 categories could not be read", messages[0])


if __name__ == "__main__":
    unittest.main()
