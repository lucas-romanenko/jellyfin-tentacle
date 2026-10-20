"""
Tentacle - Several addresses per provider

When a provider's address stops working (an ISP blocking one IP on game
nights, a host that moved), the provider usually sends a message with a
handful of others. The provider card keeps that list: one address is in use
(the provider's server_url, which everything else reads), the rest are
backups.

- add_from_text: paste the provider's whole message; the addresses are picked
  out of it (scheme://host[:port] only: a path or a login in it is dropped).
- test_address: from the server, one address at a time: the name lookup,
  Cloudflare or not, the login (account status, connections in use / max),
  then the time to the first bytes of a live channel and of a film, with the
  provider's User-Agent. The stream tests are skipped while anyone uses the
  account (Tentacle's own streams, Jellyfin playing, or the provider counting
  a connection in use): a one-connection account cuts a stream for a second
  connection.
- The hourly provider health check also checks the backups
  (check_backups), so the ranking covers the evening, when blocks happen.
- switch_to: "Use this one", or by itself when the address in use is failing
  (two checks in a row, services.provider_health) and a backup works. Same
  provider, so nothing is imported again and nothing is deleted:
  * Live TV channels are pointed at the new address (rewrite_xtream_channel_
    urls); their ids, numbers, guide and recordings stay as they are.
  * .strm files that play through Tentacle (the default) hold Tentacle's own
    URL and read the provider's address when played: no file changes. Direct
    files are rewritten by provider_relink, as for an edit.
  * A stream or recording already running keeps the address it opened
    with and finishes on it.
  * The sync must never read a failed or partial listing on the new address
    as "titles gone": marks from the old address's listings are cleared, and
    until one sync on the new address read everything (fetch_ok, at least
    PROBATION_SEEN_SHARE of the known titles) nothing is marked or pruned
    (may_prune / end_probation).

Reasons and words never carry a login: they are shown on the dashboard and
sent to the phone.
"""

import json
import logging
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

from models.database import (
    SessionLocal, Provider, ProviderAddress, Movie, Series, ProviderCategory,
    get_setting, log_activity, tentacle_address,
)

logger = logging.getLogger(__name__)

HISTORY_MAX = 96                 # four days of hourly checks
HISTORY_DAY = timedelta(hours=24)
API_TIMEOUT = (8, 12)
STREAM_TIMEOUT = (8, 12)
FIRST_BYTES = 65536
PAUSE_BETWEEN_CONNECTIONS = 3.0  # the account frees a closed connection after a moment
AUTO_SWITCH_MODES = ("switch", "switch_back", "off")
# A switch back to the preferred address waits for this many good checks in a
# row of it, so a flapping address isn't switched to and from every hour.
SWITCH_BACK_AFTER = 2
# After a switch, the first sync that reads everything ends the probation;
# "everything" is at least this share of the titles the provider had.
PROBATION_SEEN_SHARE = 0.95
PROBATION_KEY = "provider_address_probation_{}"
TEST_STATE_KEY = "provider_address_test_{}"

# Words, as the table shows them
WORKS = "works"
LOGIN_ONLY = "login works"

_test_lock = threading.Lock()     # one manual test at a time (one connection at a time)
_progress: dict = {}              # provider id -> {"running", "done", "total", "current"}


# ─── Addresses ────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"'(),]+")
# A bare host the provider wrote without http:// ("bearsclub.online",
# "line.example.com:8080"). Not part of an e-mail address or a longer URL.
_HOST_RE = re.compile(r"(?i)(?<![@\w./:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?::\d{2,5})?)(?![\w@-]|\.\w)")
_NOT_ADDRESSES = re.compile(r"(?i)\.(png|jpe?g|gif|txt|html?|php|m3u8?|ts|mp4|mkv)$")


def normalize(url: str) -> str:
    """scheme://host[:port] in lower case, or "" for something that is not an
    http(s) address. A path, a query or a login in it is dropped."""
    text = (url or "").strip().rstrip("/.,;")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        u = urlparse(text)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return ""
    if u.scheme.lower() not in ("http", "https") or not host or "." not in host and host != "localhost":
        return ""
    default = 443 if u.scheme.lower() == "https" else 80
    return f"{u.scheme.lower()}://{host}" + (f":{port}" if port and port != default else "")


