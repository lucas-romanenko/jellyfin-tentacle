"""A provider that refuses this server (HTTP 401/403 on everything) is not
asked again and again for the rest of the sync (#149).

The provider of one household started answering 403 to every request in the
middle of a sync (an IP block): the sync went on asking. Every category left
(14 of them) and every series still to go (get_series_info, whose errors the
series backfill swallows) got a request, each refused, and the run ended
with "could not be read (the provider answered HTTP 403)". A panel's flood
protection counts those too, so the block lasts longer; the next morning the
whole account was still refused (no VOD, no Live TV).

After a few refusals in a row the client stops sending: every further call
fails at once with a plain reason ("the provider refuses this server ...").
A success in between starts the count again, and server errors (5xx) and
timeouts are not refusals.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import types
import unittest

import requests

from nightly_harness import NightlyHarness
import services.sync as sync

LOGIN = ("u", "s3cret-pass")
REFUSAL_LIMIT = 3   # at most this many refused requests in a row


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = [] if body is None else body
        self.text = "Forbidden" if status >= 400 else "[]"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: "
                                     f"http://panel.example/player_api.php?username={LOGIN[0]}"
                                     f"&password={LOGIN[1]}", response=self)

    def json(self):
        return self._body


class _Session:
    """Answers each request with the next status of `statuses` (the last one
    repeats), and remembers every URL it was asked for."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return _Response(status)


def _client(session):
    c = sync.XtreamClient(types.SimpleNamespace(id=1, server_url="http://panel.example",
                                                username=LOGIN[0], password=LOGIN[1]))
    c.session = session
    return c


def _ask(client, times=10):
    """Category fetches and series lookups, as the sync makes them; every
    error is caught, as the sync's category loop and backfill do."""
    errors = []
    for i in range(times):
        for call in (lambda: client.get_vod_streams(str(i)),
                     lambda: client.get_series_list(str(i)),
                     lambda: client.get_series_info(i)):
            try:
                call()
            except Exception as e:
                errors.append(e)
    return errors


class TheClientStopsAsking(unittest.TestCase):
    def test_after_a_few_refusals_in_a_row_no_more_requests(self):
        session = _Session(403)
        _ask(_client(session))
        self.assertGreaterEqual(len(session.calls), 1)
        self.assertLessEqual(len(session.calls), REFUSAL_LIMIT,
                             f"{len(session.calls)} requests to a provider that refused every one")

    def test_401_is_a_refusal_too(self):
        session = _Session(401)
        _ask(_client(session))
        self.assertLessEqual(len(session.calls), REFUSAL_LIMIT)

    def test_the_reason_is_plain_and_carries_no_login(self):
        errors = _ask(_client(_Session(403)))
        reason = sync._provider_error_reason(errors[-1])
        self.assertIn("403", reason)
        self.assertIn("refus", reason.lower())
        self.assertNotIn(LOGIN[1], reason)
        self.assertNotIn("player_api", reason)

    def test_a_success_in_between_starts_the_count_again(self):
        session = _Session(403, 403, 200, 403, 403, 200, 403, 403, 200)
        client = _client(session)
        for i in range(9):
            try:
                client.get_vod_streams(str(i))
            except Exception:
                pass
        self.assertEqual(9, len(session.calls))

    def test_server_errors_are_not_refusals(self):
        session = _Session(500)
        client = _client(session)
        for i in range(6):
            try:
                client.get_vod_streams(str(i))
            except Exception:
                pass
        self.assertEqual(6, len(session.calls))


class TheSyncEndsSayingSo(NightlyHarness):
    """A whole sync against a provider that refuses everything: a few
    requests, then a failed run whose message says the provider refuses this
    server. Nothing is removed (no category was read)."""

    def test_a_refused_sync_stops_early_and_says_why(self):
        for cat_id in ("1", "2", "3", "4", "5", "6"):
            self.add_category(cat_id, "movie")
        for cat_id in ("7", "8", "9"):
            self.add_category(cat_id, "series")
        session = _Session(403)
        client = _client(session)
        client.provider_id = self.provider.id
        sync.make_provider_client = lambda provider: client
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertLessEqual(len(session.calls), REFUSAL_LIMIT,
                             f"{len(session.calls)} requests to a provider that refused every one")
        self.assertEqual("failed", run.status)
        self.assertIn("refus", (run.error_message or "").lower())
        self.assertNotIn(LOGIN[1], run.error_message or "")


if __name__ == "__main__":
    unittest.main()
