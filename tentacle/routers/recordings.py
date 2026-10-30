"""Recordings: recording rules ("My teams" and typed words), what will
record and what did, and whether Jellyfin is recording (or about to) for
anything that restarts Tentacle or Jellyfin.

A recording isn't a playback session: Jellyfin's /Sessions shows nothing
while it records, and nothing in the minutes of pre-padding before. A deploy
or an updater that waits for "nothing playing" would restart the server in
the middle of a game. GET /api/live/recordings/busy says when it is safe.
"""
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.database import (RecordingBooking, RecordingRule, RecordingRuleChannel, RecordingRuleTerm,
                             TentacleUser, get_db, get_setting)
from routers.auth import get_user_from_request, require_admin, require_internal_or_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# A timer whose recording (start minus pre-padding) begins within this many
# minutes counts as busy: a restart takes a minute or two, and a timer that
# fires while Jellyfin is down is lost.
BUSY_SOON_MINUTES = 20


def busy_from_timers(items: list, now: datetime, soon_minutes: int = BUSY_SOON_MINUTES) -> dict:
    """What Jellyfin's timers say: recording now (InProgress, or a New timer
    already inside its padded window: Jellyfin retrying an open), starting
    within `soon_minutes`, and the next recording's start after that."""
    from routers.livetv import _parse_timer_date, _timer_seconds
    recording, soon, next_start = [], [], None
    for t in items or []:
        status = t.get("Status")
        if status not in ("InProgress", "New"):
            continue
        start = _parse_timer_date(t.get("StartDate"))
        end = _parse_timer_date(t.get("EndDate"))
        if start is None:
            continue
        rec_start = start - timedelta(seconds=_timer_seconds(t.get("PrePaddingSeconds")))
        rec_end = (end or start) + timedelta(seconds=_timer_seconds(t.get("PostPaddingSeconds")))
        if rec_end <= now:
            continue   # a timer Jellyfin never started stays New for ever
        what = {"name": t.get("Name") or "", "channel": t.get("ChannelName") or "",
                "starts": rec_start.isoformat() + "Z", "ends": rec_end.isoformat() + "Z"}
        if status == "InProgress" or rec_start <= now:
            recording.append(what)
        elif rec_start <= now + timedelta(minutes=soon_minutes):
            soon.append(what)
        elif next_start is None or rec_start < next_start:
            next_start = rec_start
    return {"recording": recording, "starting_soon": soon,
            "next_start": next_start.isoformat() + "Z" if next_start else None}


@router.get("/api/live/recordings/busy", dependencies=[Depends(require_internal_or_admin)])
def recordings_busy(soon_minutes: int = Query(BUSY_SOON_MINUTES, ge=0, le=24 * 60),
                    db: Session = Depends(get_db)):
    """busy: Jellyfin is recording, or a recording starts (with its
    pre-padding) within `soon_minutes`; a restart of Tentacle or Jellyfin
    now would cut it. `known` is false when Jellyfin couldn't be asked: a
    caller should treat that as busy (it can't tell)."""
    return busy_now(db, soon_minutes)


def busy_now(db: Session, soon_minutes: int = BUSY_SOON_MINUTES) -> dict:
    """GET /api/live/recordings/busy's answer, for Tentacle's own background
    work too (services/provider_activity.something_busy)."""
    from routers.livetv import _jellyfin_timers, _stream_slots
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    # Tentacle's own broker knows a recording that is pulling a stream now,
    # even when Jellyfin can't be asked.
    pulling = _stream_slots.recording_active()
    out = {"busy": pulling, "known": False, "recording": [], "starting_soon": [], "next_start": None,
           "tentacle_recording_streams": pulling}
    if not url or not key:
        out["reason"] = "Jellyfin isn't connected"
        return out
    try:
        data = _jellyfin_timers(url, key)
    except Exception as e:
        logger.info(f"[Recordings] busy: Jellyfin's timers not read: {e}")
        out["reason"] = "Jellyfin's recordings couldn't be read"
        return out
    out.update(busy_from_timers((data or {}).get("Items") or [], datetime.utcnow(), soon_minutes))
    out["known"] = True
    out["busy"] = bool(pulling or out["recording"] or out["starting_soon"])
    return out


