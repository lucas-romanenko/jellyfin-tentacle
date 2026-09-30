"""The suite runs without the network: no DNS, no connections off this machine.

scripts/check (and so CI) starts the suite through this file, which installs
the guard below first. Tests used to send real requests to TMDB (the built-in
token got a 401, which the code copes with) and resolve api.trakt.tv for the
SSRF check, so they passed with a network and failed in a sandbox without
one. With the guard every run behaves the same, and a new test that reaches
out fails everywhere, CI included, instead of only offline.

- DNS: IP literals and localhost resolve as usual; the few public names the
  code checks by address (FAKE_DNS) get a fixed public address; any other
  name fails the way it does with no resolver (socket.gaierror).
- Connections: loopback and unix sockets only (tests run local servers on
  127.0.0.1). Anything else raises ConnectionRefusedError, which requests
  and httpx report as a connection error.
- No proxy: the *_PROXY variables are dropped, so a sandbox's proxy can't
  answer in the network's place.

A test that really needs the network is marked @live and runs only with
TENTACLE_LIVE_TESTS=1, which also leaves the guard out. `make check` never
sets it.

Mock a service in the test itself (for TMDB: no_tmdb(self)); never widen
FAKE_DNS to let a request out.
"""
import ipaddress
import os
import socket
import sys
import unittest
from unittest import mock

LIVE = os.environ.get("TENTACLE_LIVE_TESTS") == "1"

# Names the code resolves only to check that they are public (services/ssrf.py);
# the address is never connected to.
FAKE_DNS = {
    "trakt.tv": "104.16.0.1",
    "api.trakt.tv": "104.16.0.1",
}

_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def live(test):
    """Mark a test (or class) that needs the real network."""
    return unittest.skipUnless(LIVE, "needs the network: TENTACLE_LIVE_TESTS=1")(test)


def _is_local(host) -> bool:
    if host is None:
        return True
    host = host.decode() if isinstance(host, bytes) else str(host)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _is_literal(host) -> bool:
    try:
        ipaddress.ip_address(str(host).split("%")[0])
        return True
    except ValueError:
        return False


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host is None or _is_local(host) or _is_literal(host):
        return _real_getaddrinfo(host, port, family, type, proto, flags)
    name = (host.decode() if isinstance(host, bytes) else str(host)).lower().rstrip(".")
    if name in FAKE_DNS:
        return _real_getaddrinfo(FAKE_DNS[name], port, family, type, proto, flags)
    raise socket.gaierror(socket.EAI_NONAME, "no DNS in the tests (tests/hermetic.py): %s" % name)


def _check(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address[0]):
        raise ConnectionRefusedError("no network in the tests (tests/hermetic.py): %s" % (address,))


def _connect(self, address):
    _check(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(self, address)
    return _real_connect_ex(self, address)


def install():
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(var, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    socket.getaddrinfo = _getaddrinfo
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex


def no_tmdb(test):
    """TMDB answers nothing (as it does for a refused token), for one test."""
    import services.tmdb as tmdb
    p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
    p.start()
    test.addCleanup(p.stop)


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())  # as `python -m unittest` does: the modules under test
    if not LIVE:
        install()
    unittest.main(module=None, argv=[sys.argv[0]] + sys.argv[1:])
