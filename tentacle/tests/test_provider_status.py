"""What's wrong with Live TV, in plain words (services/provider_status.py).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Lucas, 25 Oct 2026, the night IPTV Bears went down: "on tentacle should
clearly show provider server is down. And if we did get blocked it should
show that. And it would be nice to show it on tv somewhere too". That night
the panel first said "Database Error" (502/504 on every channel), then its
Cloudflare addresses answered 521 and the direct ones refused the
connection, and Tentacle said nothing: the check's confirmation stood aside
because someone was watching. The answers here are that night's.
"""
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import requests

from models.database import LiveChannel, ProviderAddress, Setting
from services import admin_alerts, provider_health as ph, provider_status as ps
from test_provider_health import API, SERVER, Base_, FakeResponse, Net, api_ok

UA = "TiviMate/4.7.0 (Linux; Android 12)"
CF_TWIN = "http://cf.example.test"
ROOT = SERVER + "/"


class Page(FakeResponse):
    """A web answer: headers and text as well."""

    def __init__(self, status, text="", cloudflare=False):
        super().__init__(status, None, data=text.encode())
        self.headers = {"Server": "cloudflare", "CF-RAY": "8d2f-YYZ"} if cloudflare else {"Server": "nginx"}
        self.text = text

    def iter_content(self, size=1):
        if self._data:
            yield self._data


def cf_521():
    return Page(521, "error code: 521", cloudflare=True)


class Reach(unittest.TestCase):
    def reach(self, answer):
        seen = []

        def get(url, **kw):
            seen.append((url, kw["headers"]["User-Agent"]))
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch("services.provider_status.requests.get", side_effect=get):
            out = ps.reach("http://cf.bears.test", UA)
        self.assertEqual([("http://cf.bears.test/", UA)], seen, "'/' with the player's User-Agent, no login")
        return out

    def test_what_each_answer_means(self):
        cases = [
            (cf_521(), "origin_down"),
            (Page(522, "", cloudflare=True), "origin_down"),
            (Page(403, "error code: 1010", cloudflare=True), "blocked"),
            (Page(403, "error code: 1020", cloudflare=True), "blocked"),
            (Page(511, ""), "blocked"),
            (Page(401, "Not allowed"), "answers"),
            (Page(401, "Permission denied", cloudflare=True), "answers"),
            (Page(200, "There is an Database Error"), "server_error"),
            (Page(504, "Gateway Timeout"), "server_error"),
            (requests.ConnectionError("[Errno 111] Connection refused"), "refused"),
            (requests.ConnectTimeout("timed out"), "no_answer"),
            (requests.ConnectionError("Failed to resolve 'x' (Name or service not known)"), "name_not_found"),
        ]
        for answer, how in cases:
            with self.subTest(how=how, answer=getattr(answer, "status_code", answer)):
                self.assertEqual(how, self.reach(answer)["how"])

    def test_a_cloudflare_block_keeps_its_code(self):
        self.assertEqual(1010, self.reach(Page(403, "error code: 1010", cloudflare=True))["cf_error"])