# ── Recording rules: "My teams" and typed words ─────────────────────────────
#
# Who may do what: a rule records into the household's Jellyfin recordings,
# so it needs Jellyfin's own "manage recordings" permission (the same one
# the Record button needs; admins have it, kids never). Anyone signed in
# sees every rule and what is set to record. A person edits their own
# rules; admins edit everyone's.

_RECORD_PERMISSION_TTL = 60.0
_record_permission: dict = {}     # jellyfin user id -> (until, allowed)
_KINDS = ("team", "keywords", "series")
_SHOWN = ("preview", "booked", "recording", "waiting", "covered", "failed", "lost")


def can_record(db: Session, user: Optional[TentacleUser]) -> bool:
    """Admins; anyone else whose Jellyfin account may manage recordings."""
    if user is None or user.is_admin:
        return True
    if user.kids_level:
        return False
    now = time.monotonic()
    hit = _record_permission.get(user.jellyfin_user_id)
    if hit and hit[0] > now:
        return hit[1]
    from services import livetv_tuners as T
    allowed = False
    jf = T.jellyfin_for(db)
    if jf is not None:
        try:
            u = jf.get(f"/Users/{uuid.UUID(user.jellyfin_user_id)}") or {}
            allowed = bool((u.get("Policy") or {}).get("EnableLiveTvManagement"))
        except Exception as e:
            logger.info(f"[Recordings] {user.display_name}'s Jellyfin permissions not read: {e}")
    _record_permission[user.jellyfin_user_id] = (now + _RECORD_PERMISSION_TTL, allowed)
    return allowed


def _caller(request, db) -> Optional[TentacleUser]:
    if db.query(TentacleUser).count() == 0:
        return None      # first-run setup, like require_admin
    return get_user_from_request(request, db)


def _may_edit(user: Optional[TentacleUser], rule: RecordingRule) -> bool:
    return user is None or user.is_admin or rule.owner_user_id == user.id


class RuleBody(BaseModel):
    id: Optional[int] = None        # preview of a saved rule: leave it out of the connections count
    kind: str = "team"
    name: Optional[str] = None
    league: Optional[str] = None
    team: Optional[str] = None
    home_away: str = "both"
    terms: List[str] = Field(default_factory=list)
    excludes: List[str] = Field(default_factory=list)
    match_all: bool = False
    search_description: bool = False
    channels: List[str] = Field(default_factory=list)
    min_minutes: Optional[int] = None
    pre_padding_minutes: Optional[int] = None
    post_padding_minutes: Optional[int] = None
    playoff_extra_minutes: Optional[int] = None
    pregame_next: bool = True
    skip_replays: bool = True
    mode: str = "preview"
    active: bool = True
    priority: int = 0
    keep_forever: bool = False
    keep_last: Optional[int] = None


class RulePatch(BaseModel):
    name: Optional[str] = None
    home_away: Optional[str] = None
    terms: Optional[List[str]] = None
    excludes: Optional[List[str]] = None
    match_all: Optional[bool] = None
    search_description: Optional[bool] = None
    channels: Optional[List[str]] = None
    min_minutes: Optional[int] = None
    pre_padding_minutes: Optional[int] = None
    post_padding_minutes: Optional[int] = None
    playoff_extra_minutes: Optional[int] = None
    pregame_next: Optional[bool] = None
    skip_replays: Optional[bool] = None
    mode: Optional[str] = None
    active: Optional[bool] = None
    priority: Optional[int] = None
    keep_forever: Optional[bool] = None
    keep_last: Optional[int] = None


def _keep_last(v) -> Optional[int]:
    """A rule's "keep the last N": 1 to 999, or none."""
    return max(1, min(int(v), 999)) if v else None


def _clean_terms(terms) -> list:
    out = []
    for t in terms or []:
        t = " ".join(str(t).split())[:80]
        if t and t.casefold() not in {x.casefold() for x in out}:
            out.append(t)
    return out[:30]