def find_addresses(text: str) -> list:
    """The provider addresses in a pasted message, in the order written, once
    each."""
    text = text or ""
    found = []
    blanked = list(text)
    for m in _URL_RE.finditer(text):
        found.append((m.start(), m.group(0)))
        blanked[m.start():m.end()] = [" "] * (m.end() - m.start())
    for m in _HOST_RE.finditer("".join(blanked)):
        if not _NOT_ADDRESSES.search(m.group(1)):
            found.append((m.start(), m.group(1)))
    out = []
    for _, raw in sorted(found):
        url = normalize(raw)
        if url and url not in out:
            out.append(url)
    return out


def ensure_current(db, p: Provider) -> None:
    """The address in use is always in the list. Doesn't commit."""
    if (p.provider_type or "xtream") != "xtream":
        return
    url = normalize(p.server_url)
    if url and not any(normalize(a.url) == url for a in p.addresses):
        p.addresses.append(ProviderAddress(url=url, history=[]))


def add_from_text(db, p: Provider, text: str) -> dict:
    """Add every address found in `text`. {"added": [...], "known": [...]}."""
    ensure_current(db, p)
    have = {normalize(a.url) for a in p.addresses}
    added, known = [], []
    for url in find_addresses(text):
        if url in have:
            known.append(url)
            continue
        p.addresses.append(ProviderAddress(url=url, history=[]))
        have.add(url)
        added.append(url)
    db.commit()
    return {"added": added, "known": known}


def remove(db, p: Provider, address_id: int) -> None:
    a = next((a for a in p.addresses if a.id == address_id), None)
    if a is None:
        raise LookupError("No such address")
    if normalize(a.url) == normalize(p.server_url):
        raise ValueError("That is the address in use: switch to another one first")
    if normalize(p.preferred_address) == normalize(a.url):
        p.preferred_address = None
    p.addresses.remove(a)
    db.delete(a)
    db.commit()


def is_current(p: Provider, a: ProviderAddress) -> bool:
    return normalize(a.url) == normalize(p.server_url)


# ─── One address, tested ──────────────────────────────────────────────────────

def _someone_watching(db) -> bool:
    from services.provider_activity import jellyfin_playing, live_streams_active, recording_protected
    try:
        return live_streams_active() or recording_protected(db) or jellyfin_playing(db)
    except Exception:
        return True     # can't tell: don't open a connection


def _cloudflare(headers) -> bool:
    keys = {k.lower() for k in headers}
    return "cf-ray" in keys or (headers.get("Server") or "").lower() == "cloudflare"


def _how_unreachable(e: Exception) -> str:
    text = str(e).lower()
    if isinstance(e, requests.Timeout):
        return "no answer"
    if "refused" in text:
        return "connection refused"
    if any(s in text for s in ("name or service not known", "name resolution", "failed to resolve",
                               "getaddrinfo", "nodename nor servname", "no address associated")):
        return "name not found"
    if "ssl" in text or "certificate" in text:
        return "secure connection failed"
    return "no answer"


def _refused_word(code: int) -> str:
    if code == 511:
        return "refused (511): refuses this connection, as for a VPN"
    if code in (401, 403):
        return f"refused ({code})"
    if code in (429, 509):
        return f"busy ({code}): too many connections"
    if code in (404, 410):
        return f"not found ({code})"
    if code >= 500:
        return f"server error ({code})"
    return f"refused ({code})"


def _login(base: str, p: Provider, ua: str) -> dict:
    from services.xtream_client import quote_cred
    url = f"{base}/player_api.php?username={quote_cred(p.username)}&password={quote_cred(p.password)}"
    t = time.monotonic()
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=API_TIMEOUT)
    except requests.RequestException as e:
        return {"ok": False, "word": _how_unreachable(e)}
    try:
        seconds = round(time.monotonic() - t, 1)
        out = {"seconds": seconds, "cloudflare": _cloudflare(r.headers), "code": r.status_code}
        if r.status_code != 200:
            return {**out, "ok": False, "word": _refused_word(r.status_code)}
        try:
            data = r.json()
        except ValueError:
            return {**out, "ok": False, "word": "answers with a web page, not the provider"}
    finally:
        r.close()
    info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return {**out, "ok": False, "word": "answers, but not as the provider"}
    if info.get("auth") in (0, "0", False):
        return {**out, "ok": False, "word": "login rejected"}
    status = re.sub(r"[^\w ]", "", str(info.get("status") or "")).strip()[:20]

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    out.update(status=status or None, active=_int(info.get("active_cons")), max=_int(info.get("max_connections")))
    if status and status.lower() != "active":
        return {**out, "ok": False, "word": f"account {status.lower()}"}
    return {**out, "ok": True}


