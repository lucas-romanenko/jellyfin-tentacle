"""Provider health also learns from the syncs, not only from its own hourly
check (#652).

The hourly check (services/provider_health.py) was the only thing that
changed a provider's health, and it checks nothing at all while live TV or a
protected recording runs. In an outage drill (a provider answering 403 to
everything for 30 minutes):

- during a recording, the VOD sync ended "could not be read ... HTTP 403"
  and the channel sync got 403, yet /api/provider-status said "ok" all along;
- after the block ended, a VOD sync completed cleanly, and the dashboard,
  Pushover and /api/provider-status still said "failing" until someone
  pressed "Check now".

What a sync learns costs no connection, so it counts: a sync the provider
refused is a refusal (confirmed, like the check's own, by a second one at
least RETRY_DELAY_SECONDS later), and a sync that completed clears a failing
login ("api" step) and sends the recovery message.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import types
import unittest.mock
from datetime import datetime, timedelta

import requests

from models.database import Setting
from nightly_harness import NightlyHarness
from services import provider_health as ph
import services.sync as sync


class _Response:
    def __init__(self, status):
        self.status_code = status
        self.text = "Forbidden"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden", response=self)

    def json(self):
        return []


class _RefusingSession:
    """A provider that answers 403 to everything (an IP block)."""

    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, url, timeout=None, **kw):
        self.calls += 1
        return _Response(403)

    def close(self):
        pass


class SyncsFeedProviderHealth(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1", "movie")
        self.catalogue_movies("1", ["Some Film"])
        self.clock = [datetime(2026, 10, 8, 20, 0).astimezone()]
        for p in (unittest.mock.patch("services.provider_health._now", side_effect=lambda: self.clock[0]),
                  unittest.mock.patch("services.pushover.requests.post", side_effect=self._pushover)):
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        self.db.add_all([Setting(key="pushover_app_token", value="apptoken123456"),
                         Setting(key="pushover_user_key", value="userkey123456")])
        self.db.commit()

    def _pushover(self, url, data=None, timeout=None, **kw):
        self.sent.append(dict(data))
        return types.SimpleNamespace(status_code=200, json=lambda: {"status": 1})

    def health(self):
        self.db.expire_all()
        return ph.load_state(self.db).get(str(self.provider.id)) or {}

    def set_failing(self, since):
        entry = {**ph._unknown(), "name": self.provider.name, "state": "failing",
                 "since": since.isoformat(), "checked_at": since.isoformat(),
                 "step": "api", "code": 403, "kind": "http", "alerted": True,
                 "reason": "Provider refusing: 403 since 19:00. Likely IP block, "
                           "expired account or moved URL"}
        self.db.add(Setting(key=ph.SETTING_KEY, value=json.dumps({str(self.provider.id): entry})))
        self.db.commit()

    def refused_sync(self):
        client = sync.XtreamClient(types.SimpleNamespace(
            id=self.provider.id, server_url="http://provider", username="u", password="p"))
        client.session = _RefusingSession()
        client.guard.sleep = lambda s: None
        client.provider_id = self.provider.id
        sync.make_provider_client = lambda provider: client
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("failed", run.status, "the drill's sync: refused, nothing read")
        return run

    def clean_sync(self):
        sync.make_provider_client = lambda provider: self.client
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("completed", run.status, run.error_message)
        return run

    def test_a_clean_sync_clears_a_failing_login_and_says_so_once(self):
        self.set_failing(self.clock[0] - timedelta(hours=1))
        self.assertEqual(503, ph.public_status(self.db)[0])

        self.clean_sync()

        self.assertEqual("ok", self.health().get("state"),
                         "the provider answered the whole sync, yet health still says failing")
        self.assertEqual(200, ph.public_status(self.db)[0])
        self.assertEqual(1, len(self.sent), "one recovery message")
        self.assertIn("works again", self.sent[0]["title"])

        self.clean_sync()
        self.assertEqual(1, len(self.sent), "nothing more on the next good sync")

    def test_two_refused_syncs_two_minutes_apart_mark_it_failing_while_the_check_stands_aside(self):
        with unittest.mock.patch("services.provider_activity.live_streams_active", return_value=True):
            result = ph.check_providers(self.db, confirm_delay=None)
        self.assertTrue(result["skipped"], "the hourly check stands aside for live TV, as before")

        self.refused_sync()
        self.assertNotEqual("failing", self.health().get("state"),
                            "one refusal is not confirmed yet (a blip raises nothing)")
        self.assertEqual([], self.sent)

        self.clock[0] += timedelta(seconds=ph.RETRY_DELAY_SECONDS + 1)
        self.refused_sync()

        h = self.health()
        self.assertEqual("failing", h.get("state"),
                         "the provider refused two syncs, yet /api/provider-status says ok")
        self.assertEqual(403, h.get("code"))
        self.assertEqual(503, ph.public_status(self.db)[0])
        self.assertEqual(1, len(self.sent), "one 'is failing' message")
        self.assertIn("failing", self.sent[0]["title"])

    def test_two_refusals_seconds_apart_are_one_blip(self):
        self.refused_sync()
        self.clock[0] += timedelta(seconds=30)
        self.refused_sync()
        self.assertNotEqual("failing", self.health().get("state"))
        self.assertEqual([], self.sent)

    def test_a_clean_sync_between_two_refusals_starts_the_count_again(self):
        self.refused_sync()
        self.clock[0] += timedelta(seconds=60)
        self.clean_sync()
        self.clock[0] += timedelta(seconds=ph.RETRY_DELAY_SECONDS + 1)
        self.refused_sync()
        self.assertNotEqual("failing", self.health().get("state"))
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