class Diagnose(unittest.TestCase):
    def probes(self, *hows):
        return [{"in_use": i == 0, "how": h} for i, h in enumerate(hows)]

    def test_that_night_every_address_down_is_down(self):
        # cf.* 521 three times, the direct ones refused
        out = ps.diagnose(ph._fail("api", "http", 521),
                          self.probes("origin_down", "origin_down", "origin_down", "refused", "refused", "refused"))
        self.assertEqual(("down", "not answering"), (out["case"], out["detail"]))

    def test_the_panels_database_error_is_down(self):
        out = ps.diagnose({**ph._fail("api", "not_json", 200), "hint": "database_error"},
                          self.probes("server_error", "server_error"))
        self.assertEqual(("down", "server error"), (out["case"], out["detail"]))

    def test_every_channel_502_with_the_login_working_is_down(self):
        out = ps.diagnose({**ph._fail("stream", "http", 502), "api_ok": True}, self.probes("answers"))
        self.assertEqual(("down", "not sending channels"), (out["case"], out["detail"]))

    def test_one_address_silent_while_its_twin_works_is_blocked(self):
        # bearsk.xyz timing out from home while cf.bearsk.xyz logs in (Roadmap, 19 Oct)
        out = ps.diagnose(ph._fail("api", "unreachable", detail="no answer"),
                          self.probes("no_answer", "answers"), backup_works=True)
        self.assertEqual("blocked", out["case"])

    def test_silent_everywhere_is_down_not_blocked(self):
        out = ps.diagnose(ph._fail("api", "unreachable", detail="no answer"),
                          self.probes("no_answer", "no_answer"), backup_works=False)
        self.assertEqual("down", out["case"])

    def test_refusals_are_blocked(self):
        for outcome, probes in (({**ph._fail("api", "http", 403), "cf_error": 1020}, self.probes("blocked")),
                                (ph._fail("api", "http", 403), self.probes("answers")),
                                (ph._fail("api", "http", 511), [])):
            with self.subTest(outcome=outcome):
                self.assertEqual("blocked", ps.diagnose(outcome, probes)["case"])

    def test_the_account(self):
        self.assertEqual(("account", "login rejected"), tuple(ps.diagnose(ph._fail("api", "login"), []).values()))
        self.assertEqual(("account", "expired"),
                         tuple(ps.diagnose(ph._fail("api", "expired", detail="2026-10-01"), []).values()))
        self.assertEqual(("account", "banned"),
                         tuple(ps.diagnose(ph._fail("api", "status", detail="Banned"), []).values()))


class Case(Base_):
    def setUp(self):
        super().setUp()
        ps.reset()
        self.addCleanup(ps.reset)
        self.provider.name = "IPTV Bears"
        self.provider.live_tv_enabled = True
        self.provider.user_agent = UA
        self.provider.addresses = [ProviderAddress(url=SERVER, history=[]),
                                   ProviderAddress(url=CF_TWIN, history=[])]
        self.db.add(LiveChannel(id=41, provider_id=self.provider.id, enabled=True, name="US: AMC 4K",
                                stream_url=SERVER + "/live/u/p/41.ts", stream_id="41"))
        self.db.add(LiveChannel(id=42, provider_id=self.provider.id, enabled=True, name="US: CNN",
                                stream_url=SERVER + "/live/u/p/42.ts", stream_id="42"))
        self.db.add(LiveChannel(id=43, provider_id=self.provider.id, enabled=True, name="CA: TSN 1",
                                stream_url=SERVER + "/live/u/p/43.ts", stream_id="43"))
        self.db.commit()
        self.clock[0] = datetime(2026, 10, 25, 18, 30).astimezone()
        # The other addresses, logged in to after a failure: not here
        p = mock.patch("services.provider_addresses.check_backups", return_value=0)
        p.start()
        self.addCleanup(p.stop)
        # A quick look runs in a thread on Tentacle's own database: here
        # only when a test calls it
        p = mock.patch("services.provider_status._quick_look_on")
        p.start()
        self.addCleanup(p.stop)

    def run_check(self, routes, confirm=True):
        net = Net(routes)

        def wait(seconds):
            self.waits.append(seconds)
            self.clock[0] += timedelta(seconds=seconds)
        with mock.patch("requests.get", side_effect=net.get):
            result = ph.check_providers(self.db, confirm_delay=120 if confirm else None, sleep=wait)
        self.net = net
        return result

    def family(self):
        self.db.expire_all()
        return [m for m in admin_alerts.family_messages(self.db) if m["area"] == "livetv"]

    def slate(self, channel_id=41):
        self.db.expire_all()
        return ps.slate_lines(self.db, self.provider.id, channel_id)


