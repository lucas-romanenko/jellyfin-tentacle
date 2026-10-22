"""Problems go to the admin, plain words to the family (Roadmap 1c).

Every 10 minutes Tentacle looks at what can break for the household: an
IPTV provider failing, Radarr/Sonarr unreachable, a disk filling up,
downloads stuck, Live TV not set up in Jellyfin, Jellyfin itself down. A new
problem is told to every admin once (a message in Jellyfin, plus Pushover
when it's set up), and once more when it's over. Nothing goes to the family:
they see one plain line where it matters ("Live TV is unavailable right now.
Lucas knows."), from family_messages().

Provider failures already reach Pushover from provider_health (with its own
confirm-and-retry logic); here they only become an alert and a message in
Jellyfin.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.database import AdminAlert, Provider, TentacleUser, create_notification

logger = logging.getLogger(__name__)

AREAS = ("livetv", "library", "requests", "downloads", "server")


def _admins(db: Session) -> list:
    return db.query(TentacleUser).filter(TentacleUser.is_admin == True).all()  # noqa: E712


def _tell_admins(db: Session, title: str, message: str, push: bool) -> None:
    from services import live_updates, pushover
    from services.approvals import _link
    live_updates.publish("health", admins=True)
    for admin in _admins(db):
        try:
            create_notification(db, admin.id, 0, "movie", title, message, kind="problem", force=True)
        except Exception as e:
            db.rollback()
            logger.warning(f"[Alerts] Could not tell {admin.display_name}: {e}")
    if push:
        link = _link(db).replace("/#requests", "/#health")
        pushover.send(db, f"Tentacle: {title}", message, link)


def raise_alert(db: Session, key: str, area: str, title: str, message: str, push: bool = True) -> bool:
    """A problem seen now. True when it's new (the admins were told)."""
    now = datetime.utcnow()
    row = db.query(AdminAlert).filter(AdminAlert.key == key).first()
    if row is not None and row.active:
        row.last_seen_at = now
        row.message = message[:500]
        db.commit()
        return False
    if row is None:
        row = AdminAlert(key=key)
        db.add(row)
    row.area, row.title, row.message = area, title[:200], message[:500]
    row.active, row.started_at, row.last_seen_at, row.resolved_at = True, now, now, None
    db.commit()
    logger.warning(f"[Alerts] {title}: {message}")
    _tell_admins(db, title, message, push)
    return True


def clear_alert(db: Session, key: str, fixed_message: str = "", push: bool = True) -> bool:
    """A problem gone. True when it was active (the admins were told)."""
    row = db.query(AdminAlert).filter(AdminAlert.key == key, AdminAlert.active == True).first()  # noqa: E712
    if row is None:
        return False
    row.active, row.resolved_at = False, datetime.utcnow()
    db.commit()
    logger.info(f"[Alerts] Over: {row.title}")
    _tell_admins(db, f"Fixed: {row.title}", fixed_message or "It works again.", push)
    return True


def active(db: Session, area: Optional[str] = None) -> list:
    q = db.query(AdminAlert).filter(AdminAlert.active == True)  # noqa: E712
    if area:
        q = q.filter(AdminAlert.area == area)
    return q.order_by(AdminAlert.started_at).all()


# ── What is wrong now ───────────────────────────────────────────────────────

def _seen_providers(db: Session) -> dict:
    """key -> (area, title, message, push) for failing providers."""
    from services import provider_health
    out = {}
    state = provider_health.load_state(db)
    for p in db.query(Provider).all():
        entry = state.get(str(p.id)) or {}
        if entry.get("state") != "failing" or not (p.active or p.live_tv_enabled):
            continue
        area = "livetv" if p.live_tv_enabled else "library"
        out[f"provider:{p.id}"] = (area, f"{p.name} is failing",
                                   entry.get("reason") or "The provider doesn't answer.", False)
    return out


def _seen_arr(db: Session) -> dict:
    from services import arr_insight
    out = {}
    for prob in arr_insight.problems(db):
        if prob.get("kind") == "unreachable":
            out[f"arr:{prob['app'].lower()}"] = ("requests", f"{prob['app']} can't be reached",
                                                 prob.get("message") or "", True)
        elif prob.get("kind") == "disk" and prob.get("level") == "error":
            out[f"disk:{prob.get('disk')}"] = ("downloads", "A disk is almost full", prob.get("message") or "", True)
    return out


