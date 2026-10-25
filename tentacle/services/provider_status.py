"""
Tentacle - What's wrong with Live TV, in plain words

Provider health (services/provider_health.py) knows *that* a provider fails
and the technical reason ("unreachable since 18:30 (connection refused)").
People need to know *which kind* of trouble it is, because what they should
do differs (Roadmap, 25 Oct 2026):

- down      the provider's server is down: not your fault, wait. Cloudflare
            52x, connection refused or no answer on every address, the
            panel's "Database Error", 5xx on the login or on every channel.
- blocked   this home's connection is refused: 403/511 or a Cloudflare
            10xx block on the address in use, or that address silent while
            another of the provider's addresses answers.
- account   the login answers but says no: auth 0, Expired / Disabled /
            Banned, the expiry date passed. (Ends soon: admins only.)
- channels  a few channels don't come through while the others play.
- other     anything else (a moved address, test titles gone).

How it's decided, never on one look:

- reach(): GET "/" of an address with the player User-Agent, no login. It
  opens no connection on the account, so it may run while someone watches.
  (Cloudflare answers Python's own User-Agent with 403 "error code: 1010":
  with that one every address would look blocked.)
- diagnose(): a failed check plus the reach of every address of the
  provider (and the backups' last test) gives the case.
- Live TV tells this module about every channel that fails to open or ends
  while still failing, and every channel that delivers (channel_failed /
  channel_ok). Two channels of one provider failing within FAST_WINDOW with
  none delivering starts a quick look (reach only, at most every
  QUICK_GAP_SECONDS) that can confirm an outage within minutes of someone
  trying to watch; the hourly check alone could miss a whole evening.

messages() is what everyone sees (Tentacle's banner, Jellyfin Web's guide,
the TV app's guide), slate_lines() what the "Not coming through" picture
says. Neither carries a URL, a login or a code.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REACH_TIMEOUT = (6, 8)
REACH_BYTES = 4096
# Cloudflare: the site behind it doesn't answer (520-526), or 530 with a 1xxx
# error (its origin can't be found).
CF_ORIGIN_DOWN = {520, 521, 522, 523, 524, 525, 526, 530}
# Cloudflare 1xxx codes that refuse the visitor: IP banned (1006-1008),
# country (1009), browser signature (1010), hotlink (1011), access denied
# (1012), firewall rule (1020).
CF_BLOCKS = {1006, 1007, 1008, 1009, 1010, 1011, 1012, 1020}
CASES = ("down", "blocked", "account", "other")
# A backup's last test counts as "works now" this long.
BACKUP_FRESH = timedelta(hours=2)

# Channels
FAST_WINDOW = 600            # two channels failing within this: look at once
CHANNEL_WINDOW = 1800        # a channel's failures count this long
CHANNEL_FAILS = 2            # failures before a channel shows as not coming through
ALL_CHANNELS = 3             # this many channels failing, none delivering: the whole provider
QUICK_GAP_SECONDS = 300
BACK_SHOWN = timedelta(minutes=30)
EXPIRY_SOON_DAYS = 7
WATCH_CHECK_GAP = timedelta(minutes=15)

_lock = threading.Lock()
_channels: dict = {}         # channel id -> {"provider_id", "name", "fails": [t, ...], "code"}
_delivered: dict = {}        # provider id -> monotonic time a channel last delivered
_quick_at: dict = {}         # provider id -> monotonic time of the last quick look
_sig = {"value": None}
_running = {"on": False}     # start(): quick looks only in the running app


def start() -> None:
    _running["on"] = True


def _mono() -> float:
    return time.monotonic()


# ─── Reach: one address, no login ────────────────────────────────────────────

def _cf_error(text: str) -> Optional[int]:
    m = re.search(r"error code:?\s*(1\d{3})", text or "", re.I)
    return int(m.group(1)) if m else None


def reach(base: str, ua: str) -> dict:
    """What GET "/" of `base` answers, with the player User-Agent and no login.
    {"how": answers | origin_down | server_error | blocked | refused |
    no_answer | name_not_found | secure_failed, "code", "cf", "cf_error"}.
    Never raises; never logs the address."""
    out = {"how": "no_answer", "code": None, "cf": False, "cf_error": None}
    try:
        with requests.get(base.rstrip("/") + "/", headers={"User-Agent": ua}, timeout=REACH_TIMEOUT,
                          stream=True, allow_redirects=False) as r:
            code = r.status_code
            headers = getattr(r, "headers", None) or {}
            cf = "cf-ray" in {k.lower() for k in headers} or (headers.get("Server") or "").lower() == "cloudflare"
            body = b""
            for chunk in r.iter_content(1024):
                body += chunk or b""
                if len(body) >= REACH_BYTES:
                    break
    except requests.RequestException as e:
        text = str(e).lower()
        if isinstance(e, requests.Timeout):
            how = "no_answer"
        elif "refused" in text:
            how = "refused"
        elif any(s in text for s in ("name or service not known", "name resolution", "failed to resolve",
                                     "getaddrinfo", "nodename nor servname", "no address associated")):
            how = "name_not_found"
        elif "ssl" in text or "certificate" in text:
            how = "secure_failed"
        else:
            how = "no_answer"
        return {**out, "how": how}
    text = body[:REACH_BYTES].decode("utf-8", "replace")
    out.update(code=code, cf=cf, cf_error=_cf_error(text) if cf else None)
    if cf and code in CF_ORIGIN_DOWN:
        out["how"] = "origin_down"
    elif code == 511 or (cf and code == 403 and out["cf_error"] in CF_BLOCKS):
        out["how"] = "blocked"
    elif code >= 500 or "database error" in text.lower():
        out["how"] = "server_error"
    else:
        out["how"] = "answers"      # the provider itself replied (a 401 for "/" included)
    return out


def reach_all(p, ua: str) -> list:
    """reach() of every address of the provider, the one in use first:
    [{"in_use", **reach}]. Addresses never leave this module's callers."""
    from services.provider_addresses import normalize
    current = normalize(p.server_url or "")
    urls = [current] if current else []
    for a in getattr(p, "addresses", None) or []:
        u = normalize(a.url)
        if u and u not in urls:
            urls.append(u)
    out = []
    for u in urls:
        out.append({"in_use": u == current, **reach(u, ua)})
    return out


