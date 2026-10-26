"""Several addresses per provider (services/provider_addresses.py, #704).

When an address stops working (an ISP blocking one IP on game nights), the
provider sends a message with others. The card keeps them: one in use (the
provider's server_url), the rest backups; a test from the server ranks them.
The hourly check logs in to the backups only when the address in use failed;
nothing switches by itself: the Health banner offers Switch / Keep, and a
switch moves the provider without importing anything again.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import socket
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import (
    ActivityLog, EPGProgram, LiveChannel, Movie, Provider, ProviderAddress, Setting,
)
from services import provider_addresses as pa
from tmp_dirs import temp_dir

MESSAGE = """Hello! Our main link is having trouble tonight. Please use one of these:

Link 1: http://bearsclub.online
Link 2: cf.bearsp.xyz
Link 3 (backup) - http://cf.bearsk.xyz:80/
Link 4: https://line.example.tv:8443/get.php?username=me&password=secret&type=m3u_plus
VPN users only: apps.bearsk.xyz
Questions? support@bearsk.xyz or see logo.png
"""


class FindAddresses(unittest.TestCase):
    def test_the_providers_whole_message(self):
        self.assertEqual(
            ["http://bearsclub.online", "http://cf.bearsp.xyz", "http://cf.bearsk.xyz",
             "https://line.example.tv:8443", "http://apps.bearsk.xyz"],
            pa.find_addresses(MESSAGE))

    def test_no_login_path_or_mail_address_is_kept(self):
        found = " ".join(pa.find_addresses(MESSAGE))
        for leftover in ("username", "secret", "get.php", "support@", "logo.png"):
            self.assertNotIn(leftover, found)

    def test_each_address_once(self):
        self.assertEqual(["http://a.example.com:8080"],
                         pa.find_addresses("http://A.example.com:8080/ and a.example.com:8080 again"))

    def test_normalize(self):
        self.assertEqual("http://x.example", pa.normalize("x.example/"))
        self.assertEqual("https://x.example", pa.normalize("https://x.example:443/player_api.php"))
        self.assertEqual("http://x.example:8080", pa.normalize("HTTP://X.example:8080"))
        self.assertEqual("", pa.normalize("ftp://x.example"))
        self.assertEqual("", pa.normalize("not an address"))


class _Db(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.dir}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.p = Provider(name="Bears", provider_type="xtream", server_url="http://bearsk.xyz",
                          username="u", password="p1", active=True, live_tv_enabled=True)
        self.db.add(self.p)
        self.db.commit()
        # Nobody is watching unless a test says so
        watch = mock.patch.object(pa, "_someone_watching", lambda db: self.watching)
        watch.start()
        self.addCleanup(watch.stop)
        self.watching = False


class TheList(_Db):
    def test_paste_adds_backups_and_keeps_the_one_in_use(self):
        result = pa.add_from_text(self.db, self.p, MESSAGE)
        self.assertEqual(5, len(result["added"]))
        urls = [a.url for a in self.p.addresses]
        self.assertIn("http://bearsk.xyz", urls)
        self.assertEqual(6, len(urls))
        again = pa.add_from_text(self.db, self.p, "http://bearsclub.online")
        self.assertEqual([], again["added"])
        self.assertEqual(["http://bearsclub.online"], again["known"])

    def test_the_address_in_use_cannot_be_removed(self):
        pa.add_from_text(self.db, self.p, "cf.bearsp.xyz")
        current = next(a for a in self.p.addresses if a.url == "http://bearsk.xyz")
        with self.assertRaises(ValueError):
            pa.remove(self.db, self.p, current.id)
        backup = next(a for a in self.p.addresses if a.url == "http://cf.bearsp.xyz")
        pa.remove(self.db, self.p, backup.id)
        self.assertEqual(["http://bearsk.xyz"], [a.url for a in self.p.addresses])

    def test_ranked_works_first_then_the_most_reliable(self):
        pa.add_from_text(self.db, self.p, "a.example b.example c.example")
        now = datetime.utcnow()
        by = {a.url: a for a in self.p.addresses}

        def hist(oks):
            return [{"at": (now - timedelta(hours=i)).isoformat() + "Z", "ok": ok, "word": ""} for i, ok in enumerate(oks)]
        by["http://a.example"].last_result = {"at": now.isoformat(), "ok": True, "word": pa.WORKS,
                                              "live": {"ok": True, "seconds": 2.0}}
        by["http://a.example"].history = hist([True, False, False])
        by["http://b.example"].last_result = {"at": now.isoformat(), "ok": True, "word": pa.WORKS,
                                              "live": {"ok": True, "seconds": 3.0}}
        by["http://b.example"].history = hist([True, True, True])
        by["http://c.example"].last_result = {"at": now.isoformat(), "ok": False, "word": "no answer"}
        self.db.commit()
        rows = pa.ranked(self.db, self.p)
        self.assertEqual(["http://b.example", "http://a.example", "http://c.example", "http://bearsk.xyz"],
                         [r["url"] for r in rows])
        self.assertEqual(3, rows[0]["day_good"])
        in_use = next(r for r in rows if r["in_use"])
        self.assertEqual("http://bearsk.xyz", in_use["url"])


class _Resp:
    def __init__(self, status=200, body=None, headers=None, chunks=(b"x" * 100,)):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.chunks = chunks

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body

    def iter_content(self, n):
        yield from self.chunks

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        Net.open -= 1


class Net:
    """Fake network: answers per host; counts connections open at once."""
    open = 0
    most = 0

    def __init__(self, answers):
        self.answers = answers      # host -> {"api": _Resp | Exception, "live": ..., "vod": ...}
        self.calls = []
        Net.open = Net.most = 0

    def get(self, url, **kw):
        host = url.split("//")[1].split("/")[0]
        kind = "api" if "player_api.php" in url else "live" if "/live/" in url else "vod"
        self.calls.append((host, kind))
        answer = self.answers.get(host, {}).get(kind, requests.ConnectTimeout("timed out"))
        if isinstance(answer, Exception):
            raise answer
        if kw.get("stream"):
            Net.open += 1
            Net.most = max(Net.most, Net.open)
        return answer


LOGIN_OK = {"user_info": {"auth": 1, "status": "Active", "active_cons": "0", "max_connections": "1"}}


class TestOneAddress(_Db):
    def setUp(self):
        super().setUp()
        self.db.add(LiveChannel(provider_id=self.p.id, name="Leafs", stream_id="605186", enabled=True,
                                stream_url="http://bearsk.xyz/live/u/p1/605186.ts"))
        strm = self.dir / "Film.strm"
        strm.write_text("http://192.168.2.52:8888/api/vod/movie/1.4242.abcdef0123456789abcd.mkv", encoding="utf-8")
        self.db.add(Movie(tmdb_id=1, title="Film", year="2020", source="provider", provider_id=self.p.id,
                          strm_path=str(strm)))
        self.db.commit()
        dns = mock.patch.object(socket, "getaddrinfo",
                                lambda host, port: [(2, 1, 6, "", ("103.211.100.216", 0))])
        dns.start()
        self.addCleanup(dns.stop)

    def _test(self, answers, url="http://cf.bearsp.xyz"):
        self.net = Net(answers)
        sleeps = []
        with mock.patch.object(pa.requests, "get", self.net.get):
            result = pa.test_address(self.db, self.p, url, sleep=sleeps.append)
        self.sleeps = sleeps
        return result

    def test_works_login_live_and_film(self):
        r = self._test({"cf.bearsp.xyz": {"api": _Resp(body=LOGIN_OK, headers={"Server": "cloudflare"}),
                                          "live": _Resp(), "vod": _Resp(206)}})
        self.assertEqual(pa.WORKS, r["word"])
        self.assertTrue(r["ok"])
        self.assertTrue(r["cloudflare"])
        self.assertEqual("103.211.100.216", r["ip"])
        self.assertEqual({"active": 0, "max": 1}, {k: r["login"][k] for k in ("active", "max")})
        self.assertTrue(r["live"]["ok"] and r["vod"]["ok"])
        # the film is opened on the tested address, by the provider's own path
        self.assertEqual([("cf.bearsp.xyz", "api"), ("cf.bearsp.xyz", "live"), ("cf.bearsp.xyz", "vod")],
                         self.net.calls)

    def test_never_two_connections_at_once(self):
        """A one-connection account cuts a stream for a second one."""
        self._test({"cf.bearsp.xyz": {"api": _Resp(body=LOGIN_OK), "live": _Resp(), "vod": _Resp()}})
        self.assertEqual(1, Net.most)
        self.assertEqual([pa.PAUSE_BETWEEN_CONNECTIONS], self.sleeps)

    def test_words(self):
        cases = {
            "no answer": {"api": requests.ConnectTimeout("timed out")},
            "refused (403)": {"api": _Resp(403)},
            "login rejected": {"api": _Resp(body={"user_info": {"auth": 0}})},
            "account expired": {"api": _Resp(body={"user_info": {"auth": 1, "status": "Expired"}})},
            "answers with a web page, not the provider": {"api": _Resp(body=None)},
            "login works, streams refused (511): refuses this connection, as for a VPN":
                {"api": _Resp(body=LOGIN_OK), "live": _Resp(511), "vod": _Resp(511)},
        }
        for word, answers in cases.items():
            with self.subTest(word):
                r = self._test({"cf.bearsp.xyz": answers})
                self.assertEqual(word, r["word"])
                self.assertFalse(r["ok"])

    def test_name_not_found(self):
        def no_dns(host, port):
            raise socket.gaierror("Name or service not known")
        with mock.patch.object(socket, "getaddrinfo", no_dns):
            r = self._test({})
        self.assertEqual("name not found", r["word"])
        self.assertEqual([], self.net.calls)

    def test_no_stream_test_while_someone_watches(self):
        self.watching = True
        r = self._test({"cf.bearsp.xyz": {"api": _Resp(body=LOGIN_OK), "live": _Resp(), "vod": _Resp()}})
        self.assertEqual(pa.LOGIN_ONLY, r["word"])
        self.assertEqual("someone is watching", r["streams_skipped"])
        self.assertEqual([("cf.bearsp.xyz", "api")], self.net.calls)

    def test_no_stream_test_while_the_account_is_in_use(self):
        """Someone on another app (TiviMate on a phone) counts too."""
        busy = {"user_info": {"auth": 1, "status": "Active", "active_cons": "1", "max_connections": "1"}}
        r = self._test({"cf.bearsp.xyz": {"api": _Resp(body=busy), "live": _Resp(), "vod": _Resp()}})
        self.assertEqual("the account is in use", r["streams_skipped"])
        self.assertEqual([("cf.bearsp.xyz", "api")], self.net.calls)

    def test_test_all_goes_one_address_at_a_time(self):
        pa.add_from_text(self.db, self.p, "cf.bearsp.xyz bearsclub.online")
        answers = {h: {"api": _Resp(body=LOGIN_OK), "live": _Resp(), "vod": _Resp()}
                   for h in ("bearsk.xyz", "cf.bearsp.xyz", "bearsclub.online")}
        net = Net(answers)
        make = sessionmaker(bind=self.db.bind)
        with mock.patch.object(pa, "SessionLocal", make), mock.patch.object(pa.requests, "get", net.get):
            self.assertTrue(pa.test_all(self.p.id, sleep=lambda s: None))
        self.assertEqual(1, Net.most)
        self.db.expire_all()
        self.assertTrue(all((a.last_result or {}).get("word") == pa.WORKS for a in self.p.addresses))
        self.assertFalse(pa.progress(self.p.id)["running"])

    def test_no_login_in_what_is_kept(self):
        self.p.password = "s3cret-pass"
        self.db.commit()
        r = self._test({"cf.bearsp.xyz": {"api": _Resp(403)}})
        self.assertNotIn("s3cret-pass", repr(r))


class SwitchKeepsTheLibrary(_Db):
    """A switch is the same provider: nothing imported again, nothing deleted,
    no duplicates; ids, numbers, guide and files stay."""

    def setUp(self):
        super().setUp()
        from services import vod_tokens
        self.routed = self.dir / "Routed (2020).strm"
        self.routed_text = vod_tokens.Links("http://192.168.2.52:8888", "k" * 64, self.p.id).movie(43, "mkv")
        self.routed.write_text(self.routed_text, encoding="utf-8")
        self.direct = self.dir / "Direct (2020).strm"
        self.direct.write_text("http://bearsk.xyz/movie/u/p1/42.mkv", encoding="utf-8")
        self.db.add_all([
            Movie(tmdb_id=1, title="Routed", year="2020", source="provider", provider_id=self.p.id,
                  strm_path=str(self.routed), jellyfin_item_id="jf1",
                  provider_missing_since=datetime.utcnow() - timedelta(days=1)),
            Movie(tmdb_id=2, title="Direct", year="2020", source="provider", provider_id=self.p.id,
                  strm_path=str(self.direct), jellyfin_item_id="jf2"),
            LiveChannel(provider_id=self.p.id, name="Leafs", stream_id="605186", enabled=True,
                        stream_url="http://bearsk.xyz/live/u/p1/605186.ts", epg_channel_id="leafs.ca"),
            EPGProgram(channel_id="leafs.ca", title="Game", start=datetime.utcnow(),
                       stop=datetime.utcnow() + timedelta(hours=3)),
        ])
        self.db.commit()
        self.mtime = self.routed.stat().st_mtime_ns

    def _switch(self, url="http://cf.bearsp.xyz"):
        from services import provider_relink

        def relink_now(provider_id, before):
            with mock.patch("services.sync.chown_path", lambda p: None):
                provider_relink.relink_strms(self.db, self.db.get(Provider, provider_id), before)
        with mock.patch.object(provider_relink, "start_relink", relink_now), \
                mock.patch("services.pushover.send", lambda *a, **k: True):
            return pa.switch_to(self.db, self.p, url, "chosen by hand")

    def test_rows_ids_and_jellyfin_items_stay(self):
        before = [(m.id, m.tmdb_id, m.strm_path, m.jellyfin_item_id) for m in self.db.query(Movie).order_by(Movie.id)]
        ch = self.db.query(LiveChannel).one()
        ch_before = (ch.id, ch.stream_id, ch.enabled, ch.epg_channel_id)
        self._switch()
        self.db.expire_all()
        self.assertEqual(before, [(m.id, m.tmdb_id, m.strm_path, m.jellyfin_item_id)
                                  for m in self.db.query(Movie).order_by(Movie.id)])
        ch = self.db.query(LiveChannel).one()
        self.assertEqual(ch_before, (ch.id, ch.stream_id, ch.enabled, ch.epg_channel_id))
        self.assertEqual("http://cf.bearsp.xyz/live/u/p1/605186.ts", ch.stream_url)
        self.assertEqual(1, self.db.query(EPGProgram).count())
        self.assertEqual("http://cf.bearsp.xyz", self.p.server_url)

    def test_files_through_tentacle_are_not_touched(self):
        self._switch()
        self.assertEqual(self.routed_text, self.routed.read_text(encoding="utf-8"))
        self.assertEqual(self.mtime, self.routed.stat().st_mtime_ns)

    def test_direct_files_follow(self):
        self._switch()
        self.assertEqual("http://cf.bearsp.xyz/movie/u/p1/42.mkv", self.direct.read_text(encoding="utf-8"))

    def test_marks_from_the_old_address_no_longer_count(self):
        self._switch()
        self.db.expire_all()
        self.assertIsNone(self.db.query(Movie).filter(Movie.tmdb_id == 1).one().provider_missing_since)
        self.assertTrue(pa.in_probation(self.db, self.p.id))

    def test_said_in_activity_and_remembered_as_chosen(self):
        self._switch()
        self.assertEqual("http://cf.bearsp.xyz", self.p.preferred_address)
        msg = self.db.query(ActivityLog).filter(ActivityLog.event == "provider_address").one().message
        self.assertIn("now uses http://cf.bearsp.xyz instead of http://bearsk.xyz", msg)
        self.assertNotIn("p1", msg.replace("http", ""))

    def test_the_same_address_is_no_switch(self):
        result = self._switch("http://bearsk.xyz/")
        self.assertFalse(result["switched"])
        self.assertFalse(pa.in_probation(self.db, self.p.id))


class TheHourlyCheck(_Db):
    """check_backups: the other addresses are logged in to (no stream) only
    after the address in use failed, and nothing switches by itself."""

    def setUp(self):
        super().setUp()
        pa.add_from_text(self.db, self.p, "cf.bearsp.xyz bearsclub.online")
        self.results = {}
        self.tested = []

        def fake_test(db, p, url, streams=True, sleep=None):
            self.tested.append((url, streams))
            word = self.results.get(url, pa.LOGIN_ONLY)
            return {"at": datetime.utcnow().isoformat() + "Z", "ok": word in (pa.WORKS, pa.LOGIN_ONLY),
                    "word": word, "ip": None, "cloudflare": None, "login": None, "live": None,
                    "vod": None, "streams_skipped": None}
        t = mock.patch.object(pa, "test_address", fake_test)
        t.start()
        self.addCleanup(t.stop)
        s = mock.patch.object(pa, "switch_to", side_effect=AssertionError("never switches by itself"))
        s.start()
        self.addCleanup(s.stop)

    def _run(self, kind="unreachable"):
        return pa.check_backups(self.db, self.p, {"verdict": "fail", "kind": kind}, sleep=lambda s: None)

    def test_a_login_on_each_other_address_and_no_stream(self):
        self.assertEqual(2, self._run())
        self.assertEqual([("http://cf.bearsp.xyz", False), ("http://bearsclub.online", False)], self.tested)

    def test_not_for_an_account_problem(self):
        for kind in ("login", "expired", "status"):
            self.assertEqual(0, self._run(kind))
        self.assertEqual([], self.tested)

    def test_never_while_anything_plays_or_records(self):
        self.watching = True
        self.assertEqual(0, self._run())
        self.assertEqual([], self.tested)

    def test_offer_names_the_failing_address_and_the_best_that_works(self):
        self.results["http://cf.bearsp.xyz"] = "refused (403)"
        self._run()
        entry = {"state": "failing", "case": "blocked"}
        offer = pa.offer_for(self.p, entry)
        backup = next(a for a in self.p.addresses if a.url == "http://bearsclub.online")
        self.assertEqual({"from": "http://bearsk.xyz", "to": "http://bearsclub.online", "address_id": backup.id,
                          "word": pa.LOGIN_ONLY}, {k: v for k, v in offer.items() if k != "tested_at"})
        self.assertEqual("http://bearsk.xyz", self.p.server_url, "nothing switched")

    def test_no_offer_when_nothing_else_works_or_it_is_not_failing(self):
        self.results.update({"http://cf.bearsp.xyz": "no answer", "http://bearsclub.online": "refused (403)"})
        self._run()
        self.assertIsNone(pa.offer_for(self.p, {"state": "failing", "case": "down"}))
        self.results.clear()
        self._run()
        self.assertIsNone(pa.offer_for(self.p, {"state": "ok"}))
        self.assertIsNone(pa.offer_for(self.p, {"state": "failing", "case": "account"}))

    def test_a_kept_address_is_not_offered_again_another_may_be(self):
        self._run()
        first = pa.offer_for(self.p, {"state": "failing"})["to"]
        second = pa.offer_for(self.p, {"state": "failing", "kept": [first]})["to"]
        self.assertNotEqual(first, second)
        self.assertIsNone(pa.offer_for(self.p, {"state": "failing", "kept": [first, second]}))

    def test_an_old_result_is_not_offered(self):
        self._run()
        for a in self.p.addresses:
            a.last_result = {**(a.last_result or {}), "at": (datetime.utcnow() - timedelta(hours=3)).isoformat() + "Z"}
        self.assertIsNone(pa.offer_for(self.p, {"state": "failing"}))


class _Health(_Db):
    """provider_health.check_providers with the address in use failing and a
    backup that logs in: the banner's offer, Keep and Switch."""

    def setUp(self):
        super().setUp()
        from services import provider_health
        self.ph = provider_health
        pa.add_from_text(self.db, self.p, "cf.bearsp.xyz")
        self.backup = next(a for a in self.p.addresses if a.url == "http://cf.bearsp.xyz")
        self.sent = []
        self.published = []
        self.logins = []

        def fake_test(db, p, url, streams=True, sleep=None):
            self.logins.append((url, streams))
            return {"at": datetime.utcnow().isoformat() + "Z", "ok": True, "word": pa.LOGIN_ONLY, "ip": None,
                    "cloudflare": None, "login": {"ok": True, "seconds": 0.4}, "live": None, "vod": None,
                    "streams_skipped": "not tested this time"}
        self.failing = {"verdict": "fail", "step": "api", "kind": "unreachable", "code": None, "detail": "no answer"}
        self.outcome = self.failing
        for p in (mock.patch.object(provider_health, "_stand_aside", lambda db: False),
                  mock.patch.object(provider_health, "check_provider", lambda db, p, sleep: self.outcome),
                  mock.patch.object(pa, "test_address", fake_test),
                  mock.patch("services.provider_status.look",
                             lambda p, o: {"case": "blocked", "detail": "silent", "probes": []}),
                  mock.patch("services.pushover.send", lambda db, title, msg, url=None: self.sent.append(title) or True),
                  mock.patch("services.live_updates.publish",
                             lambda topic, **kw: self.published.append((topic, kw.get("admins")))),
                  mock.patch.object(pa, "switch_to", side_effect=AssertionError("never switches by itself"))):
            p.start()
            self.addCleanup(p.stop)

    def check(self):
        return self.ph.check_providers(self.db, confirm_delay=0, sleep=lambda s: None)

    def entry(self):
        return self.ph.load_state(self.db)[str(self.p.id)]


