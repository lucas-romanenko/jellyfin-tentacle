"""Turns the plan into Jellyfin recordings, and keeps recording_bookings
true to what Jellyfin has.

Jellyfin stays the recorder: a rule that records gets ordinary one-off
timers, made exactly as Jellyfin's own Record button makes them
(GET /LiveTv/Timers/Defaults?programId=, then POST /LiveTv/Timers) with the
rule's padding. Only timers Tentacle made (their ids are in
recording_bookings) are ever cancelled, and only before they start. Timers
people set themselves count for connections and duplicates ("covered") and
are never touched. A rule in preview writes nothing to Jellyfin.

One scan at a time (_lock). It runs every SCAN_MINUTES, a few seconds after
a rule changes, and after the guide is refreshed.
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import (Provider, RecordingBooking, RecordingBookingEvent, RecordingRule,
                             RecordingRuleChannel, RecordingRuleTerm, SessionLocal, TentacleUser)
from services.recording_rules import guide as G
from services.recording_rules import plan as P

logger = logging.getLogger(__name__)

SCAN_MINUTES = 30
TOLERANCE = timedelta(seconds=120)      # the same start, written another way or a minute off
ACTIVE = ("booked", "recording", "covered")
_lock = threading.Lock()
_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()
_last: dict = {"at": None, "summary": None, "error": None}


# ── Rules from the database ─────────────────────────────────────────────────

def rule_of(row: RecordingRule, terms: list, channels: list) -> P.Rule:
    return P.Rule(
        id=row.id, name=row.name, kind=row.kind or "team", league=row.league, team=row.team,
        home_away=row.home_away or "both",
        terms=[t.term for t in terms if not t.exclude], excludes=[t.term for t in terms if t.exclude],
        match_all=bool(row.match_all), search_description=bool(row.search_description),
        min_minutes=row.min_minutes, pre_padding_minutes=int(row.pre_padding_minutes or 0),
        post_padding_minutes=int(row.post_padding_minutes or 0),
        playoff_extra_minutes=int(row.playoff_extra_minutes or 0),
        pregame_next=bool(row.pregame_next), skip_replays=bool(row.skip_replays),
        channels={c.pool_key for c in channels}, priority=int(row.priority or 0), mode=row.mode or "preview")


def load_rules(db: Session, only_active: bool = True) -> list:
    q = db.query(RecordingRule)
    if only_active:
        q = q.filter(RecordingRule.active == True)  # noqa: E712
    rows = q.order_by(RecordingRule.id).all()
    ids = [r.id for r in rows]
    terms, chans = {}, {}
    if ids:
        for t in db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id.in_(ids)).all():
            terms.setdefault(t.rule_id, []).append(t)
        for c in db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id.in_(ids)).all():
            chans.setdefault(c.rule_id, []).append(c)
    return [rule_of(r, terms.get(r.id, []), chans.get(r.id, [])) for r in rows]


# What a rule recorded or booked (a later showing of the same game is a
# rerun): booked, recording, ended and covered (someone's own timer records
# it); a rule in preview, what it would have recorded. Not skipped (someone
# said no), waiting, failed or lost: nothing records those.
RECORDED = ("booked", "recording", "ended", "covered")


def recorded(db: Session, rules: list, now: datetime) -> dict:
    """rule id → [plan.Recorded] of its team games since MATCHUP_WINDOW ago."""
    team = {r.id: r for r in rules if r.kind == "team"}
    if not team:
        return {}
    since = now - P.MATCHUP_WINDOW - P.TEAM_WINDOW
    out: dict = {}
    for b in db.query(RecordingBooking).filter(RecordingBooking.rule_id.in_(list(team)),
                                               RecordingBooking.start >= since).order_by(RecordingBooking.start):
        if b.state in RECORDED or (b.state == "preview" and team[b.rule_id].mode != "record"):
            out.setdefault(b.rule_id, []).append(P.Recorded(
                b.start, b.title or "", b.sub_title or "", b.description or "", b.channel_name or "",
                bool(b.from_event)))
    return out


# ── Jellyfin ────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse(value) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _same_start(a: Optional[datetime], b: Optional[datetime]) -> bool:
    return a is not None and b is not None and abs(a - b) <= TOLERANCE


def channel_ids(db: Session, entries: Optional[list] = None) -> tuple:
    """(pool key -> Jellyfin channel item id, Jellyfin id -> pool key) for the
    lineup tuner, old keys (aliases) included in the reverse map."""
    from services import livetv_lineups as L
    from services import livetv_tuners as T
    url = T.all_tuner_url(db)
    if not url:
        return {}, {}
    entries = L.lineup(db) if entries is None else entries
    back = L.item_ids_for(url, entries)
    try:
        back.update({k: v for k, v in L.alias_item_ids_for(url, L.final_aliases(db, entries)).items()
                     if k not in back})
    except Exception as e:
        logger.debug(f"[Recordings] aliases: {e}")
    fwd = {}
    for jid, key in back.items():
        fwd.setdefault(key, jid)
    for jid, key in L.item_ids_for(url, entries).items():
        fwd[key] = jid
    return fwd, back


def _timers(jf) -> list:
    return (jf.get("/LiveTv/Timers") or {}).get("Items") or []


def _held(timers: list, ours: set, key_of: dict, provider_of: dict, now: datetime) -> list:
    """Connection windows of recordings Tentacle didn't make."""
    out = []
    for t in timers:
        if t.get("Id") in ours or t.get("Status") not in ("New", "InProgress"):
            continue
        key = key_of.get((t.get("ChannelId") or "").replace("-", ""))
        start, stop = _parse(t.get("StartDate")), _parse(t.get("EndDate"))
        if not key or start is None or stop is None:
            continue
        s = start - timedelta(seconds=float(t.get("PrePaddingSeconds") or 0))
        e = stop + timedelta(seconds=float(t.get("PostPaddingSeconds") or 0))
        if e > now:
            out.append(P.Window(provider_of.get(key), key, s, e, t.get("Name") or ""))
    return out


