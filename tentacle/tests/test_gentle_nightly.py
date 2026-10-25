"""The nightly sync is gentle on the provider (#709 follow-up).

Run from the tentacle/ directory:  python -m unittest tests.test_gentle_nightly

A household's Xtream panel held the account back after the nightly burst: on
most nights of 1-14 Oct 2026, 14-16 of its 42 categories got HTTP 403, on
23 Oct categories 2-14 came back empty, and the guide download after the sync
was refused. Now category listings are spaced out, a refused or suddenly
empty listing is asked again after a pause (Retry-After honoured), discovery
asks for its whole-catalogue counts only when a category is new, the guide
download waits for a quiet spell, and the hourly provider check skips the
nightly run.
"""
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

import requests

from models.database import Movie
from nightly_harness import FakeTMDB, NightlyHarness
from services import sync
from services.exceptions import ProviderRefusedError, SyncCancelledError
from services.xtream_client import RequestGuard


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Cat:
    def __init__(self, title_count=100, name="EN - NEW RELEASE", cid="7"):
        self.title_count = title_count
        self.category_name = name
        self.category_id = cid


def _http_error(status, retry_after=None):
    resp = requests.Response()
    resp.status_code = status
    if retry_after is not None:
        resp.headers["Retry-After"] = str(retry_after)
    return requests.HTTPError(f"{status} Client Error", response=resp)


class Client:
    """A sync client with a real guard and pacer on a fake clock."""

    def __init__(self, answers, read_before=True):
        self.fc = FakeClock()
        self.pacer = sync.ListingPacer()
        self.pacer.clock, self.pacer.sleep = self.fc.clock, self.fc.sleep
        # Mid-run (the burst case): this run already read a listing
        self.pacer.read = 1 if read_before else 0
        self.guard = RequestGuard(gap=0)
        self.answers = list(answers)
        self.asked_at = []

    def fetch(self, category_id):
        self.asked_at.append(self.fc.now)
        a = self.answers.pop(0)
        if isinstance(a, int):
            self.guard.after(a)          # raises once the guard stops asking
            raise _http_error(a)
        if isinstance(a, Exception):
            self.guard.after(a.response.status_code)
            raise a
        self.guard.after(200)
        return a