def _seen_downloads(db: Session) -> dict:
    from services import download_health as D
    stuck = 0
    stall_state = D.get_stall_state(db)
    for app in ("radarr", "sonarr"):
        url, key = D._arr_conn(db, app)
        if not url or not key:
            continue
        try:
            records = D.fetch_queue_records(url, key, app)
        except Exception:
            continue    # unreachable is its own alert
        stuck += sum(1 for r in records if D.classify_queue_item(r, stall_state)["status"] in ("stuck", "import_blocked"))
    if not stuck:
        return {}
    noun = "download is" if stuck == 1 else "downloads are"
    return {"downloads:stuck": ("downloads", f"{stuck} {noun} stuck",
                                "Open Tentacle → Health → Downloads to fix or remove them.", True)}


def _seen_livetv(db: Session) -> dict:
    from services import livetv_tuners
    st = livetv_tuners.last_status(db)
    if st.get("last_ok") is False and not st.get("running"):
        return {"livetv:setup": ("livetv", "Live TV isn't set up in Jellyfin",
                                 st.get("last_message") or "Tentacle couldn't set the tuners up.", True)}
    return {}


def _seen_jellyfin(db: Session) -> dict:
    import requests
    from services.livetv_tuners import jellyfin_for
    jf = jellyfin_for(db)
    if jf is None:
        return {}
    try:
        jf.get("/System/Info/Public")
        return {}
    except requests.RequestException:
        return {"jellyfin:down": ("server", "Jellyfin can't be reached",
                                  "Nobody can watch anything until Jellyfin is back.", True)}


CHECKS = (_seen_providers, _seen_arr, _seen_downloads, _seen_livetv, _seen_jellyfin)


def check(db: Session) -> dict:
    """Raise what is wrong, clear what was wrong and isn't any more. A check
    that fails itself leaves its alerts as they are."""
    seen, owned_prefixes = {}, set()
    for fn in CHECKS:
        try:
            found = fn(db)
        except Exception as e:
            logger.debug(f"[Alerts] {fn.__name__} failed: {e}")
            continue
        seen.update(found)
        owned_prefixes.add(fn.__name__)
    raised = sum(raise_alert(db, k, *v) for k, v in seen.items())
    prefix_of = {"_seen_providers": "provider:", "_seen_arr": "arr:", "_seen_downloads": "downloads:",
                 "_seen_livetv": "livetv:", "_seen_jellyfin": "jellyfin:"}
    checked = tuple(prefix_of[n] for n in owned_prefixes)
    cleared = 0
    for row in active(db):
        if row.key in seen:
            continue
        if row.key.startswith(checked) or (row.key.startswith("disk:") and "_seen_arr" in owned_prefixes):
            push = not row.key.startswith("provider:")
            cleared += clear_alert(db, row.key, push=push)
    return {"raised": raised, "cleared": cleared, "active": len(active(db))}


def run_check() -> None:
    """The scheduled job."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        check(db)
    except Exception:
        logger.exception("[Alerts] Check failed")
    finally:
        db.close()


# ── Plain words for the family ──────────────────────────────────────────────

_FAMILY_TEXT = {
    "livetv": "Live TV is unavailable right now. {who} knows.",
    "library": "Some movies and shows may not play right now. {who} knows.",
    "requests": "New requests will wait a little: downloads are paused for now. {who} knows.",
    "downloads": "New downloads are paused for now. {who} knows.",
    "server": "Tentacle is having trouble right now. {who} knows.",
}


def family_messages(db: Session) -> list:
    """One plain line per area with an active problem: no addresses, codes or
    app names. Same for everyone (admins see the details on Health)."""
    from services.approvals import approver_name
    who = approver_name(db)
    if who == "your admin":
        who = "Your admin"
    areas = []
    for row in active(db):
        if row.area not in areas:
            areas.append(row.area)
    return [{"area": a, "text": _FAMILY_TEXT[a].format(who=who)} for a in AREAS if a in areas]