# ── The scan ────────────────────────────────────────────────────────────────

def scan(db: Session, jf=None, now: Optional[datetime] = None, airings: Optional[list] = None) -> dict:
    """Plan every active rule and bring recording_bookings (and, for rules
    that record, Jellyfin) in line. Returns counts."""
    from services import livetv_tuners as T
    now = now or datetime.utcnow()
    airings_given = airings is not None
    with _lock:
        rules = load_rules(db)
        jf = jf if jf is not None else T.jellyfin_for(db)
        items = G.airings(db, now) if airings is None else airings
        fwd, back = channel_ids(db) if jf is not None else ({}, {})
        timers, timers_known = [], False
        if jf is not None:
            try:
                timers, timers_known = _timers(jf), True
            except requests.RequestException as e:
                logger.info(f"[Recordings] Jellyfin's recordings not read: {e}")
        provider_of = {a.pool_key: a.provider_id for a in items}
        ours = {b.jellyfin_timer_id for b in db.query(RecordingBooking.jellyfin_timer_id)
                .filter(RecordingBooking.jellyfin_timer_id.isnot(None)).all()}
        held = _held(timers, ours, back, provider_of, now)
        limits: dict = {}

        def limit_of(pid):
            if pid not in limits:
                from routers.livetv import provider_stream_limit
                limits[pid] = provider_stream_limit(db, db.get(Provider, pid) if pid else None)
            return limits[pid]

        from services.recording_rules import schedule as S
        games = S.for_rules(rules)
        done = recorded(db, rules, now)
        plans = [P.plan_rule(r, items, now, games.get((r.league, r.team)), done.get(r.id, ()))
                 for r in rules]
        fit_plans(plans, held, limit_of)
        summary = {"rules": len(rules), "listings": len(items), "booked": 0, "preview": 0, "waiting": 0,
                   "covered": 0, "cancelled": 0, "changed": False}
        if timers_known:
            _reconcile(db, timers, now, summary)
        # Rules gone, off or back in preview: their own upcoming timers are
        # cancelled first (history kept), then the plan is written.
        if jf is not None and timers_known:
            _cancel_orphans(db, jf, {r.id for r in rules if r.mode == "record"}, now, summary)
        for p in plans:
            try:
                _apply(db, p, jf, timers if timers_known else None, fwd, back, now, summary)
            except Exception as e:   # one rule's failure never starves the others
                db.rollback()
                logger.exception(f"[Recordings] rule {p.rule.id} ({p.rule.name}): {e}")
        db.commit()
        _last.update(at=now, summary=summary, error=None)
        if not airings_given:
            _look_again_at(next_change(db, now), now)
        if summary["changed"]:
            notify()
        return summary


