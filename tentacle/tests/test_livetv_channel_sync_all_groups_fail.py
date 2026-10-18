"""A Live TV channel sync where EVERY enabled group fails to load is a failed
sync, not "0 new, 0 updated, 0 total" (#630, reported by therobmilne).

The refusal guard (#149) turns a 401/403 into ProviderRefusedError only after
PROVIDER_REFUSAL_LIMIT refusals in a row. An install with one or two enabled
groups never reaches that count, and 5xx answers never count at all: each
group's error is logged as a warning, the loop ends with no channels, and the
sync is reported as a success. Activity says "Live TV channel sync for
<provider>: 0 new, 0 updated, 0 total", the status phase is "complete" and
the provider's last_live_sync moves forward while the provider refuses
every request.

The lineup itself is safe (Xtream channels are never deleted); the report is
wrong. Expected: status "error" with the reason (no login in it), no success
in Activity, last_live_sync left alone. A sync where some groups load, or a
group that is simply empty, is still a success.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
import unittest.mock
from datetime import datetime

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ActivityLog, LiveChannel, LiveChannelGroup, Provider
import routers.livetv as livetv
from tmp_dirs import temp_dir

LOGIN = ("u", "s3cret-pass")
BEFORE = datetime(2026, 1, 1, 12, 0, 0)


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = "Forbidden" if status >= 400 else "[]"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: "
                                     f"http://panel.example/player_api.php?username={LOGIN[0]}"
                                     f"&password={LOGIN[1]}&action=get_live_streams", response=self)

    def json(self):
        return self._body


class _Session:
    """answers: category_id -> HTTP status (200 serves one channel, or none
    for an id in `empty`). Account info (no action) answers 200 {}."""

    def __init__(self, answers, empty=()):
        self.answers, self.empty, self.calls, self.headers = answers, set(empty), [], {}

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        if "action=" not in url:
            return _Response(200, {})
        cat = url.rsplit("category_id=", 1)[-1]
        status = self.answers[cat]
        body = [] if cat in self.empty else [{"stream_id": int(cat) + 100, "name": f"Channel {cat}",
                                              "category_id": cat, "epg_channel_id": ""}]
        return _Response(status, body if status < 400 else [])

    def close(self):
        pass


class ChannelSyncEveryGroupFails(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        provider = Provider(name="P", server_url="http://panel.example", username=LOGIN[0],
                            password=LOGIN[1], active=True, live_tv_enabled=True, last_live_sync=BEFORE)
        db.add(provider)
        db.commit()
        self.pid = provider.id
        db.close()

    def _sync(self, answers, empty=()):
        from services.xtream_client import XtreamClient
        db = self.Session()
        for cat in answers:
            db.add(LiveChannelGroup(provider_id=self.pid, name=f"G{cat}", category_id=cat, enabled=True))
        db.commit()
        db.close()
        client = XtreamClient("http://panel.example", *LOGIN)
        client.session = _Session(answers, empty)
        client.guard.gap = 0
        livetv._sync_status.pop(self.pid, None)
        with unittest.mock.patch("services.xtream_client.XtreamClient", lambda **kw: client), \
                unittest.mock.patch.object(livetv, "SessionLocal", self.Session), \
                unittest.mock.patch.object(livetv, "_run_epg_sync_background", lambda data: None):
            livetv._run_channel_sync_background({
                "id": self.pid, "name": "P", "provider_type": "xtream",
                "server_url": "http://panel.example", "username": LOGIN[0], "password": LOGIN[1],
                "user_agent": "x", "m3u_url": None, "epg_url": None,
            })
        status = livetv._get_sync_status(self.pid)
        db = self.Session()
        activity = [a.message for a in db.query(ActivityLog).all()]
        last = db.query(Provider).get(self.pid).last_live_sync
        channels = db.query(LiveChannel).filter(LiveChannel.provider_id == self.pid).count()
        db.close()
        return status, activity, last, channels

    def _assert_failed(self, status, activity, last, code):
        self.assertEqual("error", status.get("phase"), f"reported as a success: {status} / {activity}")
        self.assertIn(code, status.get("message", ""))
        self.assertNotIn(LOGIN[1], status.get("message", ""))
        self.assertFalse([m for m in activity if "0 new, 0 updated" in m], activity)
        self.assertEqual(BEFORE, last, "last_live_sync moved on a failed sync")

    def test_one_group_refused_is_a_failed_sync(self):
        status, activity, last, channels = self._sync({"1": 403})
        self._assert_failed(status, activity, last, "403")
        self.assertEqual(0, channels)

    def test_two_groups_refused_is_a_failed_sync(self):
        status, activity, last, channels = self._sync({"1": 403, "2": 403})
        self._assert_failed(status, activity, last, "403")
        self.assertEqual(0, channels)

    def test_every_group_erroring_with_5xx_is_a_failed_sync(self):
        status, activity, last, channels = self._sync({"1": 502, "2": 502, "3": 502, "4": 502})
        self._assert_failed(status, activity, last, "502")
        self.assertEqual(0, channels)

    def test_some_groups_loading_is_still_a_success(self):
        status, activity, last, channels = self._sync({"1": 403, "2": 200})
        self.assertNotEqual("error", status.get("phase"), status)
        self.assertEqual(1, channels)
        self.assertNotEqual(BEFORE, last)

    def test_an_empty_group_is_not_a_failure(self):
        status, activity, last, channels = self._sync({"1": 200}, empty={"1"})
        self.assertEqual("complete", status.get("phase"), status)
        self.assertEqual(0, channels)
        self.assertNotEqual(BEFORE, last)


if __name__ == "__main__":
    unittest.main()
