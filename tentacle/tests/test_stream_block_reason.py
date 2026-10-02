"""A stream URL refused by the SSRF guard: the log says why, and the check
does not hold up the event loop.

Run from the tentacle/ directory:  python -m unittest discover -s tests

When the resolver failed (a DNS outage), the stream route logged "Blocked stream
URL (non-public host)" -- the same words as a refusal of a private address, and
with no address either way, so the log could not tell the two apart afterwards.
And the check's blocking getaddrinfo ran on the event loop: while the resolver
hung, every running stream and API call in the process waited with it.

What the guard accepts and refuses is unchanged: the property test compares
every decision with a frozen copy of the previous implementation.
"""
import asyncio
import ipaddress
import logging
import os
import random
import socket
import time
import unittest
from typing import Iterable, Optional
from unittest import mock
from urllib.parse import urlparse

from services import ssrf
from tmp_dirs import temp_dir

DNS = {
    "provider.example": ["93.184.216.34"],
    "cdn.example": ["93.184.216.35"],
    "tuliprox.lan": ["192.168.2.52"],
}
EAI_AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


def _gai(host, *a, **kw):
    answer = DNS.get(host)
    if answer is None:
        try:
            ipaddress.ip_address(host)
            answer = [host]
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    if isinstance(answer, BaseException):
        raise answer
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]


class _StreamRoute(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        p = mock.patch.object(ssrf.socket, "getaddrinfo", _gai)
        p.start()
        self.addCleanup(p.stop)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _channel(self, server_url, stream_url):
        from models.database import LiveChannel, Provider
        prov = Provider(name="P", server_url=server_url, username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        ch = LiveChannel(provider_id=prov.id, name="C", stream_url=stream_url)
        self.db.add(ch)
        self.db.commit()
        return ch.id

    async def _open(self, cid):
        import routers.livetv as livetv

        async def _inner(channel_id, ua, url, release, guard=None, **kw):
            release()
            return "streamed"

        with mock.patch.object(livetv, "_stream_proxy_inner", _inner):
            try:
                return await livetv.stream_proxy(cid, self.db)
            except Exception as e:           # HTTPException from the guard
                return e

    def _blocked_line(self, cid):
        with self.assertLogs("routers.livetv", level="WARNING") as logs:
            result = asyncio.run(self._open(cid))
        self.assertEqual(502, getattr(result, "status_code", None))
        lines = [l for l in logs.output if "Blocked stream URL" in l]
        self.assertEqual(1, len(lines), logs.output)
        return lines[0], result


class BlockedStreamSaysWhy(_StreamRoute):
    def test_a_resolver_failure_is_not_reported_as_a_non_public_host(self):
        cid = self._channel("http://provider.example", "http://cdn.example/live/u/p/28.ts")
        with mock.patch.dict(DNS, {"cdn.example": EAI_AGAIN}):
            line, result = self._blocked_line(cid)
        self.assertIn("cdn.example could not be resolved", line)
        self.assertIn("Temporary failure in name resolution", line)
        self.assertNotIn("non-public", line)
        # What the client gets is unchanged.
        self.assertEqual("Stream URL points to a non-public host", result.detail)

    def test_a_non_public_answer_names_the_address(self):
        cid = self._channel("http://provider.example", "http://cdn.example/live/u/p/28.ts")
        for answer, bad in ((["0.0.0.0"], "0.0.0.0"), (["127.0.0.1"], "127.0.0.1"),
                            (["93.184.216.35", "10.0.0.5"], "10.0.0.5"), (["::"], "::")):
            with self.subTest(answer=answer), mock.patch.dict(DNS, {"cdn.example": answer}):
                line, _ = self._blocked_line(cid)
                self.assertIn(f"not public: {bad}", line)
                for ip in answer:
                    self.assertIn(ip, line)

    def test_a_lan_provider_whose_name_stops_resolving_says_so(self):
        cid = self._channel("http://tuliprox.lan:8890", "http://tuliprox.lan:8890/live/u/p/1.ts")
        # Resolves when the guard is built, fails when the stream URL is checked.
        answers = iter([["192.168.2.52"], EAI_AGAIN])
        with mock.patch.dict(DNS, {}), \
                mock.patch.object(ssrf.socket, "getaddrinfo",
                                  lambda h, *a, **k: _gai_once(next(answers))):
            line, _ = self._blocked_line(cid)
        self.assertIn("tuliprox.lan could not be resolved", line)

    def test_the_url_credentials_stay_redacted(self):
        from services import log_redaction
        log_redaction.install()          # main.py installs it at start-up
        cid = self._channel("http://provider.example", "http://cdn.example/live/someuser/s3cretpw/28.ts")
        with mock.patch.dict(DNS, {"cdn.example": EAI_AGAIN}):
            line, _ = self._blocked_line(cid)
        self.assertNotIn("s3cretpw", line)
        self.assertNotIn("someuser", line)
        self.assertIn("could not be resolved", line)

    def test_a_public_stream_is_still_opened(self):
        cid = self._channel("http://provider.example", "http://cdn.example/live/u/p/28.ts")
        self.assertEqual("streamed", asyncio.run(self._open(cid)))


def _gai_once(answer):
    if isinstance(answer, BaseException):
        raise answer
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]


class AHangingResolverDoesNotFreezeTheServer(_StreamRoute):
    def test_other_coroutines_keep_running_during_the_check(self):
        cid = self._channel("http://provider.example", "http://cdn.example/live/u/p/28.ts")
        hang = 0.6

        def slow_gai(host, *a, **kw):
            if host == "cdn.example":
                time.sleep(hang)         # a resolver timing out, as in a DNS outage
                raise EAI_AGAIN
            return _gai(host)

        async def scenario():
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    await asyncio.sleep(0.02)
                    ticks += 1

            t = asyncio.create_task(ticker())
            await asyncio.sleep(0)
            result = await self._open(cid)
            stop.set()
            await t
            return result, ticks

        with mock.patch.object(ssrf.socket, "getaddrinfo", slow_gai):
            with self.assertLogs("routers.livetv", level="WARNING"):
                result, ticks = asyncio.run(scenario())
        self.assertEqual(502, getattr(result, "status_code", None))
        # 0.6 s at one tick per 20 ms is ~30; a blocked loop manages 0 or 1.
        self.assertGreaterEqual(ticks, 10, "the event loop was blocked while the resolver hung")


# ---------------------------------------------------------------------------
# Property: same decisions as before, one lookup per decision, honest reason.
# ---------------------------------------------------------------------------

def _old_ip_is_blocked(ip):
    return ssrf._ip_is_blocked(ip)   # unchanged by this fix


def _old_host_is_public(hostname):
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    return all(not _old_ip_is_blocked(ip) for ip in ips)


def _old_is_safe_url(url, allowed_hosts: Optional[Iterable[str]] = None):
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if allowed_hosts is not None:
        h = host.lower()
        allow = [a.lower() for a in allowed_hosts]
        if not any(h == a or h.endswith("." + a) for a in allow):
            return False
    return _old_host_is_public(host)


def _old_resolve(host):
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, OSError):
        return set()


