"""A Live TV channel sync where EVERY enabled group fails to load is a failed
sync, not "0 new, 0 updated, 0 total".

The refusal guard (#149) turns a 401/403 into ProviderRefusedError only after
PROVIDER_REFUSAL_LIMIT refusals in a row. An install with one or two enabled
groups never reaches that count: each group's 403 is logged as a warning, the
loop ends with no channels, and the sync is reported as a success -- Activity
says "Live TV channel sync for <provider>: 0 new, 0 updated, 0 total", the
status phase is "complete" and the provider's last_live_sync moves forward,
while the provider is refusing every request. Found in an end-to-end outage
drill (provider answering 403 to everything, one enabled group).

The lineup itself is safe (Xtream channels are never deleted); what is wrong
is the report. Expected: the sync raises (status "error" with the reason),
nothing is upserted, and last_live_sync is left alone.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
import unittest.mock

import requests


class _Response:
    def __init__(self, status):
        self.status_code = status
        self.text = "Forbidden"

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: "
                             "http://panel.example/player_api.php?username=u&password=s3cret-pass&action=get_live_streams")

    def json(self):
        return []


class _Session:
    def __init__(self, status):
        self.status, self.calls, self.headers = status, [], {}

    def get(self, url, *a, **kw):
        self.calls.append(url)
        return _Response(self.status)

    def close(self):
        pass


class ChannelSyncEveryGroupFails(unittest.TestCase):
    def _run(self, n_groups, status=403):
        import routers.livetv as livetv
        from services.xtream_client import XtreamClient
        client = XtreamClient("http://panel.example", "u", "s3cret-pass")
        client.session = _Session(status)
        client.guard.gap = 0

        class _G:
            def __init__(self, cat):
                self.category_id, self.name = cat, f"G{cat}"

        class _Q:
            def filter(self, *a):
                return self

            def all(self):
                return [_G(str(i)) for i in range(n_groups)]

            def first(self):
                return None

        class _Db:
            def query(self, *a):
                return _Q()

            def commit(self):
                pass

        upserted, activity = [], []
        with unittest.mock.patch("services.xtream_client.XtreamClient", lambda **kw: client), \
                unittest.mock.patch.object(livetv, "_upsert_channels",
                                           lambda *a, **k: upserted.append(a[1]) or {"new": 0, "updated": 0, "total": 0}), \
                unittest.mock.patch.object(livetv, "_set_sync_status", lambda *a, **k: None), \
                unittest.mock.patch.object(livetv, "log_activity", lambda db, kind, msg: activity.append(msg)):
            raised = None
            try:
                livetv._sync_channels_from_xtream({"id": 1, "name": "P", "server_url": "http://panel.example",
                                                   "username": "u", "password": "s3cret-pass", "user_agent": "x"}, _Db())
            except Exception as e:      # noqa: BLE001 -- the assertion is that it fails at all
                raised = e
        return raised, upserted, activity

    def test_one_group_refused_is_a_failed_sync(self):
        raised, upserted, activity = self._run(1)
        self.assertIsNotNone(raised, f"reported as a success: {activity}")
        self.assertEqual([], upserted)

    def test_two_groups_refused_is_a_failed_sync(self):
        raised, upserted, activity = self._run(2)
        self.assertIsNotNone(raised, f"reported as a success: {activity}")
        self.assertEqual([], upserted)

    def test_the_reason_never_carries_the_password(self):
        raised, _, _ = self._run(1)
        self.assertIsNotNone(raised)
        self.assertNotIn("s3cret-pass", str(raised))
        self.assertIn("403", str(raised))

    def test_every_group_erroring_with_5xx_is_a_failed_sync(self):
        raised, upserted, activity = self._run(2, status=502)
        self.assertIsNotNone(raised, f"reported as a success: {activity}")
        self.assertEqual([], upserted)


if __name__ == "__main__":
    unittest.main()
