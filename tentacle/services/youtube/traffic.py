"""Every request Tentacle makes to YouTube and Google goes through here.

Google's bot detection keys on the shape of the traffic from one address more
than on its volume: scripted, cookieless requests at exactly regular times, a
fresh connection (and DNS lookup) for every request, and bursts. One household
running Tentacle got its IP captcha'd for Google Search that way. This module
is where Tentacle stays polite, for every install, with nothing to configure:

- **One app-wide pause after a bot check.** A 429, "confirm you're not a bot",
  a captcha or Google's /sorry/ page stops every YouTube request until the pause
  ends: an hour, doubling for each block in a row, up to a day. It survives a
  restart, so a restart loop can't hammer on. Streams already found keep playing.
- **One shared connection pool** for YouTube's hosts, instead of a new
  connection per request.
- **An optional proxy** for YouTube traffic only (for example a VPN container's
  HTTP proxy, http://gluetun:8888). Google ties a stream URL to the IP that asked
  for it, so yt-dlp, the stream proxy and ffmpeg all use it together.
- **Random jitter** for every schedule and spacing.
- **An hourly count** of outbound requests, logged in one line.
"""
import contextlib
import contextvars
import logging
import os
import random
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The pause after a bot check: this long for the first block, doubling for each
# block in a row (a request that succeeds after a pause resets it), capped.
PAUSE_START_SECONDS = 3600
PAUSE_MAX_SECONDS = 24 * 3600

# The shortest scheduled interval. A light check costs one small request per
# channel, but a channel checked more often than this gains nothing a viewer
# would notice, and regularity is what gets an address flagged.
MIN_INTERVAL_MINUTES = 30
DEFAULT_INTERVAL_MINUTES = 60

# How often settings are re-read from the database by the hot paths.
_SETTINGS_TTL = 60

_lock = threading.RLock()
_settings = {"proxy": "", "proxy_error": "", "api_key": "", "loaded_at": 0.0}
# While a saved proxy can't be used, YouTube requests are held and looked at
# again this often (the settings cache), so a fixed proxy takes over at once.
PROXY_HOLD_SECONDS = 60
_pause = {"until": 0.0, "count": 0, "reason": "", "loaded": False}

_purpose: contextvars.ContextVar = contextvars.ContextVar("youtube_purpose", default="background")
_counts: Counter = Counter()          # (purpose, host group) -> requests this hour
_window_started = time.time()
_last_report: dict = {"at": None, "counts": {}, "total": 0}

_client = None
_client_proxy = None


# ── Settings ────────────────────────────────────────────────────────────────

def normalize_proxy(value: str) -> str:
    """"" or an http(s)://host:port proxy URL. Raises ValueError for anything else.

    Only HTTP proxies: httpx needs an extra package for SOCKS, and a VPN
    container's built-in HTTP proxy (gluetun's :8888) is what this is for.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Use an HTTP proxy address such as http://gluetun:8888 "
                         "(SOCKS proxies are not supported).")
    try:
        parsed.port
    except ValueError:
        raise ValueError(f"'{value}' has an invalid port.")
    return value.rstrip("/")


def configure(proxy: str = None, api_key: str = None) -> None:
    """Set the proxy and API key now (after a settings save, or in tests)."""
    with _lock:
        if proxy is not None:
            _settings["proxy"] = normalize_proxy(proxy)
            _settings["proxy_error"] = ""
        if api_key is not None:
            _settings["api_key"] = (api_key or "").strip()
        _settings["loaded_at"] = time.time()


def _refresh_settings() -> None:
    if time.time() - _settings["loaded_at"] < _SETTINGS_TTL:
        return
    try:
        from models.database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            proxy = get_setting(db, "youtube_proxy", "") or ""
            key = get_setting(db, "youtube_api_key", "") or ""
        finally:
            db.close()
        # A saved proxy that can't be used holds every YouTube request. Using
        # no proxy instead sent everything out from the household's own
        # address, which is what the proxy was set up to prevent (#244).
        error = ""
        try:
            proxy = normalize_proxy(proxy)
        except ValueError as e:
            error = str(e)
            if error != _settings.get("proxy_error"):
                logger.warning(f"[YouTube] The saved proxy can't be used, so nothing is sent to "
                               f"YouTube until it is fixed on the YouTube page: {e}")
            proxy = ""
        with _lock:
            _settings.update(proxy=proxy, proxy_error=error, api_key=key.strip(), loaded_at=time.time())
    except Exception as e:
        logger.debug(f"[YouTube] Could not read traffic settings: {e}")
        _settings["loaded_at"] = time.time()


def proxy() -> str:
    """The proxy for YouTube traffic, or ""."""
    _refresh_settings()
    return _settings["proxy"]


def proxy_problem() -> str:
    """Why the saved proxy can't be used, or "" (none saved, or it is fine)."""
    _refresh_settings()
    return _settings["proxy_error"]