def _defaults(body: RuleBody) -> dict:
    """A team or series rule's sports defaults from leagues.json; typed words
    get a short programme's (5 min before, 10 after)."""
    from services.recording_rules import leagues as LG
    lg = LG.league(body.league) if body.kind in ("team", "series") else None
    sports = body.kind != "keywords"
    return {
        "pre": body.pre_padding_minutes if body.pre_padding_minutes is not None else 5,
        "post": body.post_padding_minutes if body.post_padding_minutes is not None else
        int((lg or {}).get("post_padding_minutes") or (60 if sports else 10)),
        "playoff": body.playoff_extra_minutes if body.playoff_extra_minutes is not None else (60 if sports else 0),
    }


def _validate(body: RuleBody) -> str:
    from services.recording_rules import leagues as LG
    if body.kind not in _KINDS:
        raise HTTPException(422, "kind is team, keywords or series")
    if body.mode not in ("preview", "record"):
        raise HTTPException(422, "mode is preview or record")
    if body.home_away not in ("both", "home", "away"):
        raise HTTPException(422, "home_away is both, home or away")
    if body.kind == "team":
        t = LG.team(body.league, body.team)
        if t is None:
            raise HTTPException(422, "No such team in that league")
        return body.name or t["name"]
    if body.kind == "series":
        lg = LG.league(body.league)
        if lg is None or lg.get("kind") != "series":
            raise HTTPException(422, "No such series")
        return body.name or lg["name"]
    if not _clean_terms(body.terms):
        raise HTTPException(422, "Type at least one word to look for")
    return body.name or ", ".join(_clean_terms(body.terms))[:80]


def _rule_json(db: Session, r: RecordingRule, user, owners: dict, counts: dict) -> dict:
    from services.recording_rules import leagues as LG
    terms = db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == r.id).all()
    chans = db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == r.id).all()
    lg = LG.league(r.league)
    tm = LG.team(r.league, r.team)
    return {
        "id": r.id, "name": r.name, "kind": r.kind, "league": r.league, "league_name": (lg or {}).get("name"),
        "team": r.team, "team_name": (tm or {}).get("name"), "home_away": r.home_away,
        "terms": [t.term for t in terms if not t.exclude], "excludes": [t.term for t in terms if t.exclude],
        "match_all": bool(r.match_all), "search_description": bool(r.search_description),
        "channels": [c.pool_key for c in chans], "min_minutes": r.min_minutes,
        "pre_padding_minutes": r.pre_padding_minutes, "post_padding_minutes": r.post_padding_minutes,
        "playoff_extra_minutes": r.playoff_extra_minutes, "pregame_next": bool(r.pregame_next),
        "skip_replays": bool(r.skip_replays), "mode": r.mode, "active": bool(r.active), "priority": r.priority,
        "keep_forever": bool(r.keep_forever), "keep_last": r.keep_last,
        "owner": owners.get(r.owner_user_id), "mine": user is not None and r.owner_user_id == user.id,
        "can_edit": _may_edit(user, r), "upcoming": counts.get(r.id, 0),
        "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
    }


def _owners(db: Session) -> dict:
    return {u.id: u.display_name for u in db.query(TentacleUser).all()}


def _save_lists(db: Session, rule_id: int, terms=None, excludes=None, channels=None) -> None:
    if terms is not None or excludes is not None:
        old = db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == rule_id).all()
        keep_terms = [t.term for t in old if not t.exclude] if terms is None else _clean_terms(terms)
        keep_ex = [t.term for t in old if t.exclude] if excludes is None else _clean_terms(excludes)
        for t in old:
            db.delete(t)
        db.flush()
        for t in keep_terms:
            db.add(RecordingRuleTerm(rule_id=rule_id, term=t, exclude=False))
        for t in keep_ex:
            db.add(RecordingRuleTerm(rule_id=rule_id, term=t, exclude=True))
    if channels is not None:
        db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == rule_id).delete()
        for k in dict.fromkeys(str(c) for c in channels if c):
            db.add(RecordingRuleChannel(rule_id=rule_id, pool_key=k[:64]))


def _changed(db: Session) -> None:
    from services.recording_rules import book
    book.request_scan()
    book.notify()


@router.get("/api/live/recording-rules/leagues")
def recording_leagues(request: Request, db: Session = Depends(get_db)):
    """The built-in leagues and their teams, for the "My teams" picker."""
    from services.recording_rules import leagues as LG
    _caller(request, db)
    return {"leagues": LG.summary()}