# ─── Which case ──────────────────────────────────────────────────────────────

def _backup_works(p, now_utc: datetime) -> bool:
    """A backup address logged in at its latest test, within BACKUP_FRESH."""
    from services.provider_addresses import is_current
    for a in getattr(p, "addresses", None) or []:
        if is_current(p, a):
            continue
        r = a.last_result or {}
        try:
            at = datetime.fromisoformat(str(r.get("at", "")).rstrip("Z"))
        except ValueError:
            continue
        if r.get("ok") and now_utc - at <= BACKUP_FRESH:
            return True
    return False


def diagnose(outcome: dict, probes: list, backup_works: bool = False) -> dict:
    """{"case", "detail"} for a failed check. `probes`: reach_all's answer
    (may be empty); `backup_works`: another address logged in lately."""
    kind, code, step = outcome.get("kind"), outcome.get("code"), outcome.get("step")
    if kind in ("login", "expired", "status"):
        detail = {"expired": "expired", "login": "login rejected"}.get(kind) or (outcome.get("detail") or "").lower()
        return {"case": "account", "detail": detail}
    current = next((x for x in probes if x.get("in_use")), None)
    others = [x for x in probes if not x.get("in_use")]
    other_answers = any(x["how"] == "answers" for x in others)
    cf_block = outcome.get("cf_error") in CF_BLOCKS or (current or {}).get("how") == "blocked"
    if cf_block or code == 511:
        return {"case": "blocked", "detail": "refused"}
    if code in (401, 403) and step == "api":
        # A refusal of the login (not its answer saying no): the address
        # refuses this connection. Refused everywhere is still a block.
        return {"case": "blocked", "detail": "refused"}
    server_side = (kind == "unreachable" or (kind == "http" and (code or 0) >= 500)
                   or (kind == "not_json" and outcome.get("hint") == "database_error"))
    if not server_side:
        if kind == "http" and code in (401, 403) and step == "stream":
            return {"case": "blocked", "detail": "refuses streams"} if (current or {}).get("how") == "blocked" \
                else {"case": "other", "detail": "refuses streams"}
        return {"case": "other", "detail": kind or ""}
    cur_how = (current or {}).get("how")
    if cur_how in ("origin_down", "server_error") or outcome.get("hint") == "database_error" \
            or (kind == "http" and (code or 0) >= 500):
        detail = "server error"
        if cur_how == "origin_down" or code in CF_ORIGIN_DOWN or kind == "unreachable":
            detail = "not answering"
        if step == "stream":
            detail = "not sending channels"
        return {"case": "down", "detail": detail, "other_route": other_answers and backup_works}
    # No answer or refused on the address in use (or no reach to go on)
    if (other_answers and backup_works) or (cur_how == "answers" and step == "api"
                                            and kind == "unreachable" and backup_works):
        # Silent here while another address of theirs answers and logs in:
        # the way from this home to that address is blocked.
        return {"case": "blocked", "detail": "silent"}
    return {"case": "down", "detail": "not answering"}


