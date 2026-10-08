"""
Tentacle - Provider health

Every hour, per IPTV provider: log in to its API and open one short test
stream. A provider that stops answering (403 from an IP block, an expired
account, a moved URL) used to break every stream with nothing saying so.

- A failure is confirmed by one more check RETRY_DELAY_SECONDS later before
  it counts, so one blip raises no banner and no message.
- The state is kept in the setting `provider_health` (JSON, keyed by
  provider id), so a restart neither forgets an outage nor re-sends it.
- One Pushover message when a provider starts failing, one when it works
  again; nothing on the hours in between. A send that failed is tried again
  on the next run.
- Each check is a real connection on the account, and a one-connection
  account answers a second one by cutting the first (often a recording,
  services/provider_activity.py). So a run checks nothing while Tentacle
  proxies live TV or a protected recording; the next hour tries again.

Reasons are short and plain, and never carry a URL or a login: they are
shown on the dashboard, sent to the phone and served unauthenticated by
/api/provider-status (without the provider names).
"""

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from models.database import (
    SessionLocal, get_setting, set_setting, log_activity,
    Provider, Movie, LiveChannel, StreamHealth,
)
from services.log_redaction import redact

logger = logging.getLogger(__name__)

SETTING_KEY = "provider_health"
RETRY_DELAY_SECONDS = 120
API_TIMEOUT = (10, 20)
STREAM_TIMEOUT = (10, 15)
PROBE_PAUSE_SECONDS = 3.0
MAX_MOVIE_CANDIDATES = 3
MAX_CHANNEL_CANDIDATES = 2
M3U_HEAD_BYTES = 65536
DEFAULT_UA = "TiviMate/4.7.0 (Linux; Android 12)"
# "Not right now": the account is over its connection limit or rate-limited.
# No verdict either way; the state stays as it was.
BUSY_STATUSES = {429, 509}
GONE_STATUSES = {404, 410}

# The hourly job and "Check now" never run at once: two checks are two
# connections, which is what a one-connection account cuts a stream for.
_run_lock = threading.Lock()


def _now() -> datetime:
    """Local time (the container's TZ), timezone-aware."""
    return datetime.now().astimezone()


def _parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def _clock(since: datetime, now: datetime) -> str:
    """07:50 today, "Oct 7 07:50" on an earlier day."""
    if since is None:
        return "?"
    since = since.astimezone(now.tzinfo) if since.tzinfo else since
    if since.date() == now.date():
        return since.strftime("%H:%M")
    return f"{since.strftime('%b')} {since.day} {since.strftime('%H:%M')}"