@router.get("/api/live/recording-rules")
def list_rules(request: Request, db: Session = Depends(get_db)):
    from services.recording_rules import book
    user = _caller(request, db)
    rows = db.query(RecordingRule).order_by(RecordingRule.created_at, RecordingRule.id).all()
    counts: dict = {}
    for b in db.query(RecordingBooking).filter(RecordingBooking.state.in_(_SHOWN)).all():
        counts[b.rule_id] = counts.get(b.rule_id, 0) + 1
    owners = _owners(db)
    return {"rules": [_rule_json(db, r, user, owners, counts) for r in rows],
            "can_record": can_record(db, user), "is_admin": user is None or bool(user.is_admin),
            "last_scan": book.last_scan()}


@router.post("/api/live/recording-rules")
def create_rule(body: RuleBody, request: Request, db: Session = Depends(get_db)):
    user = _caller(request, db)
    if not can_record(db, user):
        raise HTTPException(403, "Your account can't set recordings (ask an admin: People → Can record)")
    name = _validate(body)
    if body.kind == "team" and db.query(RecordingRule).filter(
            RecordingRule.kind == "team", RecordingRule.league == body.league, RecordingRule.team == body.team,
            RecordingRule.owner_user_id == (user.id if user else None)).first():
        raise HTTPException(409, "You follow this team already")
    d = _defaults(body)
    now = datetime.utcnow()
    r = RecordingRule(name=name[:120], kind=body.kind, league=body.league if body.kind != "keywords" else None,
                      team=body.team if body.kind == "team" else None, home_away=body.home_away,
                      match_all=body.match_all, search_description=body.search_description,
                      min_minutes=body.min_minutes, pre_padding_minutes=d["pre"], post_padding_minutes=d["post"],
                      playoff_extra_minutes=d["playoff"], pregame_next=body.pregame_next,
                      skip_replays=body.skip_replays, mode=body.mode, active=body.active, priority=body.priority,
                      keep_forever=body.keep_forever, keep_last=None if body.keep_forever else _keep_last(body.keep_last),
                      owner_user_id=user.id if user else None, created_at=now, updated_at=now)
    db.add(r)
    db.flush()
    _save_lists(db, r.id, body.terms, body.excludes, body.channels)
    db.commit()
    _changed(db)
    return _rule_json(db, r, user, _owners(db), {})


def _rule_or_404(db, rule_id: int) -> RecordingRule:
    r = db.get(RecordingRule, rule_id)
    if r is None:
        raise HTTPException(404, "No such rule")
    return r


@router.patch("/api/live/recording-rules/{rule_id}")
def update_rule(rule_id: int, body: RulePatch, request: Request, db: Session = Depends(get_db)):
    user = _caller(request, db)
    r = _rule_or_404(db, rule_id)
    if not _may_edit(user, r) or not can_record(db, user):
        raise HTTPException(403, "Only its owner or an admin can change this rule")
    data = body.dict(exclude_unset=True)
    if data.get("mode") is not None and data["mode"] not in ("preview", "record"):
        raise HTTPException(422, "mode is preview or record")
    if data.get("home_away") is not None and data["home_away"] not in ("both", "home", "away"):
        raise HTTPException(422, "home_away is both, home or away")
    for k in ("name", "home_away", "match_all", "search_description", "min_minutes", "pre_padding_minutes",
              "post_padding_minutes", "playoff_extra_minutes", "pregame_next", "skip_replays", "mode", "active",
              "priority"):
        if k in data and (data[k] is not None or k == "min_minutes"):
            v = data[k]
            if k.endswith("_minutes") and v is not None:
                v = max(0, min(int(v), 6 * 60))
            setattr(r, k, v if k != "name" else str(v)[:120])
    if data.get("keep_forever") is not None:
        r.keep_forever = bool(data["keep_forever"])
    if "keep_last" in data:
        r.keep_last = _keep_last(data["keep_last"])
    if r.keep_forever:
        r.keep_last = None
    _save_lists(db, r.id, data.get("terms"), data.get("excludes"), data.get("channels"))
    if r.kind == "keywords" and not db.query(RecordingRuleTerm).filter(
            RecordingRuleTerm.rule_id == r.id, RecordingRuleTerm.exclude == False).count():  # noqa: E712
        db.rollback()
        raise HTTPException(422, "Type at least one word to look for")
    r.updated_at = datetime.utcnow()
    db.commit()
    _changed(db)
    return _rule_json(db, r, user, _owners(db), {})