def _old_lan_origin_guard(server_url):
    origin = ssrf._origin(server_url or "")
    if origin is None:
        return _old_is_safe_url
    ips = _old_resolve(origin[1])
    if not ips or not all(ssrf._ip_is_lan(ip) for ip in ips):
        return _old_is_safe_url

    def guard(url):
        if ssrf._origin(url or "") == origin:
            resolved = _old_resolve(origin[1])
            return bool(resolved) and all(ssrf._ip_is_lan(ip) for ip in resolved)
        return _old_is_safe_url(url)
    return guard


_POOL = ["93.184.216.34", "8.8.8.8", "2606:4700::1111", "10.0.0.5", "192.168.2.52", "172.16.0.9",
         "100.64.1.2", "127.0.0.1", "0.0.0.0", "::", "::1", "169.254.169.254", "fe80::1", "224.0.0.1",
         "198.18.0.1", "192.0.2.7", "fc00::5", "::ffff:10.0.0.5", "240.0.0.1", "not-an-ip"]
_HOSTS = ["provider.example", "cdn.example", "lan.example", "x.example"]
_URLS = ["http://{h}/live/u/p/1.ts", "https://{h}:8443/x.m3u8", "http://{h}:8890/hls/a/1.ts",
         "ftp://{h}/x", "http:///nohost", "", "http://{h}:99999/x", "http://[::1]:8890/x",
         "http://10.0.0.5:8890/x", "http://93.184.216.34/x"]


class _ScriptedDns:
    """getaddrinfo answering from a per-call script, so the old and the new
    implementation see the very same sequence of answers. Counts lookups."""

    def __init__(self, rng):
        self.rng = rng
        self.calls = 0
        self.table = {}

    def reroll(self):
        self.table = {}
        for h in _HOSTS:
            kind = self.rng.random()
            if kind < 0.12:
                self.table[h] = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
            elif kind < 0.18:
                self.table[h] = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            elif kind < 0.22:
                self.table[h] = []
            else:
                self.table[h] = self.rng.sample(_POOL, self.rng.randint(1, 3))

    def __call__(self, host, *a, **kw):
        self.calls += 1
        answer = self.table.get(host)
        if answer is None:
            try:
                ipaddress.ip_address(host)
                answer = [host]
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if isinstance(answer, BaseException):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]


