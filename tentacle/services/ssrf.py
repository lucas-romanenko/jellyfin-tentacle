"""SSRF protection helpers.

These validate that an outbound URL targets a *public* host before the server
fetches it, so unauthenticated/redirect-following proxy endpoints (Live TV stream
proxy, TVDB image proxy) cannot be tricked into reaching internal services
(Jellyfin/Radarr/Sonarr on the LAN), loopback, or cloud metadata (169.254.169.254).

Note: this resolves DNS and checks the resolved IPs. It does not fully defend
against DNS-rebinding (a name resolving to a public IP at check time and a private
IP at connect time); for the internal-home-server threat model this is an
acceptable, large reduction in attack surface and matches the audit recommendation.
"""

import ipaddress
import logging
import socket
from typing import Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → treat as unsafe
    if (
        addr.is_private        # 10/8, 172.16/12, 192.168/16, fc00::/7, ...
        or addr.is_loopback    # 127/8, ::1
        or addr.is_link_local  # 169.254/16 (incl. cloud metadata 169.254.169.254), fe80::/10
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    # Catch-all for ranges the predicates above miss. The important one here is
    # RFC 6598 shared address space 100.64.0.0/10 — it is neither `is_private`
    # nor `is_reserved`, but it is the CGNAT range Tailscale/Headscale hand out,
    # so on a home server it addresses exactly the internal peers this guard is
    # meant to keep unreachable. `is_global` is False for it and for every other
    # non-globally-routable block (192.0.0.0/24, 198.18/15, 2001:db8::/32, ...).
    return not addr.is_global


def host_is_public(hostname: str) -> bool:
    """Resolve hostname; return True only if every resolved IP is public."""
    if not hostname:
        return False
    # A bare IP literal still resolves through getaddrinfo, so this covers
    # http://169.254.169.254/ and http://127.0.0.1/ as well as names.
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    return all(not _ip_is_blocked(ip) for ip in ips)


def is_safe_url(url: str, allowed_hosts: Optional[Iterable[str]] = None) -> bool:
    """Return True if `url` is http(s), (optionally) on an allowlisted host, and
    resolves only to public IP addresses.

    allowed_hosts matches by exact host or dotted-suffix (so "thetvdb.com" allows
    "artworks.thetvdb.com" but not "thetvdb.com.evil.test").
    """
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
    return host_is_public(host)


# Where a self-hosted re-streamer can legitimately live: RFC 1918, IPv6 ULA
# and RFC 6598 CGNAT (Tailscale/Headscale). Listed explicitly rather than via
# ipaddress.is_private, which also covers documentation, benchmarking and
# other special-purpose ranges.
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
))


def _ip_is_lan(ip: str) -> bool:
    """A private LAN/VPN address. Never loopback, link-local (which includes
    cloud metadata), multicast or unspecified — none of those is in _LAN_NETS."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr.version == n.version and addr in n for n in _LAN_NETS)


def _resolve(host: str) -> set:
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, OSError):
        return set()


def _origin(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None
    return p.scheme, p.hostname.lower(), port


def lan_origin_guard(server_url: str):
    """URL check for a provider the admin deliberately configured on the LAN.

    A local IPTV re-streamer (tuliprox, xTeVe, Threadfin) presents an Xtream or
    M3U source on a LAN address, so every stream URL it hands out is private and
    is_safe_url() refuses all of them. When the provider's own configured
    server_url resolves ONLY to LAN addresses, URLs on exactly that origin
    (scheme, host and port) are allowed. Everything else — another port on the
    same box (Jellyfin on :8096), another LAN host, loopback, link-local and
    metadata addresses — still goes through is_safe_url(), so a redirect or a
    playlist line pointing elsewhere is blocked as before.

    A provider on a public host gets plain is_safe_url(): a public provider
    hostname that later re-resolves to a private address is not trusted.
    """
    origin = _origin(server_url or "")
    if origin is None:
        return is_safe_url
    ips = _resolve(origin[1])
    if not ips or not all(_ip_is_lan(ip) for ip in ips):
        return is_safe_url

    def guard(url: str) -> bool:
        if _origin(url or "") == origin:
            resolved = _resolve(origin[1])
            return bool(resolved) and all(_ip_is_lan(ip) for ip in resolved)
        return is_safe_url(url)

    return guard