@router.delete("/api/live/recording-rules/{rule_id}")
def delete_rule(rule_id: int, request: Request, db: Session = Depends(get_db)):
    """The rule goes; its upcoming recordings that haven't started are
    cancelled (by their own timer ids; nothing anyone else set). What it
    recorded stays, and so does the history."""
    user = _caller(request, db)
    r = _rule_or_404(db, rule_id)
    if not _may_edit(user, r):
        raise HTTPException(403, "Only its owner or an admin can remove this rule")
    kept = remove_rule(db, r)
    db.commit()
    _changed(db)
    return {"deleted": rule_id, "kept_recording": kept}


def remove_rule(db: Session, r: RecordingRule) -> int:
    """Remove a rule (not committed): its booked recordings are cancelled,
    the rest of its bookings let go. Returns the bookings Jellyfin wouldn't
    cancel (they still record). Also a restore's Replace (services/backup.py)."""
    from services import livetv_tuners as T
    from services.recording_rules import book
    jf = T.jellyfin_for(db)
    kept = 0
    for b in db.query(RecordingBooking).filter(RecordingBooking.rule_id == r.id).all():
        if b.state in ("booked",):
            if not book.cancel(db, jf, b, "person", f"rule “{r.name}” removed", state="skipped"):
                kept += 1
        elif b.state in ("preview", "waiting", "failed", "lost"):
            db.delete(b)
            continue
        b.rule_id = None
    db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == r.id).delete()
    db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == r.id).delete()
    db.delete(r)
    return kept


@router.post("/api/live/recording-rules/preview")
def preview_rule(body: RuleBody, request: Request, db: Session = Depends(get_db)):
    """What a rule (saved or not) would record in the guide as it is now:
    each game or programme with the channel it would use and the others it
    is also on, replays it would skip, and near misses with the reason."""
    from services.recording_rules import book, guide as G, plan as P, schedule as S
    _caller(request, db)
    name = _validate(body)
    d = _defaults(body)
    rule = P.Rule(id=0, name=name, kind=body.kind, league=body.league, team=body.team, home_away=body.home_away,
                  terms=_clean_terms(body.terms), excludes=_clean_terms(body.excludes), match_all=body.match_all,
                  search_description=body.search_description, min_minutes=body.min_minutes,
                  pre_padding_minutes=d["pre"], post_padding_minutes=d["post"], playoff_extra_minutes=d["playoff"],
                  pregame_next=body.pregame_next, skip_replays=body.skip_replays, channels=set(body.channels),
                  priority=body.priority, mode="preview")
    now = datetime.utcnow()
    items = G.airings(db, now)
    others_rules = [r for r in book.load_rules(db) if r.mode == "record" and r.id != body.id]
    games = S.for_rules([rule] + others_rules)
    # what the saved rule recorded: the same game again is a rerun
    saved = db.get(RecordingRule, body.id) if body.id else None
    mine = [P.Rule(id=saved.id, name=saved.name, mode=saved.mode or "preview")] if saved else []
    done = book.recorded(db, mine + others_rules, now)
    pl = P.plan_rule(rule, items, now, games.get((rule.league, rule.team)), done.get(body.id, ()) if saved else ())
    others = [P.plan_rule(r, items, now, games.get((r.league, r.team)), done.get(r.id, ())) for r in others_rules]
    limits: dict = {}

    def limit_of(pid):
        if pid not in limits:
            from models.database import Provider
            from routers.livetv import provider_stream_limit
            limits[pid] = provider_stream_limit(db, db.get(Provider, pid) if pid else None)
        return limits[pid]

    book.fit_plans(others + [pl], [], limit_of)
    # how far the guide reaches: regular listings (an event channel's name can
    # carry any date)
    guide_until = max((a.stop for a in items if not a.from_event), default=None)
    return {"name": name, "events": [_event_json(e) for e in pl.events],
            "near": [{"airing": _airing_json(n.airing), "reason": n.reason} for n in pl.near],
            "min_minutes": P.min_minutes(rule), "listings": len(items),
            "guide_until": guide_until.isoformat() + "Z" if guide_until else None}