# ─── Live TV's channels ──────────────────────────────────────────────────────

def channel_failed(provider_id: Optional[int], channel_id: int, name: str = "", code=None, bind=None) -> None:
    """A channel failed to open, or ended while still failing. Cheap and
    thread-safe (called from the event loop); may start a quick look (on
    `bind`, the caller's database, else Tentacle's)."""
    if not isinstance(provider_id, int):
        return
    now = _mono()
    with _lock:
        c = _channels.setdefault(channel_id, {"provider_id": provider_id, "name": name, "fails": []})
        c["provider_id"], c["code"] = provider_id, code
        if name:
            c["name"] = name
        c["fails"] = [t for t in c["fails"] if now - t <= CHANNEL_WINDOW] + [now]
        quick = _running["on"] and _quick_due(provider_id, now)
        if quick:
            _quick_at[provider_id] = now
    _changed()
    if quick:
        threading.Thread(target=_quick_look_on, args=(provider_id, bind), daemon=True,
                         name=f"provider-quick-{provider_id}").start()


def channel_ok(provider_id: Optional[int], channel_id: int) -> None:
    """A channel delivers (provider health heard it, #652)."""
    with _lock:
        had = _channels.pop(channel_id, None)
        if isinstance(provider_id, int):
            _delivered[provider_id] = _mono()
    if had and len(had["fails"]) >= CHANNEL_FAILS:
        _changed()


def _failing_channels(provider_id: int, now: float, window: float) -> list:
    """Channels of the provider that failed within `window` and haven't
    delivered since (under _lock)."""
    return [c for c in _channels.values()
            if c["provider_id"] == provider_id and c["fails"] and now - c["fails"][-1] <= window]


def _quick_due(provider_id: int, now: float) -> bool:
    recent = _failing_channels(provider_id, now, FAST_WINDOW)
    if len(recent) < 2:
        return False
    first = min(min(t for t in c["fails"] if now - t <= FAST_WINDOW) for c in recent)
    if _delivered.get(provider_id, -1e18) >= first:
        return False        # something of theirs delivered meanwhile
    return now - _quick_at.get(provider_id, -1e18) >= QUICK_GAP_SECONDS


def _whole_provider(provider_id: int, now: float) -> bool:
    """ALL_CHANNELS or more channels failed within FAST_WINDOW and none of
    the provider's channels delivered since the first of them."""
    with _lock:
        recent = _failing_channels(provider_id, now, FAST_WINDOW)
        if len(recent) < ALL_CHANNELS:
            return False
        first = min(min(t for t in c["fails"] if now - t <= FAST_WINDOW) for c in recent)
        return _delivered.get(provider_id, -1e18) < first