def _first_bytes(url: str, ua: str) -> dict:
    t = time.monotonic()
    try:
        with requests.get(url, headers={"User-Agent": ua, "Range": f"bytes=0-{FIRST_BYTES - 1}"},
                          timeout=STREAM_TIMEOUT, stream=True, allow_redirects=True) as r:
            if r.status_code not in (200, 206):
                return {"ok": False, "word": _refused_word(r.status_code), "code": r.status_code}
            for chunk in r.iter_content(8192):
                if chunk:
                    return {"ok": True, "seconds": round(time.monotonic() - t, 1)}
            return {"ok": False, "word": "sends nothing"}
    except requests.RequestException as e:
        return {"ok": False, "word": _how_unreachable(e)}


def _stream_urls(db, p: Provider, base: str) -> dict:
    """{"live": url, "vod": url} on `base`, from the provider's own channels
    and films (never through Tentacle's /api/vod)."""
    from models.database import LiveChannel
    from services.xtream_client import quote_cred
    from services import vod_tokens
    from services.stream_health import _read_strm, _parse_stream_id
    login = f"{quote_cred(p.username)}/{quote_cred(p.password)}"
    out = {}
    ch = (db.query(LiveChannel).filter(LiveChannel.provider_id == p.id, LiveChannel.stream_id.isnot(None))
          .order_by(LiveChannel.enabled.desc(), LiveChannel.id).first())
    if ch is not None and ch.stream_id:
        m = re.search(r"\.(ts|m3u8)$", ch.stream_url or "")
        ext = m.group(1) if m else "ts"
        out["live"] = f"{base}/live/{login}/{ch.stream_id}.{ext}"
    for m in (db.query(Movie).filter(Movie.provider_id == p.id, Movie.strm_path.isnot(None))
              .order_by(Movie.id).limit(20).all()):
        if m.strm_disabled:
            continue
        url = _read_strm(m.strm_path)
        kind, stream_id = _parse_stream_id(url or "")
        if kind != "movie" or not stream_id:
            continue
        container = (url.rsplit(".", 1)[-1].split("?")[0] if "." in url else "mp4") or "mp4"
        if vod_tokens.is_vod_url(url) or re.match(r"^[A-Za-z0-9]{2,5}$", container):
            out["vod"] = f"{base}/movie/{login}/{stream_id}.{container}"
            break
    return out


def test_address(db, p: Provider, url: str, streams: bool = True, sleep=time.sleep) -> dict:
    """What one address answers, from this server. Never raises.

    {"at", "ok", "word", "ip", "cloudflare", "login": {...}, "live": {...},
     "vod": {...}, "streams_skipped": why or None}"""
    ua = p.user_agent or "TiviMate/4.7.0 (Linux; Android 12)"
    base = normalize(url)
    result = {"at": datetime.utcnow().isoformat() + "Z", "ok": False, "word": "", "ip": None,
              "cloudflare": None, "login": None, "live": None, "vod": None, "streams_skipped": None}
    host = urlparse(base).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
        ips = sorted({i[4][0] for i in infos})
        result["ip"] = ", ".join(ips[:3])
    except (OSError, UnicodeError):
        result["word"] = "name not found"
        return result
    login = _login(base, p, ua)
    result["login"] = {k: v for k, v in login.items() if k != "cloudflare"}
    result["cloudflare"] = login.get("cloudflare")
    if not login.get("ok"):
        result["word"] = login.get("word") or "no answer"
        return result
    why_not = None
    if not streams:
        why_not = "not tested this time"
    elif (login.get("active") or 0) > 0:
        why_not = "the account is in use"
    elif _someone_watching(db):
        why_not = "someone is watching"
    if why_not:
        result.update(ok=True, word=LOGIN_ONLY, streams_skipped=why_not)
        return result
    urls = _stream_urls(db, p, base)
    if not urls:
        result.update(ok=True, word=LOGIN_ONLY, streams_skipped="no channel or film to try yet")
        return result
    first = True
    for kind in ("live", "vod"):
        if kind not in urls:
            continue
        if not first:
            sleep(PAUSE_BETWEEN_CONNECTIONS)
            if _someone_watching(db):
                result["streams_skipped"] = "someone started watching"
                break
        first = False
        result[kind] = _first_bytes(urls[kind], ua)
    tried = [result[k] for k in ("live", "vod") if result[k]]
    if tried and all(t["ok"] for t in tried):
        result.update(ok=True, word=WORKS)
    elif tried:
        bad = next(t for t in tried if not t["ok"])
        result.update(ok=False, word=f"login works, streams {bad['word']}")
    else:
        result.update(ok=True, word=LOGIN_ONLY)
    return result