class TheBannerAsks(_Health):
    def test_the_address_in_use_failing_is_offered_not_switched(self):
        self.check()
        self.assertEqual("http://bearsk.xyz", self.p.server_url)
        offer = self.entry()["offer"]
        self.assertEqual(("http://bearsk.xyz", "http://cf.bearsp.xyz", self.backup.id),
                         (offer["from"], offer["to"], offer["address_id"]))
        self.assertEqual([("http://cf.bearsp.xyz", False)], self.logins, "one login, no stream")
        [view] = self.ph.provider_view(self.db)
        self.assertEqual(offer, view["address_offer"])
        self.assertEqual(1, view["other_addresses"])
        self.assertIn("Another of its addresses works: switch to it, or keep this one", view["plain"])
        # Open dashboards hear it (live update), and the phone once
        self.assertIn(("health", True), self.published)
        self.assertEqual(["Tentacle: Bears is failing", "Tentacle: Bears: another address works"], self.sent)
        self.check()
        self.assertEqual(2, len(self.sent), "the same offer is not sent again")

    def test_keep_stops_offering_it_for_this_incident(self):
        self.check()
        self.published.clear()
        self.assertTrue(self.ph.keep_address(self.db, self.p.id, "http://cf.bearsp.xyz"))
        self.assertIsNone(self.entry()["offer"])
        self.assertEqual(["http://cf.bearsp.xyz"], self.entry()["kept"])
        self.assertIn(("health", True), self.published, "the banner's buttons go away everywhere")
        self.check()
        self.assertIsNone(self.entry()["offer"], "still kept on the next hour")
        self.assertEqual("http://bearsk.xyz", self.p.server_url)
        # It works again, then fails again: a new incident asks again
        self.outcome = {"verdict": "ok"}
        self.check()
        self.assertIsNone(self.entry()["kept"])
        self.outcome = self.failing
        self.check()
        self.assertEqual("http://cf.bearsp.xyz", self.entry()["offer"]["to"])

    def test_keep_without_a_failure_is_refused(self):
        self.outcome = {"verdict": "ok"}
        self.check()
        self.assertFalse(self.ph.keep_address(self.db, self.p.id, "http://cf.bearsp.xyz"))

    def test_while_it_works_the_other_addresses_are_left_alone(self):
        self.outcome = {"verdict": "ok"}
        for _ in range(3):
            self.check()
        self.assertEqual([], self.logins)
        self.assertIsNone(self.entry()["offer"])

    def test_switch_clears_the_failure_and_the_offer(self):
        self.check()
        self.ph.address_switched(self.db, self.p.id)
        e = self.entry()
        self.assertEqual(("unknown", None, None), (e["state"], e["offer"], e["kept"]))


