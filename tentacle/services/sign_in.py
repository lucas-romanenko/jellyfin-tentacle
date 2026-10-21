"""The sign-in screen: whether the household's names may be shown to this
browser, and signing in with a Quick Connect code.

Names only at home. A server reached from the internet would otherwise tell
anyone who opens the address who lives there (and which account names to
try passwords on). On the home network the picker shows everyone Jellyfin
shows on its login screen; from anywhere else people type their name, or
use a code. No setting: signing in from outside is rare (a session lasts a
month) and typing a name once is a small price. The plugin applies the same
rule to Jellyfin's own login page, with Jellyfin's idea of its local network.
"""
import ipaddress
import logging
import secrets
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Private ranges a home network uses, plus CGNAT 100.64.0.0/10, which is where
# Tailscale puts its devices (a "home" a family member's phone reaches it on).
_HOME_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
)]

# Headers a proxy or tunnel names the real browser in. Any one of them naming
# an address outside the home makes the request an outside one: a tunnel
# (Cloudflare) or reverse proxy connects from inside the home, so the
# connection itself always looks local.
_FORWARD_HEADERS = ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-forwarded-for", "forwarded")


def is_home_address(addr: Optional[str]) -> bool:
    if not addr:
        return False
    addr = addr.strip().strip('"')
    if addr.startswith("[") and "]" in addr:          # [v6]:port
        addr = addr[1:addr.index("]")]
    elif addr.count(":") == 1:                         # v4:port
        addr = addr.split(":")[0]
    addr = addr.split("%")[0]                          # fe80::1%eth0
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _HOME_NETS)


def _forwarded_for(value: str) -> list:
    """The for= addresses of an RFC 7239 Forwarded header."""
    out = []
    for element in value.split(","):
        for pair in element.split(";"):
            k, _, v = pair.strip().partition("=")
            if k.strip().lower() == "for":
                out.append(v)
    return out or [value]


def at_home(request) -> bool:
    """Whether this request comes from the home network (or Tailscale)."""
    peer = request.client.host if getattr(request, "client", None) else None
    if not is_home_address(peer):
        return False
    for name in _FORWARD_HEADERS:
        value = request.headers.get(name)
        if value is None:
            continue
        addrs = _forwarded_for(value) if name == "forwarded" else value.split(",")
        if not all(is_home_address(a) for a in addrs):
            return False
    return True


# ── Quick Connect ───────────────────────────────────────────────────────────
# The browser gets a code to approve on a device where someone is signed in
# (Tentacle → "Sign in a TV", or Jellyfin's Quick Connect page). Jellyfin's
# secret stays here: the browser holds only a random handle to it.

_QC_TTL = 600          # Jellyfin forgets a request after 10 minutes too
_QC_MAX = 200          # pending requests kept at once (unauthenticated route)
_pending: dict = {}    # handle -> (jellyfin secret, created)
_lock = threading.Lock()


def _prune(now: float) -> None:
    for h in [h for h, (_, t) in _pending.items() if now - t > _QC_TTL]:
        del _pending[h]


def remember(secret: str) -> str:
    now = time.monotonic()
    with _lock:
        _prune(now)
        if len(_pending) >= _QC_MAX:
            oldest = min(_pending, key=lambda h: _pending[h][1])
            del _pending[oldest]
        handle = secrets.token_urlsafe(24)
        _pending[handle] = (secret, now)
        return handle


def secret_for(handle: str) -> Optional[str]:
    with _lock:
        _prune(time.monotonic())
        entry = _pending.get(handle or "")
        return entry[0] if entry else None


def forget(handle: str) -> None:
    with _lock:
        _pending.pop(handle or "", None)