def _proxy_held():
    """The error that stops a request going out while the proxy can't be used."""
    problem = proxy_problem()
    if not problem:
        return None
    from services.youtube.errors import PausedByBotCheck
    return PausedByBotCheck(f"YouTube requests are held: the saved proxy can't be used ({problem}) "
                            f"Fix or clear it on the YouTube page.")


def api_key() -> str:
    """The YouTube Data API key, or "" (then RSS feeds and yt-dlp are used)."""
    _refresh_settings()
    return _settings["api_key"]


def interval_minutes(value) -> int:
    """The scheduled interval a stored setting means, never below the minimum."""
    try:
        minutes = int(str(value).strip() or DEFAULT_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, minutes)


# ── Jitter ──────────────────────────────────────────────────────────────────

def jitter(seconds: float, fraction: float = 0.2) -> float:
    """`seconds`, moved at random by up to `fraction` either way."""
    return max(0.0, seconds * random.uniform(1 - fraction, 1 + fraction))


def spacing(low: float, high: float) -> float:
    """A random wait between two requests, so they never come at a fixed beat."""
    return random.uniform(low, high)


# ── The pause after a bot check ─────────────────────────────────────────────

def _load_pause() -> None:
    if _pause["loaded"]:
        return
    _pause["loaded"] = True
    try:
        from models.database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            _pause["until"] = float(get_setting(db, "youtube_pause_until", "0") or 0)
            _pause["count"] = int(get_setting(db, "youtube_pause_count", "0") or 0)
            _pause["reason"] = get_setting(db, "youtube_pause_reason", "") or ""
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[YouTube] Could not read the saved pause: {e}")


def _save_pause() -> None:
    try:
        from models.database import SessionLocal, set_setting
        db = SessionLocal()
        try:
            set_setting(db, "youtube_pause_until", str(int(_pause["until"])))
            set_setting(db, "youtube_pause_count", str(_pause["count"]))
            set_setting(db, "youtube_pause_reason", _pause["reason"])
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[YouTube] Could not save the pause: {e}")


def paused() -> float:
    """Seconds left in the pause after a bot check, or 0.

    Also non-zero while a saved proxy can't be used: every check and refresh
    stands down then, exactly as during a pause, instead of going out direct.
    """
    held = PROXY_HOLD_SECONDS if proxy_problem() else 0.0
    with _lock:
        _load_pause()
        return max(held, _pause["until"] - time.time())


def pause_state() -> dict:
    with _lock:
        _load_pause()
        left = max(0.0, _pause["until"] - time.time())
        return {
            "paused": left > 0,
            "seconds_left": int(left),
            "until": datetime.fromtimestamp(_pause["until"]).isoformat(timespec="minutes") if left else None,
            "blocks_in_a_row": _pause["count"],
            "reason": _pause["reason"] if left else "",
        }


def record_block(reason: str) -> float:
    """YouTube asked for a bot check: pause every YouTube request. Returns the
    pause end (epoch seconds). A request that was already in flight when the
    pause began does not extend it."""
    with _lock:
        _load_pause()
        now = time.time()
        if _pause["until"] > now:
            return _pause["until"]
        count = _pause["count"] + 1
        delay = jitter(min(PAUSE_START_SECONDS * 2 ** (count - 1), PAUSE_MAX_SECONDS), 0.1)
        _pause.update(until=now + delay, count=count, reason=(reason or "")[:300])
        _save_pause()
        until = _pause["until"]
    logger.warning(
        f"[YouTube] YouTube asked this server to prove it is not a bot ({(reason or '')[:160]}). "
        f"Pausing ALL YouTube requests for {int(delay // 60)} min, until "
        f"{datetime.fromtimestamp(until):%H:%M}. Streams already found keep playing. "
        f"Block {count} in a row: the pause doubles each time, up to 24 h."
    )
    return until


def note_success() -> None:
    """A YouTube request worked: after a pause, the next block starts short again."""
    with _lock:
        _load_pause()
        if _pause["count"] and _pause["until"] <= time.time():
            _pause.update(count=0, reason="")
            _save_pause()
            logger.info("[YouTube] YouTube answers normally again after the pause")


def clear_pause() -> None:
    """End a pause now (an admin who changed the proxy, or tests)."""
    with _lock:
        _pause.update(until=0.0, count=0, reason="", loaded=True)
        _save_pause()


def ensure_allowed() -> None:
    """Raise PausedByBotCheck while the pause after a bot check runs, or while
    the saved proxy can't be used."""
    held = _proxy_held()
    if held:
        raise held
    left = paused()
    if left:
        from services.youtube.errors import PausedByBotCheck
        raise PausedByBotCheck(
            f"YouTube requests are paused after a bot check; they resume in {int(left // 60) + 1} min")