class TheBannerRoutes(_Health):
    """POST .../addresses/{id}/use (Switch) and .../keep (Keep), admins only."""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from models.database import TentacleUser, get_db
        from routers import providers
        self.db.add(Setting(key="session_secret", value="s3cret"))
        admin = TentacleUser(jellyfin_user_id="a1", display_name="Admin", is_admin=True)
        viewer = TentacleUser(jellyfin_user_id="v1", display_name="Viewer", is_admin=False)
        self.db.add_all([admin, viewer])
        self.db.commit()
        self.admin_id, self.viewer_id = admin.id, viewer.id
        app = FastAPI()
        app.include_router(providers.router)
        make = sessionmaker(bind=self.db.bind)

        def _db():
            db = make()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app)
        self.check()

    def sign_in(self, user_id):
        from routers import auth
        self.client.cookies.set(auth.COOKIE_NAME, auth._sign_session(user_id, "s3cret"))

    def test_keep(self):
        self.sign_in(self.admin_id)
        r = self.client.post(f"/api/providers/{self.p.id}/addresses/{self.backup.id}/keep")
        self.assertEqual(200, r.status_code, r.text)
        self.db.expire_all()
        self.assertIsNone(self.entry()["offer"])
        self.assertEqual("http://bearsk.xyz", self.db.get(Provider, self.p.id).server_url)

    def test_switch(self):
        self.sign_in(self.admin_id)
        done = []
        with mock.patch.object(pa, "switch_to", lambda db, p, url, why: done.append((url, why)) or
                               {"switched": True, "provider_id": p.id, "to": url}):
            r = self.client.post(f"/api/providers/{self.p.id}/addresses/{self.backup.id}/use")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([("http://cf.bearsp.xyz", "chosen by hand")], done)
        self.db.expire_all()
        self.assertIsNone(self.entry()["offer"])

    def test_viewers_and_strangers_are_refused(self):
        for who in (None, self.viewer_id):
            if who:
                self.sign_in(who)
            r = self.client.post(f"/api/providers/{self.p.id}/addresses/{self.backup.id}/keep")
            self.assertIn(r.status_code, (401, 403))
        self.db.expire_all()
        self.assertIsNotNone(self.entry()["offer"])

    def test_the_old_auto_switch_setting_is_gone(self):
        self.sign_in(self.admin_id)
        r = self.client.put(f"/api/providers/{self.p.id}/addresses/settings", json={"auto_switch": "switch"})
        self.assertIn(r.status_code, (404, 405))
        self.assertNotIn("auto_switch", self.client.get(f"/api/providers/{self.p.id}/addresses").json())


