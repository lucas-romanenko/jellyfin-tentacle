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
  services/provider_activity.py). So a run looks before every connection
  it opens and stops, with no verdict, once Tentacle proxies live TV or a
  protected recording; the next hour tries again.
- What syncs and live streams already learn counts too (`note`, #652), with
  no connection of its own: a refusal (401/403) is confirmed by a second one
  RETRY_DELAY_SECONDS or more later, a sync or stream that worked clears a
  failure of its own step. An outage during a recording shows, and a
  recovered provider clears without waiting for the hourly check.

Reasons are short and plain, and never carry a URL or a login: they are
shown on the dashboard, sent to the phone and served unauthenticated by
/api/provider-status (without the provider names).
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
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
# A noted refusal waits this long for the one that confirms it; older, it
# starts the count again.
SUSPECT_SECONDS = 3600
API_TIMEOUT = (10, 20)
STREAM_TIMEOUT = (10, 15)
PROBE_PAUSE_SECONDS = 3.0
MAX_MOVIE_CANDIDATES = 3
MAX_CHANNEL_CANDIDATES = 2
M3U_HEAD_BYTES = 65536
DEFAULT_UA = "TiviMate/4.7.0 (Linux; Android 12)"
# A start (about 25 deploys a day) checks again only when the last check is
# this old: each check is a real connection on every provider and address.
MIN_GAP_SECONDS = 3600
# "Not right now": the account is over its connection limit or rate-limited.
# No verdict either way; the state stays as it was.
BUSY_STATUSES = {429, 509}
GONE_STATUSES = {404, 410}

# The hourly job and "Check now" never run at once: two checks are two
# connections, which is what a one-connection account cuts a stream for.
_run_lock = threading.Lock()
# Held for each read-change-write of the state: by `note` (syncs, streams)
# and by a check's saves, so neither writes over what the other just wrote.
# Never _run_lock: a note while a check runs must not make "Check now" 409.
_state_lock = threading.Lock()


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


def _aside(step: str) -> dict:
    """Stood aside: live TV or a recording started while the run was under way."""
    return {**_busy(step), "aside": True}


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
    if kind == "not_json" and outcome.get("hint") == "database_error":
        return f"Provider's panel shows a database error since {t}. On their side"
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
    if not streams and outcome.get("cf_error"):
        return (f"Provider refusing: {code} (Cloudflare error {outcome['cf_error']}) since {t}. "
                f"This home's connection is blocked")
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
            return {**_fail("api", "http", code), **_page_hints(r)}
        try:
            data = r.json()
        except ValueError:
            return {**_fail("api", "not_json", code), **_page_hints(r)}
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
    return _ok(expires=exp)


def _page_hints(r) -> dict:
    """What a refusal or a web page instead of the API says, for
    services/provider_status: Cloudflare's 1xxx code, the panel's
    "Database Error". Only words, nothing of the page is kept."""
    from services.provider_status import _cf_error
    try:
        text = (getattr(r, "text", "") or "")[:4096]
    except Exception:
        return {}
    if not isinstance(text, str):
        return {}
    out = {}
    headers = getattr(r, "headers", None) or {}
    if "cf-ray" in {k.lower() for k in headers} or (headers.get("Server") or "").lower() == "cloudflare":
        out["cf"] = True
        if _cf_error(text):
            out["cf_error"] = _cf_error(text)
    if "database error" in text.lower():
        out["hint"] = "database_error"
    return out


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
        if _stand_aside(db):
            return _aside("stream")
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
    out = _check_streams(db, p, ua, api_ok=api is not None, sleep=sleep)
    if api is not None and api.get("expires") and out.get("verdict") == "ok":
        out["expires"] = api["expires"]     # "the account ends soon" (services/provider_status)
    return out


# ─── State ────────────────────────────────────────────────────────────────────

def load_state(db) -> dict:
    try:
        data = json.loads(get_setting(db, SETTING_KEY, "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_fresh(db) -> dict:
    """The state as committed now. A session hands back the row it read
    before (its identity map) unless told to read it again."""
    from models.database import Setting
    row = (db.query(Setting).filter(Setting.key == SETTING_KEY)
           .populate_existing().first())
    try:
        data = json.loads((row.value if row else "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(db, state: dict) -> None:
    before = get_setting(db, SETTING_KEY, "")
    set_setting(db, SETTING_KEY, json.dumps(state))
    if json.dumps(state) != before:
        from services import live_updates, provider_status
        live_updates.publish("health", admins=True)
        provider_status.changed()


def forget(db, provider_id: int) -> None:
    """Drop a deleted provider's state, into the session only (the delete
    commits). check_providers prunes only ids that no longer exist, and the
    next provider may get this one's id: it would start "failing since ..."
    (#625)."""
    state = load_state(db)
    if state.pop(str(provider_id), None) is None:
        return
    from models.database import Setting
    row = db.query(Setting).filter(Setting.key == SETTING_KEY).first()
    if row is not None:
        row.value = json.dumps(state)


def _unknown() -> dict:
    return {"state": "unknown", "since": None, "checked_at": None, "skipped_at": None,
            "step": None, "code": None, "kind": None, "reason": None,
            "alerted": False, "recovery_due": None, "suspect": None,
            "case": None, "case_detail": None, "back": None}


def _watched(db) -> list:
    return [p for p in db.query(Provider).order_by(Provider.priority, Provider.id).all()
            if p.active or p.live_tv_enabled]


def _stand_aside(db) -> bool:
    from services.provider_activity import jellyfin_playing, live_streams_active, recording_protected
    return live_streams_active() or recording_protected(db) or jellyfin_playing(db)


def _link(db) -> str:
    from models.database import tentacle_address
    return (get_setting(db, "external_url", "") or "").strip() or tentacle_address(db)


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
    entry["suspect"] = None     # a verdict settles a refusal still waiting for its second
    if verdict == "ok":
        if prev.get("state") == "failing":
            since = _parse(prev.get("since"))
            msg = f"Provider {p.name} works again (down since {_clock(since, now)}, {_duration(since, now) if since else '?'})"
            logger.info(f"[Provider health] {msg}")
            log_activity(db, "provider_health", msg)
            if prev.get("alerted"):
                entry["recovery_due"] = {"since": prev.get("since"), "until": now.isoformat()}
        if prev.get("state") == "failing":
            # "IPTV Bears is back" for a while (services/provider_status)
            entry["back"] = {"since": prev.get("since"), "until": now.isoformat(), "case": prev.get("case")}
        if outcome.get("expires"):
            entry["expires"] = outcome["expires"]
        entry.update(state="ok", since=None, step=None, code=None, kind=None, reason=None, alerted=False,
                     case=None, case_detail=None, other_route=None, expired_on=None)
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
                     code=outcome.get("code"), kind=outcome.get("kind"), reason=text, back=None,
                     expired_on=outcome.get("detail") if outcome.get("kind") == "expired" else None)
        if outcome.get("case") or prev.get("state") != "failing":
            # Which kind of trouble (services/provider_status.diagnose); a
            # note (a sync, a stream) has none and keeps what a check found.
            entry.update(case=outcome.get("case"), case_detail=outcome.get("case_detail"),
                         other_route=outcome.get("other_route"))

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
    `confirm_delay` None: no confirming re-check ("Check now").

    The state read here only decides which failures need confirming: syncs
    and streams note theirs while the check runs (minutes, with the wait),
    so each save re-reads it under _state_lock and changes only its part."""
    state = load_state(db)
    providers = _watched(db)
    ids = {str(p.id) for p in providers}

    def fresh() -> dict:
        current = _load_fresh(db)
        for key in [k for k in current if k not in ids]:
            del current[key]        # deleted or turned off
        return current

    now = _now()
    if _stand_aside(db):
        with _state_lock:
            state = fresh()
            for p in providers:
                entry = {**_unknown(), **state.get(str(p.id), {}), "name": p.name}
                entry["skipped_at"] = now.isoformat()
                state[str(p.id)] = entry
            _save_state(db, state)
        logger.info("[Provider health] live TV or a recording is running: nothing checked this time")
        return {"skipped": True, "checked": 0}

    outcomes, first_at, logged = {}, {}, []

    def check(p, confirming=False):
        """One provider, unless live TV or a recording started since the last look.
        Confirming a failure while someone watches: the address in use is
        asked for "/" with no login (no connection on the account), and a
        failure there confirms; an answer leaves it unconfirmed."""
        if _stand_aside(db):
            if confirming:
                out = _confirm_by_reach(p, outcomes[p.id])
                if out is not None:
                    return out
            out = _aside("api")
        else:
            out = check_provider(db, p, sleep)
        if out.get("aside") and not logged:
            logged.append(True)
            logger.info("[Provider health] live TV or a recording started during the check: providers not checked yet wait for the next run")
        return out

    for p in providers:
        first_at[p.id] = _now()
        outcomes[p.id] = check(p)

    unconfirmed = [p.id for p in providers
                   if outcomes[p.id]["verdict"] == "fail"
                   and (state.get(str(p.id)) or {}).get("state") != "failing"]
    if unconfirmed and confirm_delay is not None:
        logger.info(f"[Provider health] {len(unconfirmed)} provider(s) failed; checking again in "
                    f"{confirm_delay:.0f}s before saying so")
        db.commit()           # no read transaction held through the wait
        sleep(confirm_delay)
        for pid in unconfirmed:
            p = db.get(Provider, pid)
            if p is None:
                outcomes.pop(pid, None)
                continue
            outcomes[pid] = check(p, confirming=True)
            if outcomes[pid]["verdict"] == "ok":
                logger.info(f"[Provider health] {p.name}: the failure did not repeat")

    for p in providers:
        if (outcomes.get(p.id) or {}).get("verdict") == "fail":
            _diagnose(db, p, outcomes[p.id])
    now = _now()
    with _state_lock:
        state = fresh()
        for p in providers:
            if p.id in outcomes:
                _apply(db, state, p, outcomes[p.id], first_at[p.id], now)
        _save_state(db, state)
    # The backups of each provider with several addresses, and a switch where
    # the address in use is failing (services/provider_addresses.py).
    switched = []
    try:
        from services import provider_addresses
        switched = provider_addresses.check_backups(db, providers, outcomes, state, sleep=sleep)
    except Exception:
        logger.exception("[Provider health] checking the backup addresses failed")
        db.rollback()
    for done in switched:
        state = address_switched(db, done["provider_id"]) or state
    failing = [s for s in state.values() if s.get("state") == "failing"]
    checked = sum(1 for o in outcomes.values() if o["verdict"] in ("ok", "fail"))
    return {"skipped": bool(logged), "checked": checked, "failing": len(failing),
            "switched": len(switched)}


def _diagnose(db, p: Provider, outcome: dict) -> None:
    """Which kind of trouble a confirmed failure is (down, blocked, account,
    other): services/provider_status, from "/" of every address of the
    provider (no login) and the backups' last test. Into the outcome."""
    from services import provider_status
    try:
        found = provider_status.look(p, outcome)
    except Exception as e:
        # The type only: an error's text names the address
        logger.warning(f"[Provider health] could not tell which kind of failure this is: {type(e).__name__}")
        return
    outcome.update(case=found["case"], case_detail=found["detail"], other_route=found.get("other_route"))
    logger.info(f"[Provider health] {p.name}: {found['case']} ({found['detail']}); "
                f"addresses: {', '.join(found['probes']) or 'not asked'}")


def _confirm_by_reach(p: Provider, first: dict):
    """A failure confirmed without the account: "/" of the address in use
    (the player User-Agent, no login) fails too. None: it answers, or
    there is nothing to ask (an M3U file)."""
    from services import provider_status
    if first.get("verdict") != "fail" or first.get("step") != "api" or (p.provider_type or "xtream") != "xtream":
        return None
    r = provider_status.reach(p.server_url or "", p.user_agent or DEFAULT_UA)
    out = provider_status._outcome_from_reach(r)
    if out is None:
        return None
    logger.info(f"[Provider health] {p.name}: someone is watching, so the failure is confirmed without "
                f"logging in: the address answers {r['how']}")
    # The first check's own words (its code, its kind) stay the reason
    return {**out, **{k: v for k, v in first.items() if k in ("kind", "code", "detail", "hint", "cf_error")}}


def record(db, p: Provider, outcome: dict, first_at: datetime) -> None:
    """A failure seen outside the hourly check (services/provider_status's
    quick look, from live channels failing), applied like a check's."""
    with _state_lock:
        state = _load_fresh(db)
        _apply(db, state, p, outcome, first_at, _now())
        _save_state(db, state)


def address_switched(db, provider_id: int):
    """The provider now uses another address (by hand or by itself): its
    failure was the old address's. Cleared quietly, since the switch sent its
    own message; the next check says how the new one does."""
    with _state_lock:
        state = _load_fresh(db)
        entry = state.get(str(provider_id))
        if not entry:
            return state
        entry.update(state="unknown", since=None, step=None, code=None, kind=None, reason=None,
                     alerted=False, recovery_due=None, suspect=None)
        _save_state(db, state)
        return state


# ─── What syncs and streams learn ─────────────────────────────────────────────

def note(db, provider_id, ok: bool, step: str, code=None) -> None:
    """What a sync ("api") or a live stream ("stream") just learned about a
    provider (#652). Opens no connection, commits its own write, never raises.

    ok: clears a refusal waiting for its second, and a failure of the same
    step (a sync working says nothing about refused streams, a stream
    working nothing about a refused login; the hourly check covers those).
    Not ok (a 401/403): like the check, a refusal counts only once a second
    one comes RETRY_DELAY_SECONDS or more after the first (and within
    SUSPECT_SECONDS); one blip changes nothing."""
    try:
        with _state_lock:
            _note(db, provider_id, ok, step, code)
    except Exception as e:
        logger.warning(f"[Provider health] could not note what a {'sync' if step == 'api' else 'stream'} "
                       f"learned about provider {provider_id}: {redact(str(e))}")
        try:
            db.rollback()
        except Exception:
            pass


def _server_side(entry: dict) -> bool:
    """A failure of the provider's server (down, not a refusal or the
    account): any live stream delivering again proves it's over."""
    if entry.get("case") == "down":
        return True
    kind, code = entry.get("kind"), entry.get("code")
    return kind == "unreachable" and entry.get("case") != "blocked" or \
        (kind == "http" and (code or 0) >= 500) or (kind == "not_json" and entry.get("case") != "blocked")


def _note(db, provider_id, ok: bool, step: str, code) -> None:
    p = next((p for p in _watched(db) if p.id == provider_id), None)
    if p is None:
        return
    state = _load_fresh(db)
    key = str(p.id)
    entry = {**_unknown(), **(state.get(key) or {}), "name": p.name}
    suspect = entry.get("suspect") if isinstance(entry.get("suspect"), dict) else None
    now = _now()
    if ok:
        changed = suspect is not None
        entry["suspect"] = None
        state[key] = entry
        if entry.get("state") == "failing" and (entry.get("step") == step or (
                step == "stream" and _server_side(entry))):
            _apply(db, state, p, _ok(), now, now)
            changed = True
        if changed:
            _save_state(db, state)
        return
    if entry.get("state") == "failing":
        return          # already said; the check's reason stays
    at = _parse(suspect.get("at")) if suspect else None
    age = (now - at).total_seconds() if at else None
    what = "a sync" if step == "api" else "a stream"
    if age is None or age < 0 or age >= SUSPECT_SECONDS:
        entry["suspect"] = {"at": now.isoformat(), "step": step, "code": code}
        state[key] = entry
        _save_state(db, state)
        logger.info(f"[Provider health] {p.name} refused {what} (HTTP {code}); failing if that happens "
                    f"again in {RETRY_DELAY_SECONDS}s or more")
        return
    if age < RETRY_DELAY_SECONDS:
        return          # the same blip
    kind = "login" if step == "api" and code == 401 else "http"
    _apply(db, state, p, _fail(step, kind, code), at, now)
    _save_state(db, state)


def run_provider_health() -> bool:
    """The hourly job. False (nothing checked) while a check is running, or
    while the nightly sync is asking the providers for everything: its
    requests already tell provider health how they went (`note`), and a login
    plus a test stream on top of its burst is what a panel holds an account
    back for (#709). The next hour checks."""
    from routers.sync import nightly_running
    if nightly_running():
        logger.info("[Provider health] the nightly sync is running; this check is skipped, the next hour checks")
        return False
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


def last_checked(db):
    """When the last check that reached a verdict ran (the newest checked_at),
    or None. A run that stood aside or found every provider busy doesn't
    count: it checked nothing, the backups included."""
    times = [_parse(e.get("checked_at")) for e in load_state(db).values() if isinstance(e, dict)]
    times = [t for t in times if t is not None and t.tzinfo is not None]
    return max(times) if times else None


def next_due(db, now=None):
    """When the next check is due if the last one is under MIN_GAP_SECONDS
    old, else None (check shortly after the start). A restart then neither
    checks again nor moves the hourly run later than an hour after the last."""
    last = last_checked(db)
    now = now or _now()
    if last is None or now - last >= timedelta(seconds=MIN_GAP_SECONDS):
        return None
    return last + timedelta(seconds=MIN_GAP_SECONDS)


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

_SHOWN = ("state", "since", "checked_at", "skipped_at", "step", "code", "kind", "reason", "case")


def provider_view(db) -> list:
    """Admin view: every watched provider with its state."""
    from routers.livetv import guide_problem
    state = load_state(db)
    out = []
    for p in _watched(db):
        entry = {**_unknown(), **(state.get(str(p.id)) or {})}
        try:
            guide = guide_problem(db, p)    # a failing or ended Live TV guide (#709)
        except Exception as e:
            logger.warning(f"[Provider health] Could not read the guide state of {p.name}: {e}")
            guide = None
        from services import provider_status
        said = provider_status.describe(p.name, entry, _now(), admin=True) or {}
        out.append({"id": p.id, "name": p.name, "provider_type": p.provider_type or "xtream",
                    **{k: entry.get(k) for k in _SHOWN}, "guide_problem": guide,
                    "case": said.get("kind"), "headline": said.get("title"), "plain": said.get("text"),
                    "channels_down": provider_status.broken_channels(p.id) if p.live_tv_enabled else []})
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
    from services.provider_status import _fallback_case
    body["case"] = [e.get("case") or _fallback_case(e) for e in failing]
    return 503, body