def fit_plans(plans: list, held: list, limit_of) -> None:
    """Connections: rules that record first (together, by priority); then
    each rule in preview on its own against what is really booked, so
    previews show what would happen if switched on without blocking each
    other or a rule that records."""
    taken = P.fit([p for p in plans if p.rule.mode == "record"], held, limit_of)
    for p in plans:
        if p.rule.mode != "record":
            P.fit([p], taken, limit_of)


def _covering(timers: Optional[list], jid: Optional[str], start: datetime, ours: set) -> Optional[dict]:
    """A timer someone else set on this channel at this start."""
    for t in timers or []:
        if t.get("Id") in ours or t.get("Status") not in ("New", "InProgress"):
            continue
        if (t.get("ChannelId") or "").replace("-", "") == jid and _same_start(_parse(t.get("StartDate")), start):
            return t
    return None


def _apply(db: Session, p: P.RulePlan, jf, timers, fwd: dict, back: dict, now: datetime, summary: dict) -> None:
    rule = p.rule
    existing = {(b.pool_key, b.start): b for b in
                db.query(RecordingBooking).filter(RecordingBooking.rule_id == rule.id).all()}
    ours = {b.jellyfin_timer_id for b in existing.values() if b.jellyfin_timer_id}
    keep = set()
    for ev in p.events:
        if ev.state == "replay":
            continue
        # A booking this rule already has for the same game stays the one: the
        # same listing, or another listing of it (a team plays one game at a
        # time; words: the same programme at about the same time). Seen live:
        # once the booked channel's listing had ended (the recording still in
        # its padding), the game's event-channel slot was booked as well.
        held = _held_booking(rule, ev, existing)
        if held is not None:
            keep.add((held.pool_key, held.start))
            continue
        opt = ev.options[ev.chosen] if ev.chosen is not None else ev.best
        a = opt.airing
        if a.from_event and _stale_slot(a, existing):
            continue
        key = (a.pool_key, a.start)
        keep.add(key)
        row = existing.get(key)
        if row is None:
            row = RecordingBooking(rule_id=rule.id, pool_key=a.pool_key, start=a.start, stop=a.stop,
                                   created_at=now, attempts=0)
            db.add(row)
            existing[key] = row
        pre, post = P.padding(rule, a)
        before = (row.state, row.reason, row.stop, row.title, row.channel_name)
        row.stop, row.channel_name, row.title = a.stop, a.channel, a.title
        row.sub_title, row.description, row.from_event = a.sub_title or None, a.description or None, a.from_event
        row.why = (opt.why + (f" ({ev.reason})" if ev.reason and ev.state == "record" else ""))[:250]
        row.pre_padding_seconds, row.post_padding_seconds = pre, post
        row.jellyfin_channel_id = fwd.get(a.pool_key)
        if rule.mode != "record":
            row.state = "preview"
            row.reason = ev.reason if ev.state == "waiting" else "Preview: would record this"
            summary["preview"] += 1
        elif ev.state == "waiting":
            row.state, row.reason = "waiting", ev.reason
            summary["waiting"] += 1
        elif a.from_event and a.start - now > EVENT_SLOT_AHEAD:
            row.state = "waiting"
            row.reason = (f"An event channel's slot names only a time: booked from {EVENT_SLOT_AHEAD_HOURS} "
                          f"hours before it, when its date is sure")
            summary["waiting"] += 1
        else:
            cover = _covering(timers, row.jellyfin_channel_id, a.start, ours)
            if cover is not None:
                row.state, row.reason = "covered", "Already set to record in Jellyfin"
                summary["covered"] += 1
            elif jf is not None and timers is not None and (row.next_attempt_at is None or row.next_attempt_at <= now):
                _book(db, jf, row, rule, now, summary)
        row.updated_at = now
        if before != (row.state, row.reason, row.stop, row.title, row.channel_name):
            summary["changed"] = True
    # A booked listing the team's schedule now says is a rerun (booked before
    # the schedule was known): its timer is cancelled and the row goes.
    for n in p.reruns:
        key = (n.airing.pool_key, n.airing.start)
        row = existing.get(key)
        if row is not None and key not in keep and row.state == "booked" and jf is not None:
            if cancel(db, jf, row, "scan", n.reason, now):
                existing.pop(key, None)
                summary["cancelled"] += 1
                summary["changed"] = True
    # What the plan no longer wants: a preview or waiting row goes; a booked
    # one stays (a guide that lost the listing must never cancel a recording:
    # Jellyfin itself drops a one-off whose programme left its guide).
    for key, row in list(existing.items()):
        if key in keep:
            continue
        if row.state in ("preview", "waiting", "failed", "lost") and row.stop > now - timedelta(hours=1):
            db.delete(row)
            summary["changed"] = True
    db.flush()