def _airing_json(a) -> dict:
    return {"pool_key": a.pool_key, "channel": a.channel, "title": a.title, "sub_title": a.sub_title,
            "description": (a.description or "")[:300], "start": a.start.isoformat() + "Z",
            "stop": a.stop.isoformat() + "Z", "minutes": int(round(a.minutes)), "from_event": a.from_event}


def _event_json(e) -> dict:
    chosen = e.options[e.chosen] if e.chosen is not None else e.best
    return {"state": e.state, "reason": e.reason, "why": chosen.why, "home": chosen.home,
            "airing": _airing_json(chosen.airing),
            "also": [{"airing": _airing_json(o.airing), "note": o.note} for o in e.options if o is not chosen][:8]}


@router.post("/api/live/recording-rules/run")
def run_rules(request: Request, db: Session = Depends(get_db)):
    """Scan now (also runs every 30 minutes and after each change)."""
    from services.recording_rules import book
    user = _caller(request, db)
    if not can_record(db, user):
        raise HTTPException(403, "Your account can't set recordings")
    return {"summary": book.scan(db), "last_scan": book.last_scan()}


# ── What will record, and what did ──────────────────────────────────────────

def _booking_json(b: RecordingBooking, rules: dict) -> dict:
    r = rules.get(b.rule_id)
    return {
        "id": b.id, "rule_id": b.rule_id, "rule": r.name if r else None,
        "rule_mode": r.mode if r else None, "pool_key": b.pool_key, "channel": b.channel_name,
        "title": b.title, "sub_title": b.sub_title, "description": (b.description or "")[:300],
        "start": b.start.isoformat() + "Z", "stop": b.stop.isoformat() + "Z",
        "pre_padding_minutes": (b.pre_padding_seconds or 0) // 60, "post_padding_minutes": (b.post_padding_seconds or 0) // 60,
        "state": b.state, "reason": b.reason, "why": b.why, "from_event": bool(b.from_event),
        "jellyfin_timer_id": b.jellyfin_timer_id, "jellyfin_channel_id": b.jellyfin_channel_id,
        "jellyfin_program_id": b.jellyfin_program_id,
    }