class ReadCategory(unittest.TestCase):
    def test_listings_are_spaced_out(self):
        c = Client([[{"id": 1}], [{"id": 2}], [{"id": 3}]])
        for _ in range(3):
            sync._read_category(c, c.fetch, Cat())
            c.fc.now += 1.0      # the category's own processing counts towards the gap
        gaps = [b - a for a, b in zip(c.asked_at, c.asked_at[1:])]
        self.assertEqual(gaps, [sync.CATEGORY_GAP_SECONDS] * 2)

    def test_a_refused_listing_is_asked_again_after_a_pause(self):
        c = Client([403, [{"id": 1}]])
        with self.assertLogs("services.sync", "WARNING") as logs:
            out = sync._read_category(c, c.fetch, Cat())
        self.assertEqual(out, [{"id": 1}])
        self.assertEqual(c.pacer.retries, 1)
        self.assertEqual(c.asked_at[1] - c.asked_at[0], sync.CATEGORY_RETRY_WAITS[0])
        self.assertEqual(c.pacer.gap, sync.CATEGORY_GAP_SECONDS * 2)
        self.assertIn("answered HTTP 403", logs.output[0])
        self.assertIsNone(c.guard.refused_status)

    def test_refused_from_the_first_listing_is_not_asked_again(self):
        # Blocked before the run began (#149): no waiting, no second request
        c = Client([403, [{"id": 1}]], read_before=False)
        with self.assertRaises(requests.HTTPError):
            sync._read_category(c, c.fetch, Cat())
        self.assertEqual((len(c.asked_at), c.pacer.retries), (1, 0))

    def test_retry_after_is_honoured_and_capped(self):
        c = Client([_http_error(429, retry_after=30), _http_error(509, retry_after=99999), [{"id": 1}]])
        sync._read_category(c, c.fetch, Cat())
        self.assertEqual(c.asked_at[1] - c.asked_at[0], 30)
        self.assertEqual(c.asked_at[2] - c.asked_at[1], sync.RETRY_AFTER_MAX_SECONDS)

    def test_a_category_that_suddenly_answers_empty_is_asked_again(self):
        # 23 Oct: 13 categories in a row came back empty, then the panel recovered
        c = Client([[], [], [{"id": 1}]])
        out = sync._read_category(c, c.fetch, Cat(title_count=200))
        self.assertEqual(out, [{"id": 1}])
        self.assertEqual(c.pacer.retries, 2)
        self.assertEqual(c.pacer.waited, sum(sync.CATEGORY_RETRY_WAITS))

    def test_still_empty_after_the_retries_goes_on_as_before(self):
        c = Client([[], [], []])
        self.assertEqual(sync._read_category(c, c.fetch, Cat(title_count=200)), [])
        self.assertEqual(len(c.asked_at), 1 + len(sync.CATEGORY_RETRY_WAITS))

    def test_an_empty_category_that_never_held_titles_is_not_asked_again(self):
        c = Client([[]])
        self.assertEqual(sync._read_category(c, c.fetch, Cat(title_count=0)), [])
        self.assertEqual((len(c.asked_at), c.pacer.retries), (1, 0))

    def test_an_answer_that_will_not_pass_is_not_asked_again(self):
        c = Client([404])
        with self.assertRaises(requests.HTTPError):
            sync._read_category(c, c.fetch, Cat())
        self.assertEqual(len(c.asked_at), 1)

    def test_a_category_refused_for_good_counts_once(self):
        # A panel may restrict a category: asked three times, it is one refusal,
        # and the next category read resets the count as before.
        c = Client([403, 403, 403, [{"id": 1}]])
        with self.assertRaises(requests.HTTPError):
            sync._read_category(c, c.fetch, Cat())
        self.assertEqual(c.asked_at[-1] - c.asked_at[0], sum(sync.CATEGORY_RETRY_WAITS))
        self.assertEqual((c.guard.refusals, c.guard.refused_status), (1, None))
        self.assertEqual(sync._read_category(c, c.fetch, Cat(cid="8")), [{"id": 1}])
        self.assertEqual(c.guard.refusals, 0)

    def test_a_provider_that_refuses_everything_stops_the_run_as_before(self):
        # Three categories refused in a row: RequestGuard stops asking (#149)
        c = Client([403] * 20)
        c.pacer.budget = 10_000
        for cid in ("1", "2"):
            with self.assertRaises(requests.HTTPError):
                sync._read_category(c, c.fetch, Cat(cid=cid))
        with self.assertRaises(ProviderRefusedError):
            sync._read_category(c, c.fetch, Cat(cid="3"))
        self.assertEqual(c.guard.refused_status, 403)
        self.assertEqual(len(c.asked_at), 3 + 3 + 1)

    def test_the_waiting_is_capped_per_run(self):
        c = Client([403, [{"id": 1}]])
        c.pacer.budget = 0
        with self.assertRaises(requests.HTTPError):
            sync._read_category(c, c.fetch, Cat())
        self.assertEqual(len(c.asked_at), 1)

    def test_the_gap_grows_to_its_ceiling(self):
        c = Client([])
        for _ in range(10):
            c.pacer.back_off(0)
        self.assertEqual(c.pacer.gap, sync.CATEGORY_GAP_MAX_SECONDS)

    def test_a_cancel_stops_the_wait(self):
        c = Client([403, [{"id": 1}]])
        with self.assertRaises(SyncCancelledError):
            sync._read_category(c, c.fetch, Cat(), cancel_check=lambda: bool(c.asked_at))
        self.assertEqual(len(c.asked_at), 1)

    def test_a_client_without_a_pacer_is_read_once(self):
        # M3U: the playlist was read already; also a test's MagicMock client
        calls = []
        client = mock.MagicMock()
        out = sync._read_category(client, lambda cid: calls.append(cid) or [], Cat(title_count=50))
        self.assertEqual((out, calls), ([], ["7"]))

    def test_the_xtream_client_has_one(self):
        p = mock.MagicMock(server_url="http://panel.test", username="u", password="p")
        self.assertIsInstance(sync.XtreamClient(p).pacer, sync.ListingPacer)