# Event and pay-per-view channels name a time without a date ("NHL | 01 -
# 8pm Canadiens at Blackhawks"); the guide puts it at the next such time. A
# slot still named after a game that has been played moves to the next day,
# and a Sunday game named on Friday lands on Saturday. So a slot is booked
# only this close to its start, and never when it repeats one this rule had
# a day earlier (seen 24 Oct: tomorrow 8pm "Canadiens at Blackhawks").
EVENT_SLOT_AHEAD_HOURS = 6
EVENT_SLOT_AHEAD = timedelta(hours=EVENT_SLOT_AHEAD_HOURS)


def _stale_slot(a, existing: dict) -> bool:
    """The same event channel, the same name, about a day after a booking of
    it: the slot's name outlived its event."""
    day = timedelta(hours=24)
    for b in existing.values():
        if b.pool_key == a.pool_key and (b.title or "") == (a.title or "") \
                and abs((a.start - b.start) - day) <= timedelta(hours=2) and b.state != "waiting":
            return True
    return False


def _held_booking(rule: P.Rule, ev: P.Event, existing: dict) -> Optional[RecordingBooking]:
    from services.recording_rules import match as M
    for o in ev.options:
        b = existing.get((o.airing.pool_key, o.airing.start))
        if b is not None and b.state in ACTIVE + ("skipped", "ended"):
            return b
    same_game = rule.kind in ("team", "series")
    window = P.TEAM_WINDOW if same_game else P.GAME_WINDOW
    idents = {M.identity(o.airing.title, o.airing.description) for o in ev.options}
    for b in existing.values():
        if b.state not in ACTIVE + ("skipped", "ended"):
            continue
        if any(abs(b.start - o.airing.start) <= window for o in ev.options) and (
                same_game or M.identity(b.title, b.description) in idents):
            return b
    return None


def journal(db: Session, row: Optional[RecordingBooking], actor: str, action: str, detail: str = "",
            status: Optional[int] = None) -> RecordingBookingEvent:
    ev = RecordingBookingEvent(booking_id=row.id if row is not None else None, at=datetime.utcnow(),
                               actor=actor, action=action, detail=(detail or "")[:500], http_status=status)
    db.add(ev)
    db.commit()     # before the write it describes
    return ev