def _record(a: ProviderAddress, result: dict) -> None:
    a.last_result = result
    hist = list(a.history or [])
    hist.append({"at": result["at"], "ok": bool(result["ok"]), "word": result["word"]})
    a.history = hist[-HISTORY_MAX:]


# ─── The ranked table ─────────────────────────────────────────────────────────

def _day_counts(a: ProviderAddress, now: datetime) -> tuple:
    good = bad = 0
    for h in a.history or []:
        try:
            at = datetime.fromisoformat(str(h.get("at", "")).rstrip("Z"))
        except ValueError:
            continue
        if now - at <= HISTORY_DAY:
            if h.get("ok"):
                good += 1
            else:
                bad += 1
    return good, bad


def _score(a: ProviderAddress, now: datetime) -> tuple:
    """Sort key: works now, then worked most over the last day, then the
    quickest start."""
    r = a.last_result or {}
    good, bad = _day_counts(a, now)
    rank = 0 if r.get("word") == WORKS else 1 if r.get("ok") else 2 if r else 3
    share = good / (good + bad) if good + bad else 0
    times = [x.get("seconds") for x in (r.get("live"), r.get("vod"), r.get("login")) if isinstance(x, dict)]
    speed = sum(t for t in times if isinstance(t, (int, float))) if times else 99
    return (rank, -share, speed)


def ranked(db, p: Provider) -> list:
    """The table the card shows, best first."""
    ensure_current(db, p)
    now = datetime.utcnow()
    rows = []
    for a in sorted(p.addresses, key=lambda a: _score(a, now)):
        good, bad = _day_counts(a, now)
        r = a.last_result or {}
        rows.append({
            "id": a.id, "url": a.url, "in_use": is_current(p, a),
            "preferred": bool(p.preferred_address) and normalize(p.preferred_address) == normalize(a.url),
            "tested_at": r.get("at"), "ok": r.get("ok"), "word": r.get("word") or "",
            "ip": r.get("ip"), "cloudflare": r.get("cloudflare"),
            "login_seconds": (r.get("login") or {}).get("seconds"),
            "connections": ({"active": (r.get("login") or {}).get("active"), "max": (r.get("login") or {}).get("max")}
                            if (r.get("login") or {}).get("ok") else None),
            "live_seconds": (r.get("live") or {}).get("seconds") if (r.get("live") or {}).get("ok") else None,
            "vod_seconds": (r.get("vod") or {}).get("seconds") if (r.get("vod") or {}).get("ok") else None,
            "live_word": None if not r.get("live") or r["live"].get("ok") else r["live"].get("word"),
            "vod_word": None if not r.get("vod") or r["vod"].get("ok") else r["vod"].get("word"),
            "streams_skipped": r.get("streams_skipped"),
            "day_good": good, "day_bad": bad,
        })
    return rows


# ─── "Test all" from the card ─────────────────────────────────────────────────

def progress(provider_id: int) -> dict:
    return dict(_progress.get(provider_id) or {"running": False})


def test_all(provider_id: int, sleep=time.sleep) -> bool:
    """Test every address of a provider, one at a time. False when a test (or
    the hourly check) is already running: two would be two connections."""
    from services import provider_health
    if not _test_lock.acquire(blocking=False):
        return False
    if not provider_health._run_lock.acquire(blocking=False):
        _test_lock.release()
        return False
    db = SessionLocal()
    try:
        p = db.get(Provider, provider_id)
        if p is None:
            return True
        ensure_current(db, p)
        db.commit()
        ids = [a.id for a in p.addresses]
        _progress[provider_id] = {"running": True, "done": 0, "total": len(ids), "current": None}
        for n, aid in enumerate(ids):
            a = db.get(ProviderAddress, aid)
            if a is None:
                continue
            _progress[provider_id].update(current=a.url, done=n)
            if n:
                sleep(PAUSE_BETWEEN_CONNECTIONS)
            _record(a, test_address(db, p, a.url, sleep=sleep))
            db.commit()
        logger.info(f"[Addresses] {p.name}: tested {len(ids)} address(es)")
        return True
    except Exception:
        logger.exception("[Addresses] test failed")
        return True
    finally:
        _progress[provider_id] = {"running": False, "finished_at": datetime.utcnow().isoformat() + "Z"}
        db.close()
        provider_health._run_lock.release()
        _test_lock.release()