class _Panel:
    """A scripted Xtream panel for sync.XtreamClient: per category id a list of
    answers (status, body), the last one repeating."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.asked = []
        self.headers = {}

    def get(self, url, timeout=None, **kw):
        cid = url.split("category_id=")[-1]
        self.asked.append(cid)
        answers = self.script[cid]
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        resp = requests.Response()
        resp.status_code = status
        resp._content = __import__("json").dumps(body).encode()
        resp.url = "http://panel.test/player_api.php"
        return resp

    def close(self):
        pass


def _films(cid, n=2):
    return [{"name": f"Film {cid}{i} (2010)", "stream_id": int(cid) * 100 + i, "container_extension": "mp4"}
            for i in range(n)]


class GentleSyncRun(NightlyHarness):
    """The whole VOD sync against a panel that holds back after a burst: one
    category refused once (403), one answered empty once; both are asked again
    and read, so the run reads every category."""

    def test_every_category_is_read_after_the_panel_recovers(self):
        for cid in ("1", "2", "3"):
            cat = self.add_category(cid, "movie")
            cat.title_count = 2
            for f in _films(cid):
                FakeTMDB.ids[f"Film {cid}{f['stream_id'] % 100}"] = 9000 + f["stream_id"]
        self.db.commit()
        panel = _Panel({"1": [(200, _films("1"))],     # read first: a refusal after it is the burst case
                        "2": [(403, []), (200, _films("2"))],
                        "3": [(200, []), (200, _films("3"))]})
        client = sync.XtreamClient(types.SimpleNamespace(id=self.provider.id, server_url="http://panel.test",
                                                         username="u", password="p"))
        client.session = panel
        client.guard.gap = 0
        sync.make_provider_client = lambda provider: client
        with self.assertLogs("services.sync", "INFO") as logs:
            run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual((run.status, run.error_message), ("completed", None))
        self.assertEqual(panel.asked, ["1", "2", "2", "3", "3"])
        self.assertEqual(client.pacer.retries, 2)
        self.assertEqual(self.db.query(Movie).count(), 6)
        self.assertTrue(any("2 asked again" in line for line in logs.output), logs.output[-3:])


class GentleDiscovery(unittest.TestCase):
    def _run(self, existing, gentle=True):
        from services import discovery
        cats = ([{"category_id": "1", "category_name": "Films"}],
                [{"category_id": "2", "category_name": "Shows"}])
        calls = []

        def fetch_cats(provider, read=None, counts=True, gap=0.0):
            calls.append(("categories", counts, gap))
            return cats[0], cats[1], ({"1": 11} if counts else {}), ({"2": 22} if counts else {})

        def fetch_counts(provider, session=None, gap=0.0):
            calls.append(("counts", gap))
            return {"1": 11}, {"2": 22}

        rows = [mock.MagicMock(category_id=cid, type=t) for cid, t in existing]
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.all.return_value = rows
        provider = mock.MagicMock(provider_type="xtream", id=1)
        provider.name = "P"
        with mock.patch("routers.providers.fetch_provider_categories", fetch_cats), \
                mock.patch("routers.providers.fetch_category_counts", fetch_counts), \
                mock.patch("services.provider_new.note_created"), \
                mock.patch("services.xtream_client.pause") as slept:
            new = discovery._refresh_vod_categories(db, provider, gentle)
        added = {c.args[0].category_id: c.args[0].title_count for c in db.add.call_args_list}
        return new, calls, added, slept

    def test_no_new_category_no_whole_catalogue_listings(self):
        new, calls, added, slept = self._run([("1", "movie"), ("2", "series")])
        self.assertEqual(new, [])
        self.assertEqual(calls, [("categories", False, discovery_gap())])
        slept.assert_not_called()

    def test_a_new_category_is_counted(self):
        new, calls, added, slept = self._run([("1", "movie")])
        self.assertEqual(new, ["Shows"])
        self.assertEqual(calls, [("categories", False, discovery_gap()), ("counts", discovery_gap())])
        self.assertEqual(added, {"2": 22})
        slept.assert_called_once_with(discovery_gap())

    def test_not_gently_as_before(self):
        new, calls, added, slept = self._run([("1", "movie")], gentle=False)
        self.assertEqual(calls, [("categories", True, 0.0)])
        self.assertEqual(added, {"2": 22})


def discovery_gap():
    from services import discovery
    return discovery.DISCOVERY_GAP_SECONDS


class QuietBeforeGuide(unittest.TestCase):
    def test_waits_out_the_quiet_spell_after_the_runs_last_request(self):
        import main
        slept = []
        with mock.patch("services.xtream_client.seconds_since_api_request", return_value=20.0):
            waited = main._quiet_before_guide(datetime.utcnow() - timedelta(hours=1), sleep=slept.append)
        self.assertEqual(waited, main.GUIDE_QUIET_SECONDS - 20.0)
        self.assertEqual(slept, [main.GUIDE_QUIET_SECONDS - 20.0])

    def test_no_wait_when_quiet_or_when_the_request_came_before_the_run(self):
        import main
        slept = []
        with mock.patch("services.xtream_client.seconds_since_api_request", return_value=main.GUIDE_QUIET_SECONDS + 1):
            self.assertEqual(main._quiet_before_guide(datetime.utcnow() - timedelta(hours=1), sleep=slept.append), 0)
        with mock.patch("services.xtream_client.seconds_since_api_request", return_value=None):
            self.assertEqual(main._quiet_before_guide(datetime.utcnow() - timedelta(hours=1), sleep=slept.append), 0)
        with mock.patch("services.xtream_client.seconds_since_api_request", return_value=30.0):
            self.assertEqual(main._quiet_before_guide(datetime.utcnow() - timedelta(seconds=5), sleep=slept.append), 0)
        self.assertEqual(slept, [])

    def test_the_request_guard_notes_each_request(self):
        from services import xtream_client
        g = RequestGuard(gap=0)
        with mock.patch.object(xtream_client.time, "monotonic", return_value=5000.0):
            g.before()
        with mock.patch.object(xtream_client.time, "monotonic", return_value=5007.5):
            self.assertEqual(xtream_client.seconds_since_api_request(), 7.5)


class ProviderHealthSkipsTheNightlyRun(unittest.TestCase):
    def test_skipped_while_the_nightly_sync_runs(self):
        from routers import sync as sync_router
        from services import provider_health
        self.assertTrue(sync_router.begin_nightly_run("test"))
        try:
            with mock.patch.object(provider_health, "check_providers") as check:
                self.assertFalse(provider_health.run_provider_health())
            check.assert_not_called()
        finally:
            sync_router.end_nightly_run()
        self.assertFalse(sync_router.nightly_running())


if __name__ == "__main__":
    unittest.main()