def _duration(start: datetime, end: datetime) -> str:
    minutes = max(0, int((end - start).total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min"


def _clean(text) -> str:
    """A word the provider sent (its account status): printable, short, no URL."""
    text = re.sub(r"[^\w .,'()-]", "", str(text or "")).strip()
    return redact(text[:40]) or "unknown"


# ─── Outcomes ─────────────────────────────────────────────────────────────────
# A check answers one dict: verdict ok | fail | busy; a failure also says the
# step (api | stream), kind, HTTP code and a detail (an expiry date, the
# account status, or how the provider was unreachable).

def _ok(**extra) -> dict:
    return {"verdict": "ok", **extra}


def _busy(step: str, code=None) -> dict:
    return {"verdict": "busy", "step": step, "code": code}


def _fail(step: str, kind: str, code=None, detail: str = "") -> dict:
    return {"verdict": "fail", "step": step, "kind": kind, "code": code, "detail": detail}


def _unreachable(step: str, e: Exception) -> dict:
    text = str(e).lower()
    if isinstance(e, requests.Timeout):
        how = "no answer"
    elif any(s in text for s in ("name or service not known", "nodename nor servname", "name resolution",
                                 "failed to resolve", "getaddrinfo", "no address associated")):
        how = "name not found"
    elif "refused" in text:
        how = "connection refused"
    else:
        how = "no answer"
    # The error's text names the server (and an Xtream URL its login): the
    # kind is enough here.
    logger.info(f"[Provider health] {step} check: {type(e).__name__} ({how})")
    return _fail(step, "unreachable", detail=how)


def reason(outcome: dict, since: datetime, now: datetime) -> str:
    """The plain sentence for a failure, as the banner and the phone show it."""
    t = _clock(since, now)
    kind, code, step = outcome.get("kind"), outcome.get("code"), outcome.get("step")
    detail = outcome.get("detail") or ""
    streams = step == "stream"
    if kind == "expired":
        return f"Provider account expired on {detail}" if detail else "Provider account has expired"
    if kind == "status":
        return f"Provider account is {detail}"
    if kind == "login":
        return (f"Provider rejects the login since {t}. Check the username and password; "
                f"the account may have expired")
    if kind == "not_json":
        return (f"Provider answers with a web page instead of its API since {t}. "
                f"Likely a moved URL or a block page")
    if kind == "not_m3u":
        return (f"Provider answers with something other than its M3U playlist since {t}. "
                f"Likely a moved URL or a block page")
    if kind == "unreachable":
        return f"Provider{' streams' if streams else ''} unreachable since {t} ({detail or 'no answer'})"
    if kind == "streams_gone":
        return (f"Provider test streams not found (404) since {t}. The titles tried may have left "
                f"the provider; the next library sync replaces them")
    if kind == "empty":
        return f"Provider sends empty streams since {t}"
    # kind == "http"
    if code and code >= 500:
        return f"Provider{' stream' if streams else ''} server error (HTTP {code}) since {t}. Usually on their side"
    if not streams and code == 404:
        return f"Provider address not found (404) since {t}. The server URL may have moved"
    if not streams:
        return f"Provider refusing: {code} since {t}. Likely IP block, expired account or moved URL"
    login_ok = " (the login still works)" if outcome.get("api_ok") else ""
    return (f"Provider refusing streams: {code} since {t}{login_ok}. "
            f"Likely IP block or the account over its connection limit")


# ─── Step 1: the API ──────────────────────────────────────────────────────────

def _epoch(value):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _check_xtream_api(p: Provider, ua: str) -> dict:
    from services.xtream_client import quote_cred
    url = (f"{(p.server_url or '').rstrip('/')}/player_api.php"
           f"?username={quote_cred(p.username)}&password={quote_cred(p.password)}")
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=API_TIMEOUT)
    except requests.RequestException as e:
        return _unreachable("api", e)
    try:
        code = r.status_code
        if code in BUSY_STATUSES:
            return _busy("api", code)
        if code == 401:
            return _fail("api", "login", code)
        if code != 200:
            return _fail("api", "http", code)
        try:
            data = r.json()
        except ValueError:
            return _fail("api", "not_json", code)
    finally:
        r.close()
    user_info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(user_info, dict):
        return _fail("api", "not_json", code)
    if user_info.get("auth") in (0, "0", False):
        return _fail("api", "login")
    exp = _epoch(user_info.get("exp_date"))
    expired_on = datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else ""
    status = str(user_info.get("status") or "").strip()
    if status and status.lower() != "active":
        if status.lower() == "expired":
            return _fail("api", "expired", detail=expired_on)
        return _fail("api", "status", detail=_clean(status))
    if exp and exp < time.time():
        return _fail("api", "expired", detail=expired_on)
    return _ok()


def _check_m3u_url(p: Provider, ua: str) -> dict:
    """The playlist's first 64 KB: it answers and is an M3U. Never the whole
    playlist (they run to hundreds of MB)."""
    try:
        with requests.get(p.m3u_url, headers={"User-Agent": ua}, timeout=API_TIMEOUT, stream=True) as r:
            code = r.status_code
            if code in BUSY_STATUSES:
                return _busy("api", code)
            if code == 401:
                return _fail("api", "login", code)
            if code != 200:
                return _fail("api", "http", code)
            head = b""
            for chunk in r.iter_content(8192):
                head += chunk
                if len(head) >= M3U_HEAD_BYTES:
                    break
    except requests.RequestException as e:
        return _unreachable("api", e)
    if not head[:M3U_HEAD_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
        return _fail("api", "not_m3u", code)
    return _ok()


def _check_api(p: Provider, ua: str):
    """None when the provider has no API to log in to (an M3U file)."""
    ptype = p.provider_type or "xtream"
    if ptype == "m3u_file":
        return None
    if ptype == "m3u_url":
        return _check_m3u_url(p, ua)
    return _check_xtream_api(p, ua)


# ─── Step 2: a test stream ────────────────────────────────────────────────────

def _candidates(db, p: Provider) -> list:
    """Provider URLs to open, in order: a few of its VOD movies (by the
    provider's own address, never through Tentacle's /api/vod), then its
    first live channels."""
    from services import vod_tokens
    from services.stream_health import _read_strm, _parse_stream_id, _direct_url
    known_bad = {row.strm_path for row in db.query(StreamHealth.strm_path).all()}
    urls = []
    movies = (db.query(Movie)
              .filter(Movie.provider_id == p.id, Movie.strm_path.isnot(None))
              .order_by(Movie.id).limit(50).all())
    for m in movies:
        if len(urls) >= MAX_MOVIE_CANDIDATES:
            break
        if m.strm_disabled or m.strm_path in known_bad or not Path(m.strm_path).is_file():
            continue
        url = _read_strm(m.strm_path)
        if not url:
            continue
        kind, stream_id = _parse_stream_id(url)
        direct = _direct_url(url, kind, stream_id, p)
        if direct.startswith(("http://", "https://")) and not vod_tokens.is_vod_url(direct):
            urls.append(direct)
    channels = (db.query(LiveChannel)
                .filter(LiveChannel.provider_id == p.id)
                .order_by(LiveChannel.enabled.desc(), LiveChannel.id)
                .limit(MAX_CHANNEL_CANDIDATES).all())
    urls += [c.stream_url for c in channels
             if (c.stream_url or "").startswith(("http://", "https://"))]
    return urls


def _probe(url: str, ua: str) -> tuple:
    """(ok | gone | busy | fail | empty | unreachable, HTTP code or the error)."""
    try:
        with requests.get(url, headers={"Range": "bytes=0-65535", "User-Agent": ua},
                          timeout=STREAM_TIMEOUT, stream=True, allow_redirects=True) as r:
            code = r.status_code
            if code in (200, 206):
                for chunk in r.iter_content(65536):
                    if chunk:
                        return "ok", code
                return "empty", code
            if code in GONE_STATUSES:
                return "gone", code
            if code in BUSY_STATUSES:
                return "busy", code
            return "fail", code
    except requests.RequestException as e:
        return "unreachable", e


def _check_streams(db, p: Provider, ua: str, api_ok: bool, sleep) -> dict:
    urls = _candidates(db, p)
    if not urls:
        return _ok(stream_tested=False)
    for index, url in enumerate(urls):
        if index:
            sleep(PROBE_PAUSE_SECONDS)
        result, code = _probe(url, ua)
        if result == "ok":
            return _ok(stream_tested=True)
        if result == "gone":
            continue        # that title left the provider, not the provider itself
        if result == "busy":
            return _busy("stream", code)
        if result == "unreachable":
            out = _unreachable("stream", code)
        elif result == "empty":
            out = _fail("stream", "empty", code)
        else:
            out = _fail("stream", "http", code)
        out["api_ok"] = api_ok
        return out
    out = _fail("stream", "streams_gone", 404)
    out["api_ok"] = api_ok
    return out


def check_provider(db, p: Provider, sleep=time.sleep) -> dict:
    """One check: the API first; its failure is the reason (no stream test)."""
    ua = p.user_agent or DEFAULT_UA
    api = _check_api(p, ua)
    if api is not None and api["verdict"] != "ok":
        return api
    return _check_streams(db, p, ua, api_ok=api is not None, sleep=sleep)


# ─── State ────────────────────────────────────────────────────────────────────

def load_state(db) -> dict:
    try:
        data = json.loads(get_setting(db, SETTING_KEY, "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(db, state: dict) -> None:
    set_setting(db, SETTING_KEY, json.dumps(state))


def _unknown() -> dict:
    return {"state": "unknown", "since": None, "checked_at": None, "skipped_at": None,
            "step": None, "code": None, "kind": None, "reason": None,
            "alerted": False, "recovery_due": None}


def _watched(db) -> list:
    return [p for p in db.query(Provider).order_by(Provider.priority, Provider.id).all()
            if p.active or p.live_tv_enabled]


def _stand_aside(db) -> bool:
    from services.provider_activity import live_streams_active, recording_protected
    return live_streams_active() or recording_protected(db)


def _link(db) -> str:
    return (get_setting(db, "external_url", "") or "").strip()


def _apply(db, state: dict, p: Provider, outcome: dict, first_at: datetime, now: datetime) -> None:
    from services import pushover
    key = str(p.id)
    prev = state.get(key) or _unknown()
    entry = {**_unknown(), **prev, "name": p.name}
    verdict = outcome["verdict"]
    if verdict != "ok" and verdict != "fail":
        # Busy, or stood aside for live TV: no verdict, nothing changes.
        entry["skipped_at"] = now.isoformat()
        state[key] = entry
        return
    entry["checked_at"] = now.isoformat()
    if verdict == "ok":
        if prev.get("state") == "failing":
            since = _parse(prev.get("since"))
            msg = f"Provider {p.name} works again (down since {_clock(since, now)}, {_duration(since, now) if since else '?'})"
            logger.info(f"[Provider health] {msg}")
            log_activity(db, "provider_health", msg)
            if prev.get("alerted"):
                entry["recovery_due"] = {"since": prev.get("since"), "until": now.isoformat()}
        entry.update(state="ok", since=None, step=None, code=None, kind=None, reason=None, alerted=False)
    else:
        if prev.get("state") == "failing":
            since = _parse(prev.get("since")) or first_at
        elif prev.get("recovery_due"):
            # Failing again before the recovery message went out: for the phone
            # it is the same outage, already announced.
            since = _parse(prev["recovery_due"].get("since")) or first_at
            entry["alerted"] = True
        else:
            since = first_at
            entry["alerted"] = False
        entry["recovery_due"] = None
        text = reason(outcome, since, now)
        if prev.get("state") != "failing" or prev.get("kind") != outcome.get("kind") \
                or prev.get("code") != outcome.get("code"):
            logger.warning(f"[Provider health] {p.name}: {text}")
            log_activity(db, "provider_health", f"Provider {p.name}: {text}")
        entry.update(state="failing", since=since.isoformat(), step=outcome.get("step"),
                     code=outcome.get("code"), kind=outcome.get("kind"), reason=text)

    if entry["state"] == "failing" and not entry.get("alerted"):
        entry["alerted"] = pushover.send(db, f"Tentacle: {p.name} is failing", entry["reason"], _link(db))
    due = entry.get("recovery_due")
    if entry["state"] == "ok" and due:
        since, until = _parse(due.get("since")), _parse(due.get("until")) or now
        body = (f"Down since {_clock(since, until)} ({_duration(since, until)})." if since
                else "It answers again.")
        if pushover.send(db, f"Tentacle: {p.name} works again", body, _link(db)):
            entry["recovery_due"] = None
    state[key] = entry


def check_providers(db, confirm_delay=RETRY_DELAY_SECONDS, sleep=time.sleep) -> dict:
    """Check every watched provider, update the state, send what is due.
    `confirm_delay` None: no confirming re-check ("Check now")."""
    state = load_state(db)
    providers = _watched(db)
    ids = {str(p.id) for p in providers}
    for key in [k for k in state if k not in ids]:
        del state[key]        # deleted or turned off
    now = _now()
    if _stand_aside(db):
        for p in providers:
            entry = {**_unknown(), **state.get(str(p.id), {}), "name": p.name}
            entry["skipped_at"] = now.isoformat()
            state[str(p.id)] = entry
        _save_state(db, state)
        logger.info("[Provider health] live TV or a recording is running: nothing checked this time")
        return {"skipped": True, "checked": 0}

    outcomes, first_at = {}, {}
    for p in providers:
        first_at[p.id] = _now()
        outcomes[p.id] = check_provider(db, p, sleep)

    unconfirmed = [p.id for p in providers
                   if outcomes[p.id]["verdict"] == "fail"
                   and (state.get(str(p.id)) or {}).get("state") != "failing"]
    if unconfirmed and confirm_delay is not None:
        logger.info(f"[Provider health] {len(unconfirmed)} provider(s) failed; checking again in "
                    f"{confirm_delay:.0f}s before saying so")
        db.commit()           # no read transaction held through the wait
        sleep(confirm_delay)
        aside = _stand_aside(db)
        for pid in unconfirmed:
            p = db.get(Provider, pid)
            if p is None:
                outcomes.pop(pid, None)
                continue
            outcomes[pid] = _busy("api") if aside else check_provider(db, p, sleep)
            if outcomes[pid]["verdict"] == "ok":
                logger.info(f"[Provider health] {p.name}: the failure did not repeat")

    now = _now()
    for p in providers:
        if p.id in outcomes:
            _apply(db, state, p, outcomes[p.id], first_at[p.id], now)
    _save_state(db, state)
    failing = [s for s in state.values() if s.get("state") == "failing"]
    return {"skipped": False, "checked": len(outcomes), "failing": len(failing)}


def run_provider_health() -> bool:
    """The hourly job. False (nothing checked) while a check is running."""
    if not _run_lock.acquire(blocking=False):
        logger.info("[Provider health] a check is already running; this one checks nothing")
        return False
    db = SessionLocal()
    try:
        check_providers(db)
    except Exception:
        logger.exception("[Provider health] check failed")
    finally:
        db.close()
        _run_lock.release()
    return True


def check_now(db):
    """"Check now": no confirming re-check. None while a check is running."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        return check_providers(db, confirm_delay=None)
    finally:
        _run_lock.release()


def check_running() -> bool:
    return _run_lock.locked()


# ─── Views ────────────────────────────────────────────────────────────────────

_SHOWN = ("state", "since", "checked_at", "skipped_at", "step", "code", "kind", "reason")


def provider_view(db) -> list:
    """Admin view: every watched provider with its state."""
    state = load_state(db)
    out = []
    for p in _watched(db):
        entry = {**_unknown(), **(state.get(str(p.id)) or {})}
        out.append({"id": p.id, "name": p.name, "provider_type": p.provider_type or "xtream",
                    **{k: entry.get(k) for k in _SHOWN}})
    return out


def public_status(db) -> tuple:
    """(HTTP status, body) for the unauthenticated /api/provider-status: no
    provider names, URLs or logins. 503 while any provider is failing, so a
    monitor that counts 5xx as down alerts on it with no parsing."""
    state = load_state(db)
    watched = {str(p.id) for p in _watched(db)}
    entries = [e for k, e in state.items() if k in watched and isinstance(e, dict)]
    checked = [e["checked_at"] for e in entries if e.get("checked_at")]
    failing = [e for e in entries if e.get("state") == "failing"]
    body = {"status": "failing" if failing else "ok", "checked_at": max(checked) if checked else None}
    if not failing:
        return 200, body
    body["since"] = min(e["since"] for e in failing if e.get("since")) if any(e.get("since") for e in failing) else None
    body["problems"] = [e.get("reason") or "Provider failing" for e in failing]
    return 503, body