def start_test_all(provider_id: int) -> bool:
    if _test_lock.locked():
        return False
    _progress[provider_id] = {"running": True, "done": 0, "total": 0, "current": None}
    threading.Thread(target=test_all, args=(provider_id,), daemon=True,
                     name=f"address-test-{provider_id}").start()
    return True


# ─── Switching ────────────────────────────────────────────────────────────────

def switch_to(db, p: Provider, url: str, why: str, by_hand: bool) -> dict:
    """Make `url` the address in use. The same provider: its rows, files,
    channels, guide and recordings all stay (see the module docstring)."""
    from routers.livetv import _xtream_login, rewrite_xtream_channel_urls
    from services import provider_relink
    new = normalize(url)
    if not new:
        raise ValueError("Not an address")
    old = normalize(p.server_url)
    if by_hand:
        p.preferred_address = new
    ensure_current(db, p)
    if not any(normalize(a.url) == new for a in p.addresses):
        p.addresses.append(ProviderAddress(url=new, history=[]))
    if new == old:
        db.commit()
        return {"switched": False, "channels": 0}
    before = _xtream_login(p)
    m3u_before = p.m3u_url
    p.server_url = new
    channels = rewrite_xtream_channel_urls(p, before, db)
    if provider_relink.address_changed(p, before, m3u_before):
        provider_relink.record_pending(db, p, before)
    _start_probation(db, p)
    db.commit()
    provider_relink.start_relink(p.id, before)
    msg = f"Provider {p.name} now uses {new} instead of {old} ({why})"
    logger.info(f"[Addresses] {msg}")
    log_activity(db, "provider_address", msg)
    if not by_hand:
        from services import pushover
        pushover.send(db, f"Tentacle: {p.name} switched address", f"Now {new}: {why}.",
                      (get_setting(db, "external_url", "") or "").strip() or tentacle_address(db))
    return {"switched": True, "provider_id": p.id, "channels": channels, "from": old, "to": new}


def _start_probation(db, p: Provider) -> None:
    """Marks the old address's listings left on rows no longer count, and the
    sync prunes nothing until a sync on the new address read everything.
    Empty-category strikes start again for the same reason. Doesn't commit."""
    for Model in (Movie, Series):
        db.query(Model).filter(Model.provider_id == p.id, Model.provider_missing_since.isnot(None)) \
            .update({Model.provider_missing_since: None}, synchronize_session=False)
    db.query(ProviderCategory).filter(ProviderCategory.provider_id == p.id,
                                      ProviderCategory.consecutive_empty_syncs > 0) \
        .update({ProviderCategory.consecutive_empty_syncs: 0}, synchronize_session=False)
    from models.database import Setting
    key, value = PROBATION_KEY.format(p.id), json.dumps({
        "since": datetime.utcnow().isoformat(), "address": normalize(p.server_url)})
    row = db.query(Setting).filter(Setting.key == key).first()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def in_probation(db, provider_id: int) -> bool:
    return bool((get_setting(db, PROBATION_KEY.format(provider_id), "") or "").strip())


def may_prune(db, provider: Provider, media_type: str, seen_ids: set, fetch_ok: bool) -> bool:
    """False while the provider is on probation after a switch and this
    listing does not settle it: then nothing is marked or pruned.

    A listing settles it when it read everything (fetch_ok) and served at
    least PROBATION_SEEN_SHARE of the titles the provider has here."""
    if not in_probation(db, provider.id):
        return True
    if not fetch_ok:
        return False
    Model = Movie if media_type == "movie" else Series
    known = db.query(Model).filter(Model.provider_id == provider.id).count()
    if not known:
        return True
    have = {t for (t,) in db.query(Model.tmdb_id).filter(Model.provider_id == provider.id).all()}
    served = len(have & set(seen_ids))
    if served < known * PROBATION_SEEN_SHARE:
        logger.warning(f"[Addresses] {provider.name}: the first {media_type} listing on the new address "
                       f"served {served} of {known} titles; nothing is marked as gone until a full one")
        return False
    return True