class TheProviderIsDown(Case):
    def test_said_plainly_with_the_time_it_started(self):
        self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": Page(521, "", cloudflare=True)})
        s = self.state()
        self.assertEqual(("failing", "down"), (s["state"], s["case"]))
        [m] = self.family()
        self.assertEqual("down", m["kind"])
        self.assertEqual("IPTV Bears is down right now (their server isn't answering) since 6:30 PM. "
                         "Not a problem with your account or your setup. Live TV comes back by itself.", m["text"])
        self.assertEqual(("IPTV Bears is down", "Their server isn't answering (since 6:30 PM)",
                          "Not your setup. Live TV comes back by itself"), self.slate())
        self.assertTrue(all(len(x) <= 70 for x in self.slate()))

    def test_the_admins_message_is_titled_by_the_case(self):
        self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()})
        seen = admin_alerts._seen_providers(self.db)
        area, title, message, push = seen[f"provider:{self.provider.id}"]
        self.assertEqual(("livetv", "IPTV Bears is down"), (area, title))
        self.assertIn("(Provider server error (HTTP 521) since 18:30. Usually on their side)", message)

    def test_back_is_said_for_half_an_hour(self):
        self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()})
        self.clock[0] = datetime(2026, 10, 25, 19, 45).astimezone()
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206), SERVER + "/live/": FakeResponse(206)})
        [m] = self.family()
        self.assertEqual("back", m["kind"])
        self.assertEqual("IPTV Bears is back: Live TV works again (it was down, 6:30–7:45 PM).", m["text"])
        self.assertIsNone(self.slate())
        self.clock[0] += timedelta(minutes=31)
        self.assertEqual([], self.family())

    def test_a_channel_delivering_ends_it_at_once(self):
        self.run_check({API: requests.ConnectionError("[Errno 111] Connection refused"),
                        ROOT: requests.ConnectionError("[Errno 111] Connection refused"),
                        CF_TWIN + "/": cf_521()})
        self.assertEqual("down", self.state()["case"])
        ph.note(self.db, self.provider.id, True, "stream")
        self.db.expire_all()
        self.assertEqual("ok", self.state()["state"])

    def test_confirmed_while_someone_watches_without_logging_in(self):
        # 22:01 that night: the login timed out, then someone was watching
        # when the 2-minute confirmation came, and the outage went unsaid.
        watching = [False]
        with mock.patch("services.provider_health._stand_aside", side_effect=lambda db: watching[0]):
            routes = {API: requests.ReadTimeout("read timed out"),
                      ROOT: requests.ConnectionError("[Errno 111] Connection refused"),
                      CF_TWIN + "/": cf_521()}
            net = Net(routes)

            def wait(seconds):
                watching[0] = True
                self.clock[0] += timedelta(seconds=seconds)
            with mock.patch("requests.get", side_effect=net.get):
                ph.check_providers(self.db, confirm_delay=120, sleep=wait)
        self.assertEqual(1, len([u for u in net.calls if u.startswith(API)]), "no second login")
        s = self.state()
        self.assertEqual(("failing", "down"), (s["state"], s["case"]))
        self.assertTrue(s["reason"].startswith("Provider unreachable since 18:30 (no answer)"), s["reason"])