def _jellyfin_own(db: Session, ours: set) -> list:
    """Recordings people set in Jellyfin themselves (Record, Record series)."""
    from services import livetv_tuners as T
    jf = T.jellyfin_for(db)
    if jf is None:
        return []
    try:
        items = (jf.get("/LiveTv/Timers") or {}).get("Items") or []
    except Exception as e:
        logger.info(f"[Recordings] Jellyfin's recordings not read: {e}")
        return []
    out = []
    for t in items:
        if t.get("Id") in ours or t.get("Status") not in ("New", "InProgress"):
            continue
        out.append({"jellyfin_timer_id": t.get("Id"), "title": t.get("Name"), "channel": t.get("ChannelName"),
                    "start": (t.get("StartDate") or "")[:19] + "Z", "stop": (t.get("EndDate") or "")[:19] + "Z",
                    "state": "recording" if t.get("Status") == "InProgress" else "booked",
                    "series": bool(t.get("SeriesTimerId")), "jellyfin_channel_id": (t.get("ChannelId") or "").replace("-", ""),
                    "jellyfin_program_id": t.get("ProgramId"),
                    "pre_padding_minutes": int(float(t.get("PrePaddingSeconds") or 0) // 60),
                    "post_padding_minutes": int(float(t.get("PostPaddingSeconds") or 0) // 60),
                    "reason": "Set in Jellyfin" + (" (a series)" if t.get("SeriesTimerId") else "")})
    return out


@router.get("/api/live/recordings/upcoming")
def recordings_upcoming(request: Request, db: Session = Depends(get_db)):
    """What will record (and what rules in preview would), soonest first:
    the rules' bookings with why and on which channel, the ones waiting
    and why, and what people set in Jellyfin itself."""
    _caller(request, db)
    now = datetime.utcnow()
    rules = {r.id: r for r in db.query(RecordingRule).all()}
    rows = (db.query(RecordingBooking).filter(RecordingBooking.state.in_(_SHOWN))
            .filter(RecordingBooking.stop > now - timedelta(minutes=1)).order_by(RecordingBooking.start).all())
    ours = {b.jellyfin_timer_id for b in rows if b.jellyfin_timer_id}
    items = [_booking_json(b, rules) for b in rows]
    own = _jellyfin_own(db, ours)
    items.sort(key=lambda x: x["start"])
    return {"rules": items, "jellyfin": sorted(own, key=lambda x: x["start"])}


@router.get("/api/live/recordings/history")
def recordings_history(request: Request, days: int = Query(30, ge=1, le=400), db: Session = Depends(get_db)):
    """What the rules recorded (ended), skipped or couldn't, newest first."""
    _caller(request, db)
    since = datetime.utcnow() - timedelta(days=days)
    rules = {r.id: r for r in db.query(RecordingRule).all()}
    rows = (db.query(RecordingBooking).filter(RecordingBooking.state.in_(("ended", "skipped", "failed")))
            .filter(RecordingBooking.start >= since).order_by(RecordingBooking.start.desc()).limit(300).all())
    return {"items": [_booking_json(b, rules) for b in rows]}


@router.get("/api/live/recordings/marks")
def recordings_marks(request: Request, db: Session = Depends(get_db)):
    """For the guides (Jellyfin Web's Tentacle guide, the TV app): the
    programmes rules will record or, in preview, would; by Jellyfin channel
    id and UTC start. Small, read often."""
    _caller(request, db)
    now = datetime.utcnow()
    rules = {r.id: r for r in db.query(RecordingRule).all()}
    rows = (db.query(RecordingBooking).filter(RecordingBooking.state.in_(_SHOWN))
            .filter(RecordingBooking.stop > now).all())
    return {"marks": [{"channel_id": b.jellyfin_channel_id, "start": b.start.isoformat() + "Z",
                       "stop": b.stop.isoformat() + "Z", "state": b.state, "title": b.title,
                       "rule": rules[b.rule_id].name if b.rule_id in rules else None,
                       "program_id": b.jellyfin_program_id, "timer_id": b.jellyfin_timer_id}
                      for b in rows if b.jellyfin_channel_id]}


@router.post("/api/live/recordings/{booking_id}/skip")
def skip_booking(booking_id: int, request: Request, db: Session = Depends(get_db)):
    """Skip this showing: its recording is cancelled (if Tentacle made it and
    it hasn't started) and the rule never books it again."""
    from services import livetv_tuners as T
    user = _caller(request, db)
    if not can_record(db, user):
        raise HTTPException(403, "Your account can't change recordings")
    b = db.get(RecordingBooking, booking_id)
    if b is None:
        raise HTTPException(404, "No such recording")
    if b.state == "recording":
        raise HTTPException(409, "It's recording now: stop it in Jellyfin if you want it stopped")
    from services.recording_rules import book
    who = user.display_name if user else "an admin"
    if not book.cancel(db, T.jellyfin_for(db), b, "person", f"Skipped by {who}", state="skipped"):
        db.rollback()
        raise HTTPException(409, "It has started recording: stop it in Jellyfin if you want it stopped")
    db.commit()
    book.notify()
    return _booking_json(b, {r.id: r for r in db.query(RecordingRule).all()})


@router.post("/api/live/recordings/{booking_id}/restore")
def restore_booking(booking_id: int, request: Request, db: Session = Depends(get_db)):
    """Undo a skip: the next scan books it again (if the rule records)."""
    user = _caller(request, db)
    if not can_record(db, user):
        raise HTTPException(403, "Your account can't change recordings")
    b = db.get(RecordingBooking, booking_id)
    if b is None or b.state != "skipped":
        raise HTTPException(404, "No skipped recording with that id")
    db.delete(b)
    db.commit()
    _changed(db)
    return {"restored": booking_id}


class CancelledBody(BaseModel):
    timer_id: str


def _cancel_confirmed(db: Session, timer_id: str) -> bool:
    """The plugin has no secret shared with Tentacle: Tentacle asks Jellyfin
    (with its own key) whether that timer was really cancelled there."""
    import requests
    url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        return False
    try:
        r = requests.get(f"{url}/Tentacle/TimerCancelled/{timer_id}", headers={"X-Emby-Token": key}, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


@router.post("/api/live/recordings/cancelled")
def timer_cancelled(body: CancelledBody, request: Request, db: Session = Depends(get_db)):
    """From the plugin: someone cancelled a recording in Jellyfin. If a rule
    made it, that showing is skipped (never booked again): a person's no is
    kept, where a guide refresh deleting it would be booked again. Anyone may
    say so; it counts once Jellyfin confirms it (or an admin says it)."""
    from routers.auth import _has_internal_secret
    from services.recording_rules import book
    tid = (body.timer_id or "").replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", tid):
        raise HTTPException(422, "timer_id is a Jellyfin timer id")
    trusted = _has_internal_secret(request, db)
    if not trusted:
        try:
            user = _caller(request, db)
            trusted = user is None or bool(user.is_admin)
        except HTTPException:
            trusted = False
    if not trusted and not _cancel_confirmed(db, tid):
        raise HTTPException(403, "Jellyfin doesn't know that cancel")
    rows = [b for b in db.query(RecordingBooking).filter(RecordingBooking.jellyfin_timer_id.isnot(None)).all()
            if b.jellyfin_timer_id.replace("-", "") == tid]
    for b in rows:
        book.journal(db, b, "person", "skip", "cancelled in Jellyfin")
        b.state, b.reason, b.jellyfin_timer_id = "skipped", "Cancelled in Jellyfin", None
    db.commit()
    if rows:
        book.notify()
    return {"skipped": len(rows)}


# ── Pictures for recordings (services/recording_art) ────────────────────────

class ArtworkBody(BaseModel):
    enabled: bool


@router.get("/api/live/recordings/artwork", dependencies=[Depends(require_admin)])
def recording_artwork(db: Session = Depends(get_db)):
    """Whether Tentacle gives recordings pictures, how many it gave from
    where, and why it can't when that's so."""
    from services.recording_art import job
    return job.status(db)


@router.put("/api/live/recordings/artwork", dependencies=[Depends(require_admin)])
def recording_artwork_set(body: ArtworkBody, db: Session = Depends(get_db)):
    """On: a pass starts at once. Off: Tentacle's own pictures are removed
    (what people set stays)."""
    from services import live_updates
    from services.recording_art import job
    job.set_enabled(db, body.enabled)
    live_updates.publish("recordings", admins=True)
    return job.status(db)


# ── The space limit (services/recording_space, #868) ───────────────────────

class SpaceBody(BaseModel):
    limit_gb: Optional[float] = None      # empty: no size limit
    days: Optional[int] = None            # empty: no age limit


def _space_values(limit_gb, days) -> tuple:
    if limit_gb is not None and not (1 <= limit_gb <= 1_000_000):
        raise HTTPException(422, "The limit is 1 GB or more")
    if days is not None and not (1 <= days <= 36500):
        raise HTTPException(422, "Days is 1 or more")
    return limit_gb or None, days or None


@router.get("/api/live/recordings/space")
def recordings_space(request: Request, limit_gb: Optional[float] = Query(None), days: Optional[int] = Query(None),
                     preview: bool = Query(False), db: Session = Depends(get_db)):
    """How much the recordings take and the limit (as of the last read, every
    15 minutes). `preview`: what would go at `limit_gb`/`days` (admins; a dry
    run, nothing is deleted)."""
    from services import recording_space as RS
    user = _caller(request, db)
    admin = user is None or bool(user.is_admin)
    if preview:
        if not admin:
            raise HTTPException(403, "Only an admin sets the recordings limit")
        limit_gb, days = _space_values(limit_gb, days)
    out = RS.status(db, limit_gb=limit_gb, days=days, preview=preview)
    out["is_admin"] = admin
    return out


@router.put("/api/live/recordings/space", dependencies=[Depends(require_admin)])
def recordings_space_set(body: SpaceBody, db: Session = Depends(get_db)):
    """Set (or clear: both empty) the limit. A pass follows in seconds and
    deletes what the preview showed."""
    from services import recording_space as RS
    limit_gb, days = _space_values(body.limit_gb, body.days)
    RS.set_limits(db, limit_gb, days)
    logger.info(f"[Recordings limit] set: {f'{limit_gb:g} GB' if limit_gb else 'no size limit'}"
                f"{f', {days} days' if days else ''}")
    if limit_gb or days:
        RS.request_pass()
    from services.recording_rules import book
    book.notify()
    return RS.status(db)
