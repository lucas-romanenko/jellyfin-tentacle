"""The hourly provider health check: log in to the provider's API, open one
short test stream, and say so (dashboard banner, one Pushover message, one
recovery message) when either fails.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The provider started answering 403 (streams first, then its API) and
nothing in Tentacle said so. requests is faked by URL; the 2-minute
confirmation wait is injected, so nothing sleeps.
"""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, LiveChannel, Movie, Provider, Setting, TentacleUser,
                             get_db, get_setting)
from services import provider_health as ph
from tmp_dirs import temp_dir

SERVER = "http://iptv.example.test:8080"
USER, PASSWORD = "acct-user-77", "s3cr3t-pass-91"
API = f"{SERVER}/player_api.php"
MOVIE_URLS = [f"{SERVER}/movie/{USER}/{PASSWORD}/{n}.mp4" for n in (101, 102, 103)]
LIVE_URL = f"{SERVER}/live/{USER}/{PASSWORD}/555.ts"


class FakeResponse:
    def __init__(self, status=200, body=None, data=b"x" * 1024):
        self.status_code = status
        self._body = body
        self._data = data

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")

    def iter_content(self, size=1):
        if self._data:
            yield self._data[:size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def api_ok(**user_info):
    info = {"auth": 1, "status": "Active", "exp_date": str(int((datetime.now() + timedelta(days=30)).timestamp()))}
    info.update(user_info)
    return FakeResponse(200, {"user_info": info, "server_info": {}})


class Net:
    """Answers by URL prefix; a list of answers is used one per call (the last repeats)."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                answer = self.routes[prefix]
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected request {url}")


class Base_(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.dir}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.provider = Provider(name="Acme IPTV", provider_type="xtream", server_url=SERVER,
                                 username=USER, password=PASSWORD, active=True)
        self.db.add(self.provider)
        self.db.add_all([Setting(key="pushover_app_token", value="apptoken123456"),
                         Setting(key="pushover_user_key", value="userkey123456"),
                         Setting(key="external_url", value="https://tentacle.example.test")])
        self.db.commit()
        for n, url in enumerate(MOVIE_URLS):
            strm = self.dir / f"m{n}.strm"
            strm.write_text(url, encoding="utf-8")
            self.db.add(Movie(tmdb_id=1000 + n, title=f"Film {n}", source=f"provider_{self.provider.id}",
                              provider_id=self.provider.id, strm_path=str(strm)))
        self.db.commit()
        self.sent = []
        self.pushover_ok = True
        patches = [
            mock.patch("services.pushover.requests.post", side_effect=self._pushover),
            mock.patch("services.provider_activity.live_streams_active", return_value=False),
            mock.patch("services.provider_activity.recording_protected", return_value=False),
            mock.patch("services.provider_health.PROBE_PAUSE_SECONDS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = [datetime(2026, 10, 8, 7, 50).astimezone()]
        clock = mock.patch("services.provider_health._now", side_effect=lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.waits = []

    def _pushover(self, url, data=None, timeout=None, **kw):
        self.sent.append(dict(data))
        return FakeResponse(200 if self.pushover_ok else 500, {"status": 1} if self.pushover_ok else {"errors": ["down"]})

    def run_check(self, routes, confirm=True):
        net = Net(routes)

        def wait(seconds):
            self.waits.append(seconds)
            self.clock[0] += timedelta(seconds=seconds)
        with mock.patch("services.provider_health.requests.get", side_effect=net.get):
            result = ph.check_providers(self.db, confirm_delay=120 if confirm else None, sleep=wait)
        self.net = net
        return result

    def state(self):
        return ph.load_state(self.db)[str(self.provider.id)]


class TheChecks(Base_):
    def test_api_403_is_failing_with_the_code_and_the_local_time(self):
        self.run_check({API: FakeResponse(403, data=b"")})
        s = self.state()
        self.assertEqual(s["state"], "failing")
        self.assertEqual((s["step"], s["code"]), ("api", 403))
        self.assertEqual(s["reason"], "Provider refusing: 403 since 07:50. Likely IP block, expired account or moved URL")
        self.assertEqual(self.waits, [120], "confirmed by a second check 2 minutes later")
        # The API failed: no test stream was opened.
        self.assertFalse([u for u in self.net.calls if "/movie/" in u])

    def test_each_api_answer_gives_its_reason(self):
        past = str(int(datetime(2026, 10, 1, 12).timestamp()))
        cases = [
            (api_ok(auth=0), "Provider rejects the login since 07:50. Check the username and password; "
                             "the account may have expired"),
            (FakeResponse(401), "Provider rejects the login since 07:50"),
            (FakeResponse(200, None), "Provider answers with a web page instead of its API since 07:50"),
            (api_ok(status="Expired", exp_date=past), "Provider account expired on 2026-10-01"),
            (api_ok(exp_date=past), "Provider account expired on 2026-10-01"),
            (api_ok(status="Banned"), "Provider account is Banned"),
            (FakeResponse(404), "Provider address not found (404) since 07:50"),
            (FakeResponse(502), "Provider server error (HTTP 502) since 07:50. Usually on their side"),
            (requests.ConnectTimeout("timed out"), "Provider unreachable since 07:50 (no answer)"),
            (requests.ConnectionError("[Errno 111] Connection refused"),
             "Provider unreachable since 07:50 (connection refused)"),
            (requests.ConnectionError("Failed to resolve 'iptv.example.test' (Name or service not known)"),
             "Provider unreachable since 07:50 (name not found)"),
        ]
        for answer, expected in cases:
            with self.subTest(expected=expected):
                self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
                self.db.commit()
                self.clock[0] = datetime(2026, 10, 8, 7, 50).astimezone()
                self.run_check({API: answer})
                s = self.state()
                self.assertEqual(s["state"], "failing")
                self.assertTrue(s["reason"].startswith(expected), s["reason"])

    def test_streams_refused_while_the_login_works(self):
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(403, data=b"")})
        s = self.state()
        self.assertEqual((s["state"], s["step"], s["code"]), ("failing", "stream", 403))
        self.assertEqual(s["reason"], "Provider refusing streams: 403 since 07:50 (the login still works). "
                                      "Likely IP block or the account over its connection limit")

    def test_a_gone_title_tries_the_next_one(self):
        self.run_check({API: api_ok(), MOVIE_URLS[0]: FakeResponse(404, data=b""),
                        MOVIE_URLS[1]: FakeResponse(206)})
        self.assertEqual(self.state()["state"], "ok")
        self.assertEqual(self.sent, [])

    def test_every_candidate_gone_is_failing(self):
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(404, data=b""),
                        SERVER + "/live/": FakeResponse(410, data=b"")})
        s = self.state()
        self.assertEqual(s["state"], "failing")
        self.assertIn("test streams not found", s["reason"])

    def test_live_channel_is_tried_after_the_films(self):
        self.db.add(LiveChannel(provider_id=self.provider.id, name="News", stream_id="555",
                                stream_url=LIVE_URL, enabled=True))
        self.db.commit()
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(404, data=b""), LIVE_URL: FakeResponse(200)})
        self.assertEqual(self.state()["state"], "ok")
        self.assertEqual(self.net.calls[-1], LIVE_URL)

    def test_provider_busy_gives_no_verdict(self):
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(509, data=b"")})
        s = self.state()
        self.assertEqual(s["state"], "unknown")
        self.assertIsNotNone(s["skipped_at"])
        self.assertEqual(self.sent, [])

    def test_a_blip_that_does_not_repeat_raises_nothing(self):
        self.run_check({API: [FakeResponse(403), api_ok()], SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(self.state()["state"], "ok")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.waits, [120])

    def test_check_now_needs_no_confirmation(self):
        self.run_check({API: FakeResponse(403)}, confirm=False)
        self.assertEqual(self.state()["state"], "failing")
        self.assertEqual(self.waits, [])

    def test_m3u_file_provider_only_opens_a_stream(self):
        self.provider.provider_type = "m3u_file"
        self.provider.m3u_url = str(self.dir / "list.m3u")
        self.db.commit()
        self.run_check({SERVER + "/movie/": FakeResponse(403, data=b"")})
        s = self.state()
        self.assertEqual(s["state"], "failing")
        self.assertNotIn("login still works", s["reason"])
        self.assertFalse([u for u in self.net.calls if "player_api" in u])

    def test_m3u_url_must_be_a_playlist(self):
        self.provider.provider_type = "m3u_url"
        self.provider.m3u_url = "http://lists.example.test/get.php?username=u&password=p"
        self.db.commit()
        self.run_check({"http://lists.example.test/": FakeResponse(200, data=b"<html>blocked</html>")})
        self.assertIn("other than its M3U playlist", self.state()["reason"])
        self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
        self.db.commit()
        self.run_check({"http://lists.example.test/": FakeResponse(200, data=b"\xef\xbb\xbf#EXTM3U\n#EXTINF:-1,x\n"),
                        SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(self.state()["state"], "ok")


class Messages(Base_):
    def test_one_message_down_none_while_down_one_on_recovery(self):
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["title"], "Tentacle: Acme IPTV is failing")
        self.assertTrue(self.sent[0]["message"].startswith("Provider refusing: 403 since 07:50"))
        self.assertEqual(self.sent[0]["url"], "https://tentacle.example.test")
        self.assertEqual((self.sent[0]["token"], self.sent[0]["user"]), ("apptoken123456", "userkey123456"))

        self.clock[0] += timedelta(hours=1)
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(len(self.sent), 1, "nothing on the hours in between")
        self.assertEqual(self.waits, [120], "an outage already confirmed is not re-confirmed")
        self.assertTrue(self.state()["since"].startswith("2026-10-08T07:50"))

        self.clock[0] = datetime(2026, 10, 8, 11, 0).astimezone()
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1]["title"], "Tentacle: Acme IPTV works again")
        self.assertEqual(self.sent[1]["message"], "Down since 07:50 (3 h 10 min).")
        self.assertEqual(self.state()["state"], "ok")

        self.clock[0] += timedelta(hours=1)
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(len(self.sent), 2)

    def test_pushover_not_set_up_sends_nothing_and_raises_nothing(self):
        self.db.query(Setting).filter(Setting.key.like("pushover_%")).delete(synchronize_session=False)
        self.db.commit()
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state()["state"], "failing")
        # Recovered before any message went out: nothing to take back.
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_tried_again_next_run(self):
        self.pushover_ok = False
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.state()["alerted"])
        self.pushover_ok = True
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.state()["alerted"])
        self.run_check({API: FakeResponse(403)})
        self.assertEqual(len(self.sent), 2)

    def test_a_failed_recovery_send_is_tried_again(self):
        self.run_check({API: FakeResponse(403)})
        self.pushover_ok = False
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(len(self.sent), 2)
        self.pushover_ok = True
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[2]["title"], "Tentacle: Acme IPTV works again")
        self.assertIsNone(self.state()["recovery_due"])


class StandsAside(Base_):
    def test_live_tv_or_a_protected_recording_checks_nothing(self):
        self.run_check({API: FakeResponse(403)})
        before = self.state()
        for target in ("services.provider_activity.live_streams_active",
                       "services.provider_activity.recording_protected"):
            with self.subTest(target=target), mock.patch(target, return_value=True):
                result = self.run_check({})
                self.assertTrue(result["skipped"])
                self.assertEqual(self.net.calls, [])
                s = self.state()
                self.assertEqual((s["state"], s["since"], s["reason"]),
                                 (before["state"], before["since"], before["reason"]))
                self.assertIsNotNone(s["skipped_at"])

    def test_turned_off_provider_is_dropped(self):
        self.run_check({API: FakeResponse(403)})
        self.provider.active = False
        self.db.commit()
        self.run_check({})
        self.assertEqual(ph.load_state(self.db), {})


class NoSecrets(Base_):
    def test_reasons_messages_and_logs_carry_no_url_or_login(self):
        answers = [FakeResponse(403), api_ok(auth=0), FakeResponse(200, None), FakeResponse(502),
                   requests.ConnectionError(f"HTTPConnectionPool(host='iptv.example.test'): Max retries "
                                            f"exceeded with url: /player_api.php?username={USER}&password={PASSWORD} "
                                            f"(Caused by NewConnectionError: Connection refused)")]
        with self.assertLogs("services.provider_health", level="INFO") as logs:
            for answer in answers:
                self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
                self.db.commit()
                self.run_check({API: answer})
            self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
            self.db.commit()
            self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(403)})
        stored = get_setting(self.db, ph.SETTING_KEY)
        text = json.dumps(self.sent) + stored + "\n".join(logs.output)
        for secret in (USER, PASSWORD, "iptv.example.test"):
            self.assertNotIn(secret, text)


class Views(Base_):
    def test_public_status_is_200_ok_then_503_failing_without_names(self):
        self.assertEqual(ph.public_status(self.db), (200, {"status": "ok", "checked_at": None}))
        self.run_check({API: FakeResponse(403)})
        code, body = ph.public_status(self.db)
        self.assertEqual(code, 503)
        self.assertEqual(body["status"], "failing")
        self.assertEqual(body["problems"], ["Provider refusing: 403 since 07:50. Likely IP block, "
                                            "expired account or moved URL"])
        self.assertTrue(body["since"].startswith("2026-10-08T07:50"))
        text = json.dumps(body)
        for secret in ("Acme", USER, PASSWORD, "iptv.example.test", "server_url"):
            self.assertNotIn(secret, text)

    def test_provider_status_route_is_served_by_main(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/provider-status")', src)
        self.assertIn("public_status(db)", src)


class Routes(Base_):
    def setUp(self):
        super().setUp()
        self.db.add(Setting(key="session_secret", value="s3cret"))
        admin = TentacleUser(jellyfin_user_id="a1", display_name="Admin", is_admin=True)
        viewer = TentacleUser(jellyfin_user_id="v1", display_name="Viewer", is_admin=False)
        self.db.add_all([admin, viewer])
        self.db.commit()
        self.admin_id, self.viewer_id = admin.id, viewer.id
        from routers import health, settings as settings_router
        app = FastAPI()
        app.include_router(health.router)
        app.include_router(settings_router.router)

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app)

    def sign_in(self, user_id):
        from routers import auth
        self.client.cookies.set(auth.COOKIE_NAME, auth._sign_session(user_id, "s3cret"))

    def test_non_admin_and_anonymous_are_refused(self):
        for who in (None, self.viewer_id):
            if who:
                self.sign_in(who)
            with self.subTest(who=who):
                self.assertIn(self.client.get("/api/health/providers").status_code, (401, 403))
                with mock.patch("services.provider_health.requests.get") as get:
                    self.assertIn(self.client.post("/api/health/providers/check").status_code, (401, 403))
                get.assert_not_called()

    def test_admin_sees_the_state_and_can_check_now(self):
        self.sign_in(self.admin_id)
        net = Net({API: FakeResponse(403)})
        with mock.patch("services.provider_health.requests.get", side_effect=net.get):
            r = self.client.post("/api/health/providers/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["failing"], 1)
        listed = self.client.get("/api/health/providers").json()
        self.assertEqual(listed["providers"][0]["name"], "Acme IPTV")
        self.assertEqual(listed["providers"][0]["state"], "failing")
        self.assertTrue(listed["pushover"])
        self.assertNotIn(PASSWORD, json.dumps(listed))

    def test_check_now_refused_while_a_protected_recording_runs(self):
        self.sign_in(self.admin_id)
        with mock.patch("services.provider_activity.recording_protected", return_value=True), \
                mock.patch("services.provider_health.requests.get") as get:
            r = self.client.post("/api/health/providers/check")
        self.assertEqual(r.status_code, 503)
        get.assert_not_called()

    def test_pushover_keys_come_back_masked(self):
        self.sign_in(self.admin_id)
        shown = self.client.get("/api/settings").json()
        for key, value in (("pushover_app_token", "apptoken123456"), ("pushover_user_key", "userkey123456")):
            self.assertNotEqual(shown[key], value)
            self.assertTrue(shown[key].startswith("••••"))

    def test_send_test_uses_the_saved_keys_for_a_masked_field(self):
        self.sign_in(self.admin_id)
        r = self.client.post("/api/settings/test-pushover", json={"app_token": "••••3456", "user_key": "typed-user-key"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.sent[-1]["token"], self.sent[-1]["user"]), ("apptoken123456", "typed-user-key"))
        self.pushover_ok = False
        r = self.client.post("/api/settings/test-pushover", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Pushover refused the message", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
