"""Titles Tentacle is still getting ready after Radarr/Sonarr imported them.

When a download is imported, Radarr/Sonarr drop it from their queue and fire a
webhook. Tentacle then has a minute or two of work before the title can be
played from the home screen: Jellyfin has to find it, its tags are pushed, it
is added to the playlists, and only then does the "ready to watch"
notification go out. Activity used to show the title under "Recently
downloaded" at the start of that window -- the database row exists from the
first moment -- and the notification arrived about two minutes later.

A webhook now registers the title here for the duration of that work, and
Activity reads it: while a title is being got ready it is shown as
"importing" with the downloads, and it joins "Recently downloaded" at the same
moment its notification is sent. In memory only: after a restart nothing is
in flight, so nothing is held back.
"""
import threading
import time

# A webhook thread that died without reporting back must not hide a title for
# ever. The longest real run is ~3 minutes (five Jellyfin lookups with growing
# waits, then up to 30 s for images).
STALE_SECONDS = 15 * 60
# Titles that became ready are remembered for as long as "Recently downloaded"
# can show them (24 h), plus a margin.
READY_MEMORY_SECONDS = 25 * 3600

_lock = threading.Lock()
_in_flight: "dict[int, dict]" = {}      # token -> {media_type, tmdb_id, title, episode, source, started}
_ready_at: "dict[tuple, float]" = {}    # (media_type, tmdb_id) -> when it last became ready
_next_token = 1


def begin(media_type: str, tmdb_id, title: str = "", episode: str = "", source: str = "") -> "int | None":
    """A webhook started getting this title ready. Returns a token for end()."""
    global _next_token
    if not tmdb_id:
        return None
    with _lock:
        token = _next_token
        _next_token += 1
        _in_flight[token] = {"media_type": media_type, "tmdb_id": int(tmdb_id), "title": title or "",
                             "episode": episode or "", "source": source, "started": time.monotonic()}
        return token


def end(token: "int | None", ready: bool) -> None:
    """The webhook finished. `ready`: the title is in Jellyfin and its
    notification has gone out."""
    if token is None:
        return
    with _lock:
        entry = _in_flight.pop(token, None)
        if entry is not None and ready:
            _ready_at[(entry["media_type"], entry["tmdb_id"])] = time.monotonic()


def _prune(now: float) -> None:
    for token in [t for t, e in _in_flight.items() if now - e["started"] > STALE_SECONDS]:
        del _in_flight[token]
    for key in [k for k, at in _ready_at.items() if now - at > READY_MEMORY_SECONDS]:
        del _ready_at[key]


def in_flight() -> list:
    """Copies of the titles being got ready right now, oldest first."""
    with _lock:
        _prune(time.monotonic())
        return sorted((dict(e) for e in _in_flight.values()), key=lambda e: e["started"])


def held_back(media_type: str, tmdb_id) -> bool:
    """True while this title is being got ready and has not been ready yet.

    A title that was already ready stays visible while a later episode is
    being got ready (no flicker out of "Recently downloaded" and back)."""
    key = (media_type, int(tmdb_id or 0))
    with _lock:
        _prune(time.monotonic())
        if key in _ready_at:
            return False
        return any((e["media_type"], e["tmdb_id"]) == key for e in _in_flight.values())


def reset() -> None:
    """For tests."""
    with _lock:
        _in_flight.clear()
        _ready_at.clear()