def _book(db: Session, jf, row: RecordingBooking, rule: P.Rule, now: datetime, summary: dict) -> None:
    """One Jellyfin timer for this airing, made like the Record button makes it."""
    jid = row.jellyfin_channel_id
    if not jid:
        row.state, row.reason = "waiting", "This channel isn't in Jellyfin's guide yet"
        return
    try:
        progs = (jf.get("/LiveTv/Programs", channelIds=jid, minStartDate=_iso(row.start - TOLERANCE),
                        maxStartDate=_iso(row.start + TOLERANCE), limit=5) or {}).get("Items") or []
    except requests.RequestException as e:
        row.state, row.reason = "waiting", f"Jellyfin's guide couldn't be read ({_status(e) or 'no answer'})"
        return
    prog = next((x for x in progs if _same_start(_parse(x.get("StartDate")), row.start)), None)
    if prog is None:
        row.state, row.reason = "waiting", "Not in Jellyfin's guide yet: booked after its next guide refresh"
        return
    try:
        timer = jf.get("/LiveTv/Timers/Defaults", programId=prog["Id"]) or {}
    except requests.RequestException as e:
        if _status(e) != 500:
            row.state, row.reason = "waiting", f"Jellyfin didn't answer ({_status(e) or 'no answer'})"
            return
        # 10.11 answers 500 for a programme that is already on: the timer by hand
        timer = {"ChannelId": prog.get("ChannelId") or jid, "ProgramId": prog["Id"], "Name": prog.get("Name"),
                 "Overview": prog.get("Overview"), "StartDate": prog.get("StartDate"), "EndDate": prog.get("EndDate"),
                 "ServiceName": "Emby", "KeepUntil": "UntilDeleted"}
    timer.update({"PrePaddingSeconds": row.pre_padding_seconds or 0, "PostPaddingSeconds": row.post_padding_seconds or 0,
                  "IsPrePaddingRequired": True, "IsPostPaddingRequired": True, "Priority": rule.priority})
    if row.id is None:
        db.flush()
    journal(db, row, "scan", "book", f"{row.title} on {row.channel_name} at {_iso(row.start)}")
    row.attempts = (row.attempts or 0) + 1
    try:
        jf.post("/LiveTv/Timers", timer)
    except requests.RequestException as e:
        code = _status(e)
        journal(db, row, "jellyfin", "refused", str(e)[:200], code)
        if code == 400:
            # 10.11: a timer for this programme already exists (someone's own)
            row.state, row.reason = "covered", "Already set to record in Jellyfin"
            return
        row.state = "failed"
        row.reason = f"Jellyfin refused it ({code or 'no answer'}); trying again later"
        row.next_attempt_at = now + timedelta(minutes=min(240, 10 * 2 ** min(row.attempts, 5)))
        return
    # POST returns no body: the new timer's id is looked up.
    tid = None
    try:
        for t in _timers(jf):
            if t.get("ProgramId") == prog["Id"] or (
                    (t.get("ChannelId") or "").replace("-", "") == jid and _same_start(_parse(t.get("StartDate")), row.start)):
                tid = t.get("Id")
                break
    except requests.RequestException:
        pass
    row.jellyfin_timer_id, row.jellyfin_program_id = tid, prog["Id"]
    row.state, row.reason, row.next_attempt_at = "booked", "Set to record in Jellyfin", None
    summary["booked"] += 1
    summary["changed"] = True
    journal(db, row, "jellyfin", "booked", f"timer {tid}", 204)


def _status(e) -> Optional[int]:
    r = getattr(e, "response", None)
    return getattr(r, "status_code", None)


def _reconcile(db: Session, timers: list, now: datetime, summary: dict) -> None:
    """Each booking with a Jellyfin timer: recording now, still set, ended,
    or gone before its start (lost: booked again by the scan)."""
    by_id = {t.get("Id"): t for t in timers}
    rows = db.query(RecordingBooking).filter(RecordingBooking.state.in_(("booked", "recording", "covered"))).all()
    for row in rows:
        pre = timedelta(seconds=row.pre_padding_seconds or 0)
        post = timedelta(seconds=row.post_padding_seconds or 0)
        before = row.state
        if row.state == "covered":
            if row.stop + post <= now:
                row.state, row.reason = "ended", "Recorded by Jellyfin (set there)"
            elif _covering(timers, row.jellyfin_channel_id, row.start, set()) is None:
                row.state, row.reason = "waiting", "The recording set in Jellyfin was cancelled"
            continue
        t = by_id.get(row.jellyfin_timer_id) if row.jellyfin_timer_id else None
        if t is None and row.jellyfin_channel_id:
            # Moved by a lineup change, or re-made by Jellyfin: same channel, same start.
            t = next((x for x in timers if (x.get("ChannelId") or "").replace("-", "") == row.jellyfin_channel_id
                      and _same_start(_parse(x.get("StartDate")), row.start)
                      and x.get("Id") not in {r.jellyfin_timer_id for r in rows if r is not row}), None)
            if t is not None and row.jellyfin_timer_id != t.get("Id"):
                journal(db, row, "scan", "adopt", f"timer {row.jellyfin_timer_id} is now {t.get('Id')}")
                row.jellyfin_timer_id = t.get("Id")
        if t is not None:
            if t.get("Status") == "InProgress":
                row.state, row.reason = "recording", "Recording now"
            elif t.get("Status") == "New":
                row.state = "booked"
                if row.reason != "Set to record in Jellyfin":
                    row.reason = "Set to record in Jellyfin"
            continue
        if row.stop + post <= now:
            row.state, row.reason = "ended", "Recorded"
        elif row.start - pre > now:
            journal(db, row, "scan", "lost", f"timer {row.jellyfin_timer_id} is gone before its start")
            row.state, row.reason, row.jellyfin_timer_id = "lost", "Its Jellyfin recording went away; booking it again", None
        else:
            row.state, row.reason = "ended", "Stopped before its end in Jellyfin"
        if row.state != before:
            row.updated_at = now
            summary["changed"] = True
    # "lost" rows are booked again by _apply (they're like new).
    for row in db.query(RecordingBooking).filter(RecordingBooking.state == "lost").all():
        row.state = "waiting"