class TheOldAutoSwitchSettingIsDropped(unittest.TestCase):
    def test_the_column_goes_and_the_providers_stay(self):
        import sqlite3
        conn = sqlite3.connect(temp_dir(self) + "/t.db")
        self.addCleanup(conn.close)
        cur = conn.cursor()
        cur.execute("CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT, server_url TEXT, "
                    "address_auto_switch TEXT DEFAULT 'switch', preferred_address TEXT)")
        cur.execute("INSERT INTO providers (name, server_url, address_auto_switch, preferred_address) "
                    "VALUES ('Bears', 'http://bearsk.xyz', 'switch_back', 'http://bearsk.xyz')")
        conn.commit()
        mdb._drop_retired_columns(cur, conn)
        mdb._drop_retired_columns(cur, conn)        # twice is fine
        self.assertNotIn("address_auto_switch", mdb._existing_columns(cur, "providers"))
        self.assertEqual([("Bears", "http://bearsk.xyz", "http://bearsk.xyz")],
                         cur.execute("SELECT name, server_url, preferred_address FROM providers").fetchall())
        self.assertNotIn("address_auto_switch", mdb.Provider.__table__.columns)


class DeletingTheProviderTakesItsAddresses(_Db):
    def test_cascade(self):
        pa.add_from_text(self.db, self.p, "cf.bearsp.xyz")
        self.db.delete(self.p)
        self.db.commit()
        self.assertEqual(0, self.db.query(ProviderAddress).count())