# ── Counting ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def purpose(name: str):
    """Label the requests made inside this block ("playback", "artwork", ...)."""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def _host_group(host: str) -> str:
    host = (host or "").lower()
    if host.endswith("googlevideo.com"):
        return "googlevideo"
    if host.endswith("ytimg.com") or host.endswith("ggpht.com"):
        return "ytimg"
    if host.endswith("googleapis.com"):
        return "api"
    if host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("youtube-nocookie.com"):
        return "youtube.com"
    if host.endswith("google.com"):
        return "google.com"
    return host or "?"


def count(url: str) -> None:
    host = urlparse(str(url)).hostname or ""
    with _lock:
        _counts[(_purpose.get(), _host_group(host))] += 1


def counts() -> dict:
    """Requests so far in the current hour, as {purpose: {host group: n}}."""
    out: dict = {}
    with _lock:
        for (p, g), n in _counts.items():
            out.setdefault(p, {})[g] = n
    return out


def summary_line(snapshot: dict) -> str:
    total = sum(sum(g.values()) for g in snapshot.values())
    parts = []
    for p in sorted(snapshot):
        groups = ", ".join(f"{g} {n}" for g, n in sorted(snapshot[p].items()))
        parts.append(f"{p} {sum(snapshot[p].values())} ({groups})")
    return f"{total} request(s)" + (": " + "; ".join(parts) if parts else "")


def hourly_report() -> Optional[str]:
    """Log last hour's outbound requests in one line, then start a new hour.

    Logged whenever the YouTube feature is on, zeros included, so the effect of
    a setting shows up in the log without anything else to do.
    """
    global _window_started
    with _lock:
        snapshot = counts()
        _counts.clear()
        started, _window_started = _window_started, time.time()
    _last_report.update(at=datetime.now().isoformat(timespec="minutes"), counts=snapshot,
                        total=sum(sum(g.values()) for g in snapshot.values()))
    enabled = True
    try:
        from models.database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            enabled = get_setting(db, "youtube_enabled", "false") == "true"
        finally:
            db.close()
    except Exception:
        pass
    if not enabled and not snapshot:
        return None
    minutes = max(1, int((time.time() - started) / 60))
    state = pause_state()
    line = (f"[YouTube] Requests to YouTube/Google in the last {minutes} min: {summary_line(snapshot)}"
            + (f". Paused after a bot check until {state['until']}" if state["paused"] else ""))
    logger.info(line)
    return line


def last_report() -> dict:
    return dict(_last_report)


# ── HTTP ────────────────────────────────────────────────────────────────────

def _on_request(request) -> None:
    count(str(request.url))


def _on_response(response) -> None:
    host = (response.url.host or "").lower()
    group = _host_group(host)
    if group == "api":
        return
    if group == "google.com" and response.url.path.startswith("/sorry"):
        record_block(f"Google's /sorry/ page for {response.request.url.host}")
    elif response.status_code == 429:
        record_block(f"HTTP 429 from {host}")
    elif group == "youtube.com" and response.status_code < 400:
        note_success()


def http_client():
    """The shared HTTP client for YouTube's and Google's hosts.

    Keep-alive connections are reused across requests and threads, requests are
    counted, 429s and captchas start the pause, and the proxy setting applies.
    Callers pass their own timeout per request, and close their responses, never
    the client.
    """
    global _client, _client_proxy
    import httpx
    held = _proxy_held()
    if held:
        raise held
    wanted = proxy()
    with _lock:
        if _client is None or wanted != _client_proxy:
            _client = httpx.Client(
                follow_redirects=True,
                proxy=wanted or None,
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16,
                                    keepalive_expiry=60),
                event_hooks={"request": [_on_request], "response": [_on_response]},
            )
            # The old client is not closed here: a stream may still be reading
            # from it. It is released when its last response is closed.
            _client_proxy = wanted
        return _client


def ffmpeg_proxy_args() -> list:
    """ffmpeg input options that route one -i through the proxy, if one is set."""
    held = _proxy_held()
    if held:
        raise held
    p = proxy()
    return ["-http_proxy", p] if p else []


def ydl_options() -> dict:
    """Options every yt-dlp call gets: the proxy, and a cache that survives restarts."""
    held = _proxy_held()
    if held:
        raise held
    opts = {}
    p = proxy()
    if p:
        opts["proxy"] = p
    cache = Path(os.getenv("DATA_DIR", "/data")) / "cache" / "yt-dlp"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        opts["cachedir"] = str(cache)
    except OSError:
        pass
    return opts


def reset_for_tests() -> None:
    global _client, _client_proxy, _window_started
    with _lock:
        _counts.clear()
        _settings.update(proxy="", proxy_error="", api_key="", loaded_at=time.time())
        _pause.update(until=0.0, count=0, reason="", loaded=True)
        _client, _client_proxy = None, None
        _window_started = time.time()
