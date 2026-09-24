"""Signed, stable identifiers for provider VOD served through Tentacle.

A `.strm` that points straight at the provider (`{server}/movie/{user}/{pass}/
{id}.mkv`) hands every client the account's credentials and opens a provider
connection Tentacle never sees -- so a movie started on the TV can make the
provider refuse the recording that is running (HTTP 509 on the open stream).
Served through `GET /api/vod/{kind}/{token}.{ext}` instead, the play takes a
"vod" lease from the same broker as live TV (a recording outranks it), the
credentials stay on the server, and a dropped upstream is resumed with a
Range request from where it stopped.

The token is `{provider_id}.{stream_id}.{signature}`: the ids are in the
clear (so the health sweep and the wrong-match keys can still tell which
title a file is, exactly as with the direct URL), and the signature -- an
HMAC over provider, kind, stream id and container with a per-install secret
-- means the public route serves only what the sync itself wrote, never an
arbitrary id somebody types in. The same inputs always give the same token,
so a `.strm` written once stays valid for the life of the title.
"""
import hashlib
import hmac
import re
import secrets
from typing import Optional

from models.database import get_setting, set_setting

SECRET_SETTING = "vod_token_secret"
KINDS = ("movie", "series")
_SIG_LEN = 20   # hex chars = 80 bits: unguessable, short enough for a filename

# /api/vod/movie/12.345.abcdef0123456789abcd.mkv
_TOKEN_URL = re.compile(r"/api/vod/(movie|series)/(\d+)\.(\d+)\.([0-9a-f]+)\.([A-Za-z0-9]+)(?:[?#]|$)")


def token_secret(db) -> str:
    """The per-install signing secret; made once and kept."""
    current = (get_setting(db, SECRET_SETTING, "") or "").strip()
    if current:
        return current
    made = secrets.token_hex(32)
    set_setting(db, SECRET_SETTING, made)
    return made


def _signature(secret: str, provider_id: int, kind: str, stream_id, container: str) -> str:
    msg = f"{int(provider_id)}|{kind}|{int(stream_id)}|{container.lower()}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:_SIG_LEN]


def mint(secret: str, provider_id: int, kind: str, stream_id, container: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown VOD kind {kind!r}")
    return f"{int(provider_id)}.{int(stream_id)}.{_signature(secret, provider_id, kind, stream_id, container)}"


def url(base_url: str, secret: str, provider_id: int, kind: str, stream_id, container: str) -> str:
    """The address written into a .strm."""
    container = (container or "mp4").strip(". ").lower() or "mp4"
    return f"{base_url.rstrip('/')}/api/vod/{kind}/{mint(secret, provider_id, kind, stream_id, container)}.{container}"


def parse(kind: str, token_file: str) -> Optional[dict]:
    """`{provider_id, stream_id, sig, container}` from the path tail
    `{provider_id}.{stream_id}.{sig}.{ext}`, or None if it is not that shape.
    Does NOT check the signature; see verify()."""
    if kind not in KINDS:
        return None
    m = re.fullmatch(r"(\d+)\.(\d+)\.([0-9a-f]+)\.([A-Za-z0-9]+)", token_file or "")
    if not m:
        return None
    return {"provider_id": int(m.group(1)), "stream_id": int(m.group(2)),
            "sig": m.group(3), "container": m.group(4).lower()}


def verify(secret: str, kind: str, parsed: dict) -> bool:
    expect = _signature(secret, parsed["provider_id"], kind, parsed["stream_id"], parsed["container"])
    return hmac.compare_digest(expect, parsed["sig"])


class Links:
    """Writes the Tentacle VOD address for one provider's titles. Given to
    the sync client (`client.vod_links`) when `vod_via_tentacle_enabled` is
    on; absent, the client writes direct provider URLs as before."""

    def __init__(self, base_url: str, secret: str, provider_id: int):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.provider_id = int(provider_id)

    def movie(self, stream_id, container: str = "mp4") -> str:
        return url(self.base_url, self.secret, self.provider_id, "movie", stream_id, container)

    def episode(self, episode_id, container: str = "mp4") -> str:
        return url(self.base_url, self.secret, self.provider_id, "series", episode_id, container)


def stream_id_in_url(url_text: str) -> Optional[tuple]:
    """`(kind, stream_id)` if this is a Tentacle VOD URL, else None -- for
    code that keys titles by their provider stream id (blocks, health)."""
    m = _TOKEN_URL.search(url_text or "")
    return (m.group(1), int(m.group(3))) if m else None


def is_vod_url(url_text: str) -> bool:
    return stream_id_in_url(url_text) is not None