def broken_channels(provider_id: int) -> list:
    """Names of the provider's channels that didn't come through
    CHANNEL_FAILS times or more within CHANNEL_WINDOW, while another of its
    channels delivered in that time: some channels, not the provider."""
    now = _mono()
    with _lock:
        if now - _delivered.get(provider_id, -1e18) > CHANNEL_WINDOW:
            return []
        rows = [c for c in _failing_channels(provider_id, now, CHANNEL_WINDOW)
                if len(c["fails"]) >= CHANNEL_FAILS]
        return sorted({c["name"] or "A channel" for c in rows})


def channel_broken(channel_id: int) -> bool:
    """This channel failed CHANNEL_FAILS times within CHANNEL_WINDOW while
    another channel of its provider delivered in that time."""
    now = _mono()
    with _lock:
        c = _channels.get(channel_id)
        if not c or now - _delivered.get(c["provider_id"], -1e18) > CHANNEL_WINDOW:
            return False
        return len([t for t in c["fails"] if now - t <= CHANNEL_WINDOW]) >= CHANNEL_FAILS


def reset() -> None:
    """Tests."""
    with _lock:
        _channels.clear()
        _delivered.clear()
        _quick_at.clear()
    _sig["value"] = None


# ─── The quick look ──────────────────────────────────────────────────────────

def _outcome_from_reach(r: dict) -> Optional[dict]:
    """A failure like a check's, from what "/" answered; None when it answered normally."""
    from services.provider_health import _fail
    how, code = r.get("how"), r.get("code")
    if how == "origin_down":
        return _fail("api", "http", code)
    if how == "server_error":
        if code and code >= 500:
            return _fail("api", "http", code)
        return {**_fail("api", "not_json", code), "hint": "database_error"}
    if how == "blocked":
        return {**_fail("api", "http", code or 403), "cf_error": r.get("cf_error")}
    if how in ("refused", "no_answer", "name_not_found", "secure_failed"):
        words = {"refused": "connection refused", "name_not_found": "name not found"}
        return _fail("api", "unreachable", detail=words.get(how, "no answer"))
    return None


def _quick_look_on(provider_id: int, bind) -> None:
    if bind is None:
        quick_look(provider_id)
        return
    from sqlalchemy.orm import Session
    db = Session(bind=bind)
    try:
        quick_look(provider_id, db)
    finally:
        db.close()


def quick_look(provider_id: int, db=None) -> Optional[str]:
    """Channels of this provider keep failing: is it the provider? Reach
    only (no login, no stream), so it runs while people watch. An outage
    seen here is recorded at once (the failing channels were the first
    look); every channel failing while the server answers is an outage of
    its streams. Returns the case recorded, or None."""
    from models.database import SessionLocal, Provider
    from services import provider_health as ph
    own = db is None
    db = db or SessionLocal()
    try:
        p = db.get(Provider, provider_id)
        if p is None or not (p.active or p.live_tv_enabled) or (p.provider_type or "xtream") == "m3u_file":
            return None
        if ph.load_state(db).get(str(p.id), {}).get("state") == "failing":
            return None     # already said
        ua = p.user_agent or ph.DEFAULT_UA
        base = (p.server_url or p.m3u_url or "")
        if (p.provider_type or "xtream") == "m3u_url":
            from urllib.parse import urlparse
            u = urlparse(p.m3u_url or "")
            base = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""
            probes = [{"in_use": True, **reach(base, ua)}] if base else []
        else:
            probes = reach_all(p, ua)
        current = next((x for x in probes if x["in_use"]), None)
        outcome = _outcome_from_reach(current) if current else None
        if outcome is None and _whole_provider(provider_id, _mono()):
            with _lock:
                codes = [c.get("code") for c in _channels.values() if c["provider_id"] == provider_id]
            code = next((c for c in codes if isinstance(c, int) and c >= 500), None)
            outcome = {**ph._fail("stream", "http" if code else "unreachable", code,
                                  detail="" if code else "no answer"), "api_ok": True}
        if outcome is None:
            logger.info(f"[Provider status] {p.name}: channels fail but the provider answers; "
                        f"some channels only")
            return None
        found = diagnose(outcome, probes, _backup_works(p, datetime.utcnow()))
        outcome.update(case=found["case"], case_detail=found["detail"])
        with _lock:
            fails = [t for c in _channels.values() if c["provider_id"] == provider_id for t in c["fails"]]
        first = ph._now() - timedelta(seconds=int(max(0.0, _mono() - min(fails)))) if fails else ph._now()
        ph.record(db, p, outcome, first)
        logger.warning(f"[Provider status] {p.name}: live channels fail and the provider is {found['case']} "
                       f"({found['detail']})")
        return found["case"]
    except Exception as e:
        logger.warning(f"[Provider status] the quick look failed: {type(e).__name__}")
        return None
    finally:
        if own:
            db.close()


