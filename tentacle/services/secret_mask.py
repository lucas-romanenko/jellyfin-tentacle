"""How a stored secret is shown back, and how a Save recognises it unchanged.

A masked API key keeps only the last four characters ("••••wxyz"); a
password or shared secret, and any value of eight characters or fewer, is
shown as "••••" alone. A proxy URL keeps its address and user and hides the password
(http://user:••••@host:port).

A Save that sends back exactly what was shown means "unchanged". The older
"first 8 ... last 4" form is recognised the same way, for pages that still
hold a value masked that way; any other value is saved as typed, even one
that contains "...".
"""
from urllib.parse import urlsplit, urlunsplit

MASK = "••••"


def mask(value: str, whole: bool = False) -> str:
    """whole: show nothing of it (passwords, shared secrets)."""
    value = value or ""
    if not value:
        return ""
    return MASK + value[-4:] if len(value) > 8 and not whole else MASK


def _legacy(value: str) -> str:
    return value[:8] + "..." + value[-4:]


def is_shown_form(submitted: str, stored: str) -> bool:
    """True when `submitted` is what a masked listing showed for `stored`."""
    if not submitted or not stored:
        return False
    return submitted in (mask(stored), mask(stored, whole=True), _legacy(stored))


def looks_masked(value: str) -> bool:
    """A form value that is a mask, not something the user typed."""
    return bool(value) and (value.startswith(MASK) or "..." in value)


def mask_url_login(url: str) -> str:
    """The URL with its password (if any) replaced by the mask."""
    if not url or "@" not in url:
        return url or ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if parts.password is None:
        return url
    netloc = f"{parts.username}:{MASK}@{parts.netloc.rsplit('@', 1)[1]}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def restore_url_login(submitted: str, stored: str) -> str:
    """A proxy URL sent back with the masked password gets the stored one again.

    Only when the stored URL has a password for the same user; anything else
    is returned as sent."""
    if not submitted or MASK not in submitted or not stored:
        return submitted
    try:
        new, old = urlsplit(submitted), urlsplit(stored)
    except ValueError:
        return submitted
    if new.password != MASK or old.password is None or new.username != old.username:
        return submitted
    netloc = f"{new.username}:{old.password}@{new.netloc.rsplit('@', 1)[1]}"
    return urlunsplit((new.scheme, netloc, new.path, new.query, new.fragment))