def _cancel_orphans(db: Session, jf, recording_rule_ids: set, now: datetime, summary: dict) -> None:
    """Upcoming timers of rules that were deleted, paused or put back in
    preview: cancelled by id (never one that has started)."""
    rows = db.query(RecordingBooking).filter(RecordingBooking.state == "booked").all()
    for row in rows:
        if row.rule_id in recording_rule_ids:
            continue
        cancel(db, jf, row, "scan", "the rule no longer records", now)
        summary["cancelled"] += 1
        summary["changed"] = True


def cancel(db: Session, jf, row: RecordingBooking, actor: str, why: str, now: Optional[datetime] = None,
           state: Optional[str] = None) -> bool:
    """Cancel this booking's own Jellyfin timer, if it hasn't started.
    Returns False when it is recording (left alone)."""
    now = now or datetime.utcnow()
    started = row.state == "recording" or row.start - timedelta(seconds=row.pre_padding_seconds or 0) <= now
    if row.jellyfin_timer_id and not started and jf is not None:
        journal(db, row, actor, "cancel", why)
        try:
            jf.delete(f"/LiveTv/Timers/{row.jellyfin_timer_id}")
        except requests.RequestException as e:
            if _status(e) != 404:
                journal(db, row, "jellyfin", "refused", str(e)[:200], _status(e))
                return False
        row.jellyfin_timer_id = None
    elif started and row.state in ("booked", "recording"):
        return False
    if state:
        row.state, row.reason = state, why
    elif row.rule_id is None or row.state == "booked":
        db.delete(row)
    return True


# ── Running it ──────────────────────────────────────────────────────────────

def run_scan() -> Optional[dict]:
    """The scheduled scan (own session). Does nothing without rules."""
    db = SessionLocal()
    try:
        if db.query(RecordingRule).filter(RecordingRule.active == True).count() == 0 and \
                db.query(RecordingBooking).filter(RecordingBooking.state.in_(("booked", "preview", "waiting"))).count() == 0:  # noqa: E712
            return None
        return scan(db)
    except Exception as e:
        logger.exception(f"[Recordings] scan failed: {e}")
        _last.update(at=datetime.utcnow(), error=str(e)[:300])
        return None
    finally:
        db.close()


def next_change(db: Session, now: datetime) -> Optional[datetime]:
    """When a booking next starts recording or ends (padding included):
    Upcoming should say so then, not at the next half-hourly scan."""
    times = []
    for b in db.query(RecordingBooking).filter(RecordingBooking.state.in_(("booked", "recording"))).all():
        start = b.start - timedelta(seconds=b.pre_padding_seconds or 0)
        end = b.stop + timedelta(seconds=b.post_padding_seconds or 0)
        times += [t for t in (start, end) if t > now]
    return min(times) if times else None


def _look_again_at(when: Optional[datetime], now: datetime) -> None:
    """A scan a minute after `when`, if that is before the next regular one
    (Jellyfin marks a timer InProgress once it has opened the stream)."""
    if when is None:
        return
    delay = (when - now).total_seconds() + 60
    if 0 < delay < SCAN_MINUTES * 60:
        request_scan(delay=delay)


def request_scan(delay: float = 3.0) -> None:
    """A scan in a few seconds (rules changed, the guide was refreshed);
    several requests in a row make one scan."""
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, run_scan)
        _timer.daemon = True
        _timer.start()


def last_scan() -> dict:
    return {"at": _last["at"].isoformat() + "Z" if _last["at"] else None,
            "summary": _last["summary"], "error": _last["error"]}


def notify() -> None:
    """Every open screen (dashboards, Jellyfin Web's Tentacle pages, the TV
    app) re-reads its recordings: they are the household's."""
    from services import live_updates
    db = SessionLocal()
    try:
        ids = [u.id for u in db.query(TentacleUser.id).all()]
    finally:
        db.close()
    live_updates.changed("recordings", everyone=True, user_ids=ids, clients=True)