class YoureBlocked(Case):
    def test_refused_by_cloudflare(self):
        block = Page(403, "error code: 1020", cloudflare=True)
        self.run_check({API: block, ROOT: block, CF_TWIN + "/": block})
        self.assertEqual("blocked", self.state()["case"])
        [m] = self.family()
        self.assertTrue(m["text"].startswith("IPTV Bears is refusing this home's connection since 6:30 PM. "
                                             "Your account is fine."), m["text"])
        self.assertEqual("Not coming through", self.slate()[0])
        self.assertEqual("IPTV Bears is refusing this home's connection", self.slate()[1])
        self.assertIn("Cloudflare error 1020", self.state()["reason"])

    def test_silent_here_while_the_twin_logs_in(self):
        twin = next(a for a in self.provider.addresses if a.url == CF_TWIN)
        twin.last_result = {"at": datetime.utcnow().isoformat() + "Z", "ok": True, "word": "works"}
        self.db.commit()
        self.run_check({API: requests.ConnectTimeout("timed out"), ROOT: requests.ConnectTimeout("timed out"),
                        CF_TWIN + "/": Page(401, "Not allowed", cloudflare=True)})
        self.assertEqual("blocked", self.state()["case"])

    def test_nothing_answering_and_no_internet_is_this_homes_connection(self):
        silent = requests.ConnectionError("Failed to resolve 'x' (Name or service not known)")
        self.run_check({API: silent, ROOT: silent, CF_TWIN + "/": silent, ps.INTERNET_PROBE: silent})
        self.assertEqual("offline", self.state()["case"])
        [m] = self.family()
        self.assertEqual("Live TV can't reach IPTV Bears since 6:30 PM: this home's internet connection seems to "
                         "be down (nothing else answers either). It comes back with the connection.", m["text"])
        self.assertNotIn("Not a problem with", m["text"])
        self.assertEqual("This home's internet connection seems to be down", self.slate()[2])

    def test_nothing_answering_while_the_internet_works_is_down(self):
        silent = requests.ConnectTimeout("timed out")
        self.run_check({API: silent, ROOT: silent, CF_TWIN + "/": silent, ps.INTERNET_PROBE: Page(401, "")})
        self.assertEqual("down", self.state()["case"])

    def test_admins_are_told_what_to_do(self):
        block = Page(403, "error code: 1006", cloudflare=True)
        self.run_check({API: block, ROOT: block, CF_TWIN + "/": block})
        [view] = ph.provider_view(self.db)
        self.assertEqual("blocked", view["case"])
        self.assertEqual("IPTV Bears is refusing this home's connection", view["headline"])
        self.assertIn("Sources", view["plain"])