if __name__ == "__main__":
    unittest.main()


# ─── The sync after a switch (nightly_harness: the real sync_provider) ──────

from nightly_harness import NightlyHarness, FakeClient  # noqa: E402
import services.sync as sync  # noqa: E402
from models.database import Series  # noqa: E402


class _AddressClient(FakeClient):
    """Writes direct links on the provider's current address, as the real
    XtreamClient does."""

    def __init__(self, provider, **kw):
        super().__init__(**kw)
        self.provider = provider

    def movie_stream_url(self, stream_id, ext):
        return f"{self.provider.server_url}/movie/u/p/{stream_id}.{ext}"

    def episode_stream_url(self, ep_id, ext):
        return f"{self.provider.server_url}/series/u/p/{ep_id}.{ext}"


class TheSyncAfterASwitch(NightlyHarness):
    """The sweep never reads a failed or partial listing on the new address as
    "titles gone"; once a full one came, the usual two strikes apply."""

    TITLES = [f"Film {i}" for i in range(40)]

    def setUp(self):
        super().setUp()
        self.client = _AddressClient(self.provider)
        sync.make_provider_client = lambda provider: self.client
        self.add_category("1")
        self.add_category("2")
        self.add_category("s1", type_="series")
        self.catalogue_movies("1", self.TITLES[:20])
        self.catalogue_movies("2", self.TITLES[20:], first_tmdb=2000)
        self.catalogue_series("s1", ["Show A", "Show B"])
        self.night()
        self.full = {c: list(v) for c, v in self.client.movies.items()}
        self.assertEqual(40, self.db.query(Movie).count())
        self.rows = self._rows()

    def _rows(self):
        return sorted((m.id, m.tmdb_id, m.strm_path) for m in self.db.query(Movie))

    def _switch(self):
        from services import provider_relink
        with mock.patch.object(provider_relink, "start_relink", lambda *a: None):
            pa.switch_to(self.db, self.provider, "http://cf.provider", "test")

    def test_a_failed_listing_marks_nothing(self):
        self._switch()
        self.client.raise_for = {"2"}
        self.night()
        self.assertEqual(self.rows, self._rows())
        self.assertEqual(0, self.db.query(Movie).filter(Movie.provider_missing_since.isnot(None)).count())
        self.assertTrue(pa.in_probation(self.db, self.provider.id))

    def test_a_partial_listing_marks_nothing(self):
        """Every category answered, but with only some of its titles (a
        cache, a server still filling): not "the rest are gone"."""
        self._switch()
        self.client.movies = {"1": self.full["1"][:5], "2": self.full["2"]}
        for _ in range(3):
            self.night()
        self.assertEqual(self.rows, self._rows())
        self.assertEqual(0, self.db.query(Movie).filter(Movie.provider_missing_since.isnot(None)).count())
        self.assertTrue(pa.in_probation(self.db, self.provider.id))

    def test_a_full_listing_ends_it_without_new_rows(self):
        self._switch()
        self.night()
        self.assertFalse(pa.in_probation(self.db, self.provider.id))
        self.assertEqual(self.rows, self._rows())            # no re-import, no duplicates
        self.assertEqual(2, self.db.query(Series).count())
        # the files follow the address (direct links), in place
        strm = Path(self.db.query(Movie).first().strm_path)
        self.assertTrue(strm.read_text(encoding="utf-8").startswith("http://cf.provider/movie/"))

    def test_a_mark_from_the_old_address_is_no_first_strike(self):
        self.client.movies = {"1": self.full["1"][1:], "2": self.full["2"]}
        self.night()                                         # Film 0 marked on the old address
        self.assertIsNotNone(self.movie(1000).provider_missing_since)
        self._switch()
        self.night()                                         # gone on the new one too: first strike there
        self.assertIsNotNone(self.movie(1000))
        self.night()                                         # second strike on the new address
        self.assertIsNone(self.movie(1000))
        self.assertEqual(39, self.db.query(Movie).count())

    def test_without_a_switch_nothing_changes(self):
        self.client.movies = {"1": self.full["1"][1:], "2": self.full["2"]}
        self.night()
        self.night()
        self.assertIsNone(self.movie(1000))


