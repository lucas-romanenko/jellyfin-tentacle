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
    return (
        addr.is_private        # 10/8, 172.16/12, 192.168/16, fc00::/7, ...
        or addr.is_loopback    # 127/8, ::1
        or addr.is_link_local  # 169.254/16 (incl. cloud metadata 169.254.169.254), fe80::/10
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


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