class SameDecisionsAsBefore(unittest.TestCase):
    SEEDS = int(os.environ.get("SSRF_PROP_SEEDS", "1000"))

    def test_decisions_lookups_and_reasons(self):
        base = int(os.environ.get("SSRF_PROP_SEED", "0"))
        for seed in range(base, base + self.SEEDS):
            rng = random.Random(seed)
            dns = _ScriptedDns(rng)
            with mock.patch.object(ssrf.socket, "getaddrinfo", dns):
                for _ in range(8):
                    dns.reroll()
                    url = rng.choice(_URLS).format(h=rng.choice(_HOSTS))
                    server = rng.choice(["http://lan.example:8890", "http://provider.example",
                                         "http://10.0.0.5:8890", "", "nonsense"])
                    allowed = rng.choice([None, ["example"], ["cdn.example"], ["other.test"]])
                    msg = f"seed={seed} url={url!r} server={server!r} allowed={allowed} dns={dns.table}"

                    # is_safe_url: same answer, same number of lookups; explain agrees.
                    dns.calls = 0
                    old = _old_is_safe_url(url, allowed)
                    old_calls = dns.calls
                    dns.calls = 0
                    new = ssrf.is_safe_url(url, allowed)
                    self.assertIs(new, old, msg)
                    self.assertEqual(old_calls, dns.calls, msg)
                    dns.calls = 0
                    ok, why = ssrf.explain_safe_url(url, allowed)
                    self.assertEqual(old_calls, dns.calls, "explain made an extra lookup: " + msg)
                    self.assertIs(ok, old, msg)
                    self.assertEqual(ok, why == "", msg)
                    self._reason_is_honest(ok, why, url, allowed, dns, msg)

                    # lan_origin_guard: same guard choice, same verdicts, one lookup each.
                    dns.calls = 0
                    og = _old_lan_origin_guard(server)
                    old_build = dns.calls
                    dns.calls = 0
                    ng = ssrf.lan_origin_guard(server)
                    self.assertEqual(old_build, dns.calls, msg)
                    self.assertEqual(og is _old_is_safe_url, ng is ssrf.is_safe_url, msg)
                    target = rng.choice([url, server + "/live/u/p/1.ts"])
                    dns.calls = 0
                    ov = og(target)
                    oc = dns.calls
                    dns.calls = 0
                    nv = ng(target)
                    self.assertIs(nv, ov, msg + f" target={target!r}")
                    self.assertEqual(oc, dns.calls, msg)
                    dns.calls = 0
                    ok, why = ssrf.explain_url(ng, target)
                    self.assertEqual(oc, dns.calls, "explain made an extra lookup: " + msg)
                    self.assertIs(ok, ov, msg + f" target={target!r}")
                    self.assertEqual(ok, why == "", msg)

    def _reason_is_honest(self, ok, why, url, allowed, dns, msg):
        if ok:
            return
        host = ssrf._url_host(url, allowed)[0]   # None: refused before any lookup
        answer = dns.table.get(host) if host else None
        if isinstance(answer, BaseException):
            self.assertIn("could not be resolved", why, msg)
            self.assertNotIn("not public", why, msg)
        elif "not public: " in why:
            named = why.split("not public: ", 1)[1].split(", ")
            for ip in named:
                self.assertTrue(ssrf._ip_is_blocked(ip), f"{ip} named as not public: " + msg)


class PlainGuardsStillWork(unittest.TestCase):
    def test_explain_url_falls_back_for_a_guard_without_a_reason(self):
        self.assertEqual((True, ""), ssrf.explain_url(lambda u: True, "http://x/"))
        self.assertEqual((False, ""), ssrf.explain_url(lambda u: 0, "http://x/"))
        # A Mock has every attribute, .explain included: still just its verdict.
        self.assertEqual((True, ""), ssrf.explain_url(mock.MagicMock(return_value=True), "http://x/"))

    def test_public_provider_still_gets_is_safe_url_itself(self):
        with mock.patch.object(ssrf.socket, "getaddrinfo", _gai):
            self.assertIs(ssrf.lan_origin_guard("http://provider.example"), ssrf.is_safe_url)

    def test_is_safe_url_still_goes_through_host_is_public(self):
        with mock.patch.object(ssrf, "host_is_public", lambda host: True):
            self.assertTrue(ssrf.is_safe_url("http://name.that.does.not.resolve/x"))


if __name__ == "__main__":
    unittest.main()