class OneTentacleAddress(unittest.TestCase):
    """Roadmap §1: one "Tentacle address" instead of six. An install that has
    none yet takes it from the first older setting that holds one, once."""

    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def test_taken_from_the_wizards_bare_host(self):
        mdb.set_setting(self.db, "webhook_host", "192.0.2.5:8888")
        mdb._one_tentacle_address(self.db)
        self.assertEqual("http://192.0.2.5:8888", mdb.tentacle_address(self.db))

    def test_the_vod_address_comes_first_and_stays(self):
        mdb.set_setting(self.db, "webhook_host", "192.0.2.5:8888")
        mdb.set_setting(self.db, "vod_base_url", "http://192.0.2.6:8888/")
        mdb._one_tentacle_address(self.db)
        self.assertEqual("http://192.0.2.6:8888", mdb.tentacle_address(self.db))
        self.assertEqual("http://192.0.2.6:8888/", mdb.get_setting(self.db, "vod_base_url"))

    def test_a_saved_address_is_kept(self):
        mdb.set_setting(self.db, "youtube_base_url", "https://tentacle.example.com")
        mdb.set_setting(self.db, "external_url", "http://192.0.2.7:8888")
        mdb._one_tentacle_address(self.db)
        self.assertEqual("https://tentacle.example.com", mdb.tentacle_address(self.db))

    def test_nothing_known_nothing_set(self):
        mdb._one_tentacle_address(self.db)
        self.assertEqual("", mdb.tentacle_address(self.db))


class TheAddressInUseInTheTable(_Db):
    def test_short_words_from_the_health_check(self):
        cases = [({"kind": "unreachable", "detail": "name not found"}, "name not found"),
                 ({"kind": "http", "code": 403, "step": "api"}, "refused (403)"),
                 ({"kind": "http", "code": 511, "step": "stream", "api_ok": True},
                  "login works, streams refused (511): refuses this connection, as for a VPN"),
                 ({"kind": "login"}, "login rejected"),
                 ({"kind": "expired", "detail": "2026-01-01"}, "account expired")]
        for outcome, word in cases:
            with self.subTest(word):
                self.assertEqual(word, pa._word_for(outcome))

    def test_recorded_by_the_hourly_check(self):
        pa.ensure_current(self.db, self.p)
        self.db.commit()
        failing = {"verdict": "fail", "kind": "unreachable", "detail": "no answer", "step": "api"}
        pa.record_in_use(self.db, self.p, failing)
        current = self.p.addresses[0]
        self.assertEqual("no answer", current.last_result["word"])
        self.assertFalse(current.history[-1]["ok"])
