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
import unittest.mock

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

    def close(self):
        pass


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


class TheSyncIsPaced(unittest.TestCase):
    """At most 4 API requests a second on one client: both blocks started
    while a sync hit the panel at about 7 a second for minutes."""

    def test_requests_keep_a_minimum_gap(self):
        from services.xtream_client import XTREAM_MIN_REQUEST_GAP
        now = [100.0]
        sent_at = []

        class _Timed(_Session):
            def get(self, url, timeout=None, **kw):
                sent_at.append(now[0])
                now[0] += 0.01          # the panel answers fast
                return super().get(url, timeout, **kw)
        client = _client(_Timed(200))
        client.guard.clock = lambda: now[0]
        client.guard.sleep = lambda s: now.__setitem__(0, now[0] + s)
        for i in range(5):
            client.get_vod_streams(str(i))
            client.get_series_info(i)
        gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
        self.assertEqual(10, len(sent_at))
        self.assertTrue(all(g >= XTREAM_MIN_REQUEST_GAP - 1e-9 for g in gaps), gaps)


class TheLiveTvClientStopsToo(unittest.TestCase):
    """The Live TV channel and group syncs use services.xtream_client: the
    same stop, and a refused channel fetch ends the lineup fetch instead of
    returning a short one."""

    def _client(self, *statuses):
        from services.xtream_client import XtreamClient
        c = XtreamClient("http://panel.example", *LOGIN)
        c.session = _Session(*statuses)
        c.guard.gap = 0
        return c

    def test_a_few_refusals_then_no_more_requests(self):
        from services.exceptions import ProviderRefusedError
        client = self._client(403)
        with self.assertRaises(ProviderRefusedError):
            client.get_all_live_streams_by_category([str(i) for i in range(20)], delay=0)
        self.assertEqual(REFUSAL_LIMIT, len(client.session.calls))

    def test_the_channel_sync_fails_and_keeps_its_lineup(self):
        import routers.livetv as livetv
        from services.exceptions import ProviderRefusedError
        client = self._client(403)

        class _Groups:
            def __init__(self, cat):
                self.category_id, self.name = cat, f"G{cat}"

        class _Query:
            def filter(self, *a):
                return self

            def all(self):
                return [_Groups(str(i)) for i in range(10)]

        class _Db:
            def query(self, *a):
                return _Query()

        upserted = []
        with unittest.mock.patch("services.xtream_client.XtreamClient", lambda **kw: client), \
                unittest.mock.patch.object(livetv, "_upsert_channels", lambda *a, **k: upserted.append(a)), \
                unittest.mock.patch.object(livetv, "_set_sync_status", lambda *a, **k: None):
            with self.assertRaises(ProviderRefusedError):
                livetv._sync_channels_from_xtream({"id": 1, "name": "P", "server_url": "http://panel.example",
                                                   "username": LOGIN[0], "password": LOGIN[1],
                                                   "user_agent": "x"}, _Db())
        self.assertEqual(REFUSAL_LIMIT, len(client.session.calls))
        self.assertEqual([], upserted)


if __name__ == "__main__":
    unittest.main()