def watch() -> None:
    """Every few minutes: a provider said down or blocked whose address in
    use answers "/" normally again gets a full check at once, when nobody
    watches (someone watching hears it from their stream: provider health's
    note). So "back" doesn't wait for the hourly check."""
    from models.database import SessionLocal, Provider
    from services import provider_health as ph
    db = SessionLocal()
    try:
        state = ph.load_state(db)
        due = False
        now = ph._now()
        for key, entry in state.items():
            if entry.get("state") != "failing" or entry.get("case") not in ("down", "blocked"):
                continue
            last = _local(entry.get("checked_at"))
            if last is not None and now - last < WATCH_CHECK_GAP:
                continue        # a full check (a login, a test stream) at most this often
            p = db.get(Provider, int(key)) if str(key).isdigit() else None
            if p is None or not p.server_url:
                continue
            if reach(p.server_url, p.user_agent or ph.DEFAULT_UA)["how"] == "answers":
                due = True
        if due and not ph._stand_aside(db):
            logger.info("[Provider status] a failing provider answers again: checking it now")
            ph.check_now(db)
    except Exception as e:
        logger.warning(f"[Provider status] the watch failed: {type(e).__name__}")
    finally:
        db.close()


# ─── Plain words ─────────────────────────────────────────────────────────────

def _local(ts) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    except (TypeError, ValueError):
        return None
    if t is None:
        return None
    return t.astimezone() if t.tzinfo else t.astimezone()


def clock(t: Optional[datetime], now: datetime) -> str:
    """'6:30 PM' today, 'yesterday 6:30 PM', 'Oct 24 6:30 PM'."""
    if t is None:
        return ""
    t, n = t.astimezone(now.tzinfo), now
    c = t.strftime("%I:%M %p").lstrip("0")
    days = (n.date() - t.date()).days
    if days == 0:
        return c
    if days == 1:
        return f"yesterday {c}"
    return f"{t.strftime('%b')} {t.day} {c}"


def _span(a: Optional[datetime], b: Optional[datetime], now: datetime) -> str:
    """'5:39–7:00 PM' within one morning or afternoon, else 'yesterday 11:10 PM – 1:20 AM'."""
    if a is None or b is None:
        return ""
    ca, cb = clock(a, now), clock(b, now)
    a, b = a.astimezone(now.tzinfo), b.astimezone(now.tzinfo)
    if a.date() == b.date() == now.date() and ca[-2:] == cb[-2:]:
        return f"{ca[:-3]}–{cb}"
    return f"{ca} – {cb}"