def end_probation(db, provider: Provider, settled: bool) -> None:
    """After a sync: a run whose every synced type settled it ends it."""
    if settled and in_probation(db, provider.id):
        from models.database import Setting
        db.query(Setting).filter(Setting.key == PROBATION_KEY.format(provider.id)).delete()
        db.commit()
        logger.info(f"[Addresses] {provider.name}: the new address served a full listing; "
                    f"titles it no longer has are handled as usual from now on")


# ─── The hourly check ─────────────────────────────────────────────────────────

def _word_for(outcome: dict) -> str:
    """The table's short word for what the health check found
    (services.provider_health's outcome), like test_address's."""
    kind, code = outcome.get("kind"), outcome.get("code")
    streams = outcome.get("step") == "stream"
    if kind == "unreachable":
        word = outcome.get("detail") or "no answer"
    elif kind == "login":
        word = "login rejected"
    elif kind in ("expired", "status"):
        word = f"account {(outcome.get('detail') or 'expired') if kind == 'status' else 'expired'}"
    elif kind in ("not_json", "not_m3u"):
        word = "answers with a web page, not the provider"
    elif kind == "streams_gone":
        word = "test streams not found (404)"
    elif kind == "empty":
        word = "sends nothing"
    else:
        word = _refused_word(code) if code else "fails"
    return f"login works, streams {word}" if streams and outcome.get("api_ok") else word


def _backups(p: Provider) -> list:
    return [a for a in p.addresses if not is_current(p, a)]


def check_backups(db, providers, outcomes: dict, state: dict, sleep=time.sleep) -> list:
    """Run by the hourly provider health check after the addresses in use:
    test each backup (one at a time; streams only while nobody watches),
    then switch where the rules say so. Returns what was switched."""
    switched = []
    for p in providers:
        if (p.provider_type or "xtream") != "xtream" or not p.addresses:
            continue
        ensure_current(db, p)
        current = next((a for a in p.addresses if is_current(p, a)), None)
        out = outcomes.get(p.id) or {}
        if current is not None and out.get("verdict") in ("ok", "fail"):
            # What the check itself found for the address in use
            _record(current, {"at": datetime.utcnow().isoformat() + "Z", "ok": out["verdict"] == "ok",
                              "word": WORKS if out["verdict"] == "ok" else _word_for(out),
                              **{k: (current.last_result or {}).get(k) for k in ("ip", "cloudflare", "login", "live", "vod")},
                              "streams_skipped": None})
        backups = _backups(p)
        if not backups:
            db.commit()
            continue
        failing = (state.get(str(p.id)) or {}).get("state") == "failing"
        for a in backups:
            if _someone_watching(db) and not failing:
                break       # the next hour tries again; nothing urgent
            sleep(PAUSE_BETWEEN_CONNECTIONS)
            _record(a, test_address(db, p, a.url, streams=True, sleep=sleep))
            db.commit()
        done = _maybe_switch(db, p, failing)
        if done:
            switched.append(done)
    return switched


def _proven(a: ProviderAddress, now: datetime) -> bool:
    """A backup worth switching to: its streams started at its latest test,
    or its login works now (streams not tried, someone was watching) and its
    streams started at a test in the last day."""
    r = a.last_result or {}
    if r.get("word") == WORKS:
        return True
    if r.get("word") != LOGIN_ONLY:
        return False
    for h in a.history or []:
        try:
            at = datetime.fromisoformat(str(h.get("at", "")).rstrip("Z"))
        except ValueError:
            continue
        if h.get("word") == WORKS and now - at <= HISTORY_DAY:
            return True
    return False


def _maybe_switch(db, p: Provider, failing: bool):
    mode = p.address_auto_switch if p.address_auto_switch in AUTO_SWITCH_MODES else "switch"
    if mode == "off":
        return None
    now = datetime.utcnow()
    good = [a for a in _backups(p) if _proven(a, now)]
    if failing and good:
        best = sorted(good, key=lambda a: _score(a, now))[0]
        return switch_to(db, p, best.url, "the address in use failed its checks and this one works", by_hand=False)
    if not failing and mode == "switch_back" and p.preferred_address \
            and normalize(p.preferred_address) != normalize(p.server_url):
        pref = next((a for a in p.addresses if normalize(a.url) == normalize(p.preferred_address)), None)
        recent = (pref.history or [])[-SWITCH_BACK_AFTER:] if pref else []
        if len(recent) == SWITCH_BACK_AFTER and all(h.get("ok") for h in recent) \
                and (pref.last_result or {}).get("word") == WORKS:
            return switch_to(db, p, pref.url, "your chosen address works again", by_hand=False)
    return None