class AccountProblem(Case):
    def test_expired(self):
        past = str(int(datetime(2026, 10, 20, 12).timestamp()))
        self.run_check({API: api_ok(status="Expired", exp_date=past)})
        s = self.state()
        self.assertEqual(("account", "expired"), (s["case"], s["case_detail"]))
        self.assertFalse([u for u in self.net.calls if u.endswith("/")], "no address asked: it's the account")
        [m] = self.family()
        self.assertEqual("The IPTV Bears account has expired (2026-10-20). Live TV is off until it's renewed. "
                         "Your admin knows.", m["text"])
        self.assertEqual(("Live TV is off", "The IPTV Bears account has expired", "Live TV is off until it's sorted out"),
                         self.slate())

    def test_banned_and_login_refused(self):
        self.run_check({API: api_ok(status="Banned")})
        self.assertEqual("The IPTV Bears account is banned since 6:30 PM. Live TV is off until it's sorted out. "
                         "Your admin knows.", self.family()[0]["text"])
        self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
        self.db.commit()
        self.run_check({API: api_ok(auth=0)})
        self.assertTrue(self.family()[0]["text"].startswith("IPTV Bears doesn't accept the login since 6:32 PM."), self.family()[0]["text"])

    def test_ending_soon_is_for_admins_only(self):
        soon = str(int((self.clock[0] + timedelta(days=4, hours=2)).timestamp()))
        self.run_check({API: api_ok(exp_date=soon), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual("ok", self.state()["state"])
        self.assertEqual([], self.family())
        admin = [m for m in ps.messages(self.db, admin=True) if m["kind"] == "account_soon"]
        self.assertEqual(1, len(admin))
        self.assertTrue(admin[0]["text"].startswith("The IPTV Bears account ends on Oct 29 (in 4 days)."), admin[0])
        seen = admin_alerts._seen_providers(self.db)
        self.assertEqual("The IPTV Bears account ends soon", seen[f"provider_expiry:{self.provider.id}"][1])

    def test_months_away_says_nothing(self):
        self.run_check({API: api_ok(), SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual([], ps.messages(self.db, admin=True))


class SomeChannelsOnly(Case):
    def test_one_channel_twice_while_others_play(self):
        ps.channel_ok(self.provider.id, 42)                 # CNN plays
        ps.channel_failed(self.provider.id, 41, "US: AMC 4K", 504)
        self.assertIsNone(self.slate(41), "once is a blip")
        ps.channel_failed(self.provider.id, 41, "US: AMC 4K", 504)
        [m] = self.family()
        self.assertEqual("channels", m["kind"])
        self.assertEqual("US: AMC 4K isn't coming through from IPTV Bears right now. Other channels work.", m["text"])
        self.assertEqual(("Not coming through", "IPTV Bears isn't sending this channel right now",
                          "Other channels work. Try another one"), self.slate(41))
        self.assertIsNone(self.slate(42))
        [view] = ph.provider_view(self.db)
        self.assertEqual(["US: AMC 4K"], view["channels_down"])
        ps.channel_ok(self.provider.id, 41)
        self.assertEqual([], self.family())

    def test_nothing_delivering_is_not_some_channels(self):
        ps._quick_at[self.provider.id] = ps._mono()        # no quick look in this test
        ps.channel_failed(self.provider.id, 41, "US: AMC 4K", 504)
        ps.channel_failed(self.provider.id, 41, "US: AMC 4K", 504)
        self.assertEqual([], self.family())

    def test_names_are_listed_shortly(self):
        ps.channel_ok(self.provider.id, 99)
        for cid, name in ((41, "AMC 4K"), (42, "CNN"), (43, "TSN 1"), (44, "TSN 2")):
            ps.channel_failed(self.provider.id, cid, name)
            ps.channel_failed(self.provider.id, cid, name)
        self.assertEqual("AMC 4K, CNN and 2 more channels aren't coming through from IPTV Bears right now. "
                         "Other channels work.", self.family()[0]["text"])


class QuickLook(Case):
    """Channels failing start a look at once (no login, no stream)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(ps._running, {"on": True})
        p.start()
        self.addCleanup(p.stop)

    def look(self, routes):
        net = Net(routes)
        started = []
        with mock.patch("services.provider_status.threading.Thread",
                        side_effect=lambda target, args, **kw: started.append(args) or mock.Mock()):
            ps.channel_failed(self.provider.id, 41, "US: AMC 4K", 504)
            ps.channel_failed(self.provider.id, 42, "US: CNN", 504)
        self.assertEqual([(self.provider.id, None)], started, "one look for the burst")
        with mock.patch("requests.get", side_effect=net.get):
            case = ps.quick_look(self.provider.id, self.db)
        self.assertFalse([u for u in net.calls if "player_api" in u or "/live/" in u], "no login, no stream")
        return case

    def test_two_channels_and_the_server_down_is_said_at_once(self):
        self.assertEqual("down", self.look({ROOT: requests.ConnectionError("[Errno 111] Connection refused"),
                                            CF_TWIN + "/": cf_521()}))
        s = self.state()
        self.assertEqual(("failing", "down"), (s["state"], s["case"]))
        self.assertEqual("down", self.family()[0]["kind"])

    def test_the_server_answering_is_no_outage(self):
        self.assertIsNone(self.look({ROOT: Page(401, "Not allowed"), CF_TWIN + "/": Page(401, "Not allowed")}))
        self.assertEqual("unknown", ph.load_state(self.db).get(str(self.provider.id), {"state": "unknown"})["state"])

    def test_three_channels_failing_and_none_delivering_is_the_whole_provider(self):
        ps.channel_failed(self.provider.id, 43, "CA: TSN 1", 502)
        self.assertEqual("down", self.look({ROOT: Page(401, "Not allowed"), CF_TWIN + "/": Page(401, "Not allowed")}))
        self.assertEqual("IPTV Bears is down right now (their server isn't sending any channel) since 6:30 PM. "
                         "Not a problem with your account or your setup. Live TV comes back by itself.",
                         self.family()[0]["text"])

    def test_only_in_the_running_app(self):
        ps._running["on"] = False
        started = []
        with mock.patch("services.provider_status.threading.Thread",
                        side_effect=lambda target, args, **kw: started.append(args) or mock.Mock()):
            ps.channel_failed(self.provider.id, 41, "US: AMC 4K")
            ps.channel_failed(self.provider.id, 42, "US: CNN")
        self.assertEqual([], started)

    def test_not_again_within_five_minutes(self):
        ps._quick_at[self.provider.id] = ps._mono()
        started = []
        with mock.patch("services.provider_status.threading.Thread",
                        side_effect=lambda target, args, **kw: started.append(args) or mock.Mock()):
            ps.channel_failed(self.provider.id, 41, "US: AMC 4K")
            ps.channel_failed(self.provider.id, 42, "US: CNN")
        self.assertEqual([], started)


class BackWithoutWaitingForTheHour(Case):
    def watch(self, answer):
        net = Net({ROOT: answer})
        with mock.patch("requests.get", side_effect=net.get), \
                mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch("services.provider_health.check_now") as check:
            ps.watch()
        return check.called

    def test_a_down_provider_answering_again_is_checked_at_once(self):
        self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()})
        self.assertFalse(self.watch(Page(401, "Not allowed")), "checked under 15 minutes ago")
        self.clock[0] += timedelta(minutes=16)
        self.assertFalse(self.watch(cf_521()), "still down: no login for nothing")
        self.assertTrue(self.watch(Page(401, "Not allowed")))


class EveryScreenHears(Case):
    def test_status_goes_to_every_person_in_jellyfin(self):
        from models.database import TentacleUser
        from services import live_updates
        self.db.add_all([Setting(key="jellyfin_url", value="http://jf.test"), Setting(key="jellyfin_api_key", value="k"),
                         TentacleUser(jellyfin_user_id="aaaa1111", display_name="Lucas", is_admin=True),
                         TentacleUser(jellyfin_user_id="bbbb2222", display_name="Ann")])
        self.db.commit()
        sent = []
        with mock.patch.object(live_updates, "_clients_on", True), \
                mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch.object(live_updates, "post_notify", lambda url, key, topics: sent.append(topics)), \
                mock.patch("services.live_updates.threading.Timer"):
            live_updates.tell_clients("status", everyone=True)
            live_updates._send_to_clients()
        self.assertEqual([{"aaaa1111": {"status"}, "bbbb2222": {"status"}}], sent)

    def test_a_change_in_the_words_is_published_once(self):
        from services import live_updates
        told = []
        with mock.patch.object(live_updates, "active", lambda: True), \
                mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch.object(live_updates, "publish", lambda topic, **kw: told.append(("tab", topic))), \
                mock.patch.object(live_updates, "tell_clients", lambda topic, **kw: told.append(("client", topic))):
            ps._sig["value"] = "[]"
            self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()})
            ps._timer["t"] and ps._timer["t"].cancel()
            ps._publish_if_changed()
            ps._publish_if_changed()
        self.assertEqual([("tab", "status"), ("client", "status")], [t for t in told if t[1] == "status"])


class NoSecretsInTheWords(Case):
    def test_messages_carry_no_address_or_login(self):
        for routes in ({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()},
                       {API: Page(403, "error code: 1010", cloudflare=True), ROOT: cf_521(), CF_TWIN + "/": cf_521()}):
            self.db.query(Setting).filter(Setting.key == ph.SETTING_KEY).delete()
            self.db.commit()
            self.run_check(routes)
            text = json.dumps(ps.messages(self.db, admin=True)) + json.dumps(self.slate()) + \
                json.dumps(ph.public_status(self.db)[1])
            for secret in ("example.test", "acct-user-77", "s3cr3t-pass-91", "http"):
                self.assertNotIn(secret, text)

    def test_public_status_says_the_case(self):
        self.run_check({API: cf_521(), ROOT: cf_521(), CF_TWIN + "/": cf_521()})
        code, body = ph.public_status(self.db)
        self.assertEqual((503, ["down"]), (code, body["case"]))


if __name__ == "__main__":
    unittest.main()