def describe(name: str, entry: dict, now: datetime, admin: bool = False) -> Optional[dict]:
    """The message for one failing provider's state entry, or None.
    {"kind", "provider", "since", "title", "text"}."""
    if entry.get("state") != "failing":
        return None
    case = entry.get("case") or _fallback_case(entry)
    since = _local(entry.get("since"))
    at = f" since {clock(since, now)}" if since else ""
    detail = entry.get("case_detail") or ""
    if case == "down":
        why = {"server error": "their server has an error",
               "not sending channels": "their server isn't sending any channel"}.get(detail,
                                                                                    "their server isn't answering")
        title = f"{name} is down right now"
        text = (f"{name} is down right now ({why}){at}. Not a problem with your account or your setup. "
                f"Live TV comes back by itself.")
        if admin and entry.get("other_route"):
            text += " Another of its addresses answers: Tentacle switches to it if it works."
    elif case == "blocked":
        title = f"{name} is refusing this home's connection"
        text = f"{name} is refusing this home's connection{at}. Your account is fine."
        if admin:
            text += (" Tentacle switches to another of its addresses when one works; "
                     "add the addresses they sent in Sources → the provider → Address.")
        else:
            text += " Live TV is off until it's sorted out."
    elif case == "account":
        if detail == "expired":
            exp = entry.get("expired_on") or ""
            title = f"The {name} account has expired"
            text = (f"The {name} account has expired{f' ({exp})' if exp else ''}. "
                    f"Live TV is off until it's renewed.")
        elif detail == "login rejected":
            title = f"{name} doesn't accept the login"
            text = (f"{name} doesn't accept the login{at}. Live TV is off until it's sorted out"
                    f"{': check the username and password in Sources' if admin else ''}.")
        else:
            word = detail or "not active"
            title = f"The {name} account is {word}"
            text = f"The {name} account is {word}{at}. Live TV is off until it's sorted out."
    else:
        title = f"{name} isn't working right now"
        text = f"{name} isn't working right now{at}."
        if admin and entry.get("reason"):
            text += f" {entry['reason']}."
    return {"kind": case, "provider": name, "since": entry.get("since"), "title": title, "text": text}


def _fallback_case(entry: dict) -> str:
    """A state saved before cases existed (or by a sync's note): the same
    rules on what was kept."""
    return diagnose({"kind": entry.get("kind"), "code": entry.get("code"), "step": entry.get("step"),
                     "detail": ""}, [])["case"]


def _back(name: str, entry: dict, now: datetime) -> Optional[dict]:
    back = entry.get("back") if isinstance(entry.get("back"), dict) else None
    if entry.get("state") == "failing" or not back:
        return None
    a, b = _local(back.get("since")), _local(back.get("until"))
    if b is None or now - b > BACK_SHOWN or now < b - timedelta(minutes=1):
        return None
    span = _span(a, b, now)
    was = {"blocked": "refused this home's connection", "account": "had an account problem"}.get(
        back.get("case") or "", "was down")
    return {"kind": "back", "provider": name, "since": back.get("since"), "until": back.get("until"),
            "title": f"{name} is back",
            "text": f"{name} is back: Live TV works again (it {was}{f', {span}' if span else ''})."}


def _expiry(name: str, entry: dict, now: datetime) -> Optional[dict]:
    exp = entry.get("expires")
    if entry.get("state") == "failing" or not isinstance(exp, (int, float)):
        return None
    left = datetime.fromtimestamp(exp).astimezone() - now
    if left.total_seconds() <= 0 or left > timedelta(days=EXPIRY_SOON_DAYS):
        return None
    when = datetime.fromtimestamp(exp).astimezone()
    days = max(1, round(left.total_seconds() / 86400))
    return {"kind": "account_soon", "provider": name, "until": when.isoformat(),
            "title": f"The {name} account ends soon",
            "text": (f"The {name} account ends on {when.strftime('%b')} {when.day} "
                     f"(in {days} day{'s' if days != 1 else ''}). Renew it to keep Live TV."),
            "admins": True}


def _channels_message(name: str, provider_id: int) -> Optional[dict]:
    names = broken_channels(provider_id)
    if not names:
        return None
    if len(names) == 1:
        lead = f"{names[0]} isn't"
    elif len(names) <= 3:
        lead = f"{', '.join(names[:-1])} and {names[-1]} aren't"
    else:
        lead = f"{', '.join(names[:2])} and {len(names) - 2} more channels aren't"
    return {"kind": "channels", "provider": name, "channels": names,
            "title": f"{len(names)} channel{'s' if len(names) != 1 else ''} not coming through",
            "text": f"{lead} coming through from {name} right now. Other channels work."}


_ORDER = {"down": 0, "blocked": 0, "account": 0, "other": 1, "channels": 2, "back": 3, "account_soon": 4}


def messages(db, admin: bool = False, live_tv_only: bool = False) -> list:
    """What's wrong with the providers now, most serious first:
    [{"area", "kind", "provider", "since", "until"?, "title", "text"}].
    `admin`: admins' wording and the admins-only lines (an account ending)."""
    from services import provider_health as ph
    now = ph._now()
    state = ph.load_state(db)
    out = []
    for p in ph._watched(db):
        if live_tv_only and not p.live_tv_enabled:
            continue
        entry = state.get(str(p.id)) or {}
        area = "livetv" if p.live_tv_enabled else "library"
        for m in (describe(p.name, entry, now, admin), _back(p.name, entry, now),
                  _channels_message(p.name, p.id) if p.live_tv_enabled and entry.get("state") != "failing" else None,
                  _expiry(p.name, entry, now) if admin else None):
            if m:
                out.append({"area": area, **m})
    out.sort(key=lambda m: _ORDER.get(m["kind"], 9))
    return out


def slate_lines(db, provider_id: Optional[int], channel_id: Optional[int] = None) -> Optional[tuple]:
    """The "Not coming through" picture's (title, line, line) for what's
    wrong with this provider now, or None (then the general one)."""
    from models.database import Provider
    from services import provider_health as ph
    if not isinstance(provider_id, int):
        return None
    p = db.get(Provider, provider_id)
    if p is None:
        return None
    entry = ph.load_state(db).get(str(p.id)) or {}
    now = ph._now()
    name = p.name[:30]
    if entry.get("state") == "failing":
        case = entry.get("case") or _fallback_case(entry)
        since = _local(entry.get("since"))
        at = f" (since {clock(since, now)})" if since else ""
        if case == "down":
            return (f"{name} is down", f"Their server isn't answering{at}",
                    "Not your setup. Live TV comes back by itself")
        if case == "blocked":
            return ("Not coming through", f"{name} is refusing this home's connection",
                    "Your account is fine. Try again later")
        if case == "account":
            word = "has expired" if entry.get("case_detail") == "expired" else "isn't active"
            return ("Live TV is off", f"The {name} account {word}", "Live TV is off until it's sorted out")
        return ("Not coming through", f"{name} isn't working right now{at}", "Try again later")
    if channel_id is not None and channel_broken(channel_id):
        return ("Not coming through", f"{name} isn't sending this channel right now",
                "Other channels work. Try another one")
    return None


# ─── Telling the clients ─────────────────────────────────────────────────────

_timer = {"t": None}
_timer_lock = threading.Lock()


def _changed() -> None:
    """Something that may change messages(): looked at in a moment (a burst
    of channel failures is one look) and, when the words changed, every open
    Tentacle tab and Jellyfin client hears "status". Nothing while there is
    nobody to tell (the tests, a start-up)."""
    from services import live_updates
    if not live_updates.active():
        return
    with _timer_lock:
        if _timer["t"] is not None:
            return
        t = threading.Timer(1.0, _publish_if_changed)
        t.daemon = True
        _timer["t"] = t
        t.start()


def changed() -> None:
    _changed()


def _publish_if_changed() -> None:
    with _timer_lock:
        _timer["t"] = None
    try:
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            sig = repr([(m["kind"], m["text"]) for m in messages(db)])
        finally:
            db.close()
        if sig == _sig["value"]:
            return
        first = _sig["value"] is None
        _sig["value"] = sig
        if first and sig == "[]":
            return
        from services import live_updates
        live_updates.publish("status", everyone=True)
        live_updates.tell_clients("status", everyone=True)
    except Exception as e:
        logger.debug(f"[Provider status] could not tell the clients: {e}")
