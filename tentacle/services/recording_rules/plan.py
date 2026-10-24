"""What each rule would record, and why not the rest. Pure: listings, rules
and the recordings already set in, decisions out; nothing is written.

One game, one recording. A team plays one game at a time, so a team rule's
listings that start within TEAM_WINDOW of each other are one game, whatever
each channel calls it (an event channel's "Penguins@Maple Leafs" and
Sportsnet's "NHL Hockey"); the best of them is recorded: a regular channel
before an event channel's slot, a "Live:" listing before one without. A
later listing of the same programme is a replay: skipped, unless the first
showing can't be recorded (no connection free), then it's the later showing.
A keyword rule groups listings by the programme itself (title and
description) the same way.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from services.recording_rules import leagues as LG
from services.recording_rules import match as M

GAME_WINDOW = timedelta(minutes=45)       # the same programme on another channel starts this close
TEAM_WINDOW = timedelta(minutes=90)       # a team plays one game at a time (event slots' times are rough)
REPLAY_AFTER = timedelta(minutes=60)      # the same programme this much later is a replay
PREGAME_GAP = timedelta(minutes=15)       # the game follows its pregame show within this
_OTHER_SERIES = ("F1 Academy", "Formula E", "Formula 2", "Formula 3", "F2", "F3", "Moto2", "Moto3", "MotoE")


@dataclass
class Rule:
    id: int
    name: str
    kind: str = "team"                  # team | keywords | series
    league: Optional[str] = None
    team: Optional[str] = None
    home_away: str = "both"
    terms: list = field(default_factory=list)
    excludes: list = field(default_factory=list)
    match_all: bool = False
    search_description: bool = False
    min_minutes: Optional[int] = None
    pre_padding_minutes: int = 5
    post_padding_minutes: int = 60
    playoff_extra_minutes: int = 0
    pregame_next: bool = True
    skip_replays: bool = True
    channels: set = field(default_factory=set)    # pool keys; empty = all
    priority: int = 0
    mode: str = "preview"


@dataclass
class Option:
    airing: object
    why: str
    home: Optional[bool] = None
    note: str = ""                       # why it isn't the first choice ("same game as ...")


@dataclass
class Event:
    """One game (or programme) a rule wants: the airings that would do, best first."""
    rule: Rule
    options: list
    replay_of: Optional[Option] = None   # set: this is a later showing of an earlier event
    chosen: Optional[int] = 0            # index into options, None = nothing can be recorded
    state: str = "record"                # record | replay | waiting
    reason: str = ""

    @property
    def best(self) -> Option:
        return self.options[self.chosen or 0]


@dataclass
class NearMiss:
    airing: object
    reason: str


@dataclass
class RulePlan:
    rule: Rule
    events: list
    near: list


def min_minutes(rule: Rule) -> int:
    if rule.min_minutes is not None:
        return rule.min_minutes
    lg = LG.league(rule.league)
    return int((lg or {}).get("min_minutes") or (0 if rule.kind == "keywords" else 90))


def padding(rule: Rule, airing) -> tuple:
    """(pre, post) seconds; a playoff game gets the rule's extra time."""
    post = rule.post_padding_minutes
    if rule.playoff_extra_minutes and M.is_playoff(_listing(airing), LG.league(rule.league)):
        post += rule.playoff_extra_minutes
    return rule.pre_padding_minutes * 60, post * 60


def _listing(a) -> M.Listing:
    return M.Listing(a.title, a.sub_title, a.description, a.category, a.channel_raw)


def _hit(rule: Rule, a):
    """(matched, why, home, near, opponents)."""
    lst = _listing(a)
    if rule.kind == "team":
        lg, tm = LG.league(rule.league), LG.team(rule.league, rule.team)
        if not lg or not tm:
            return False, "", None, "", []
        h = M.match_team(lst, lg, tm, LG.foreign_names(rule.league, rule.team))
        bad = M.any_of(lst.text(), rule.excludes) if h.matched else None
        if bad:
            # the rule's "Leave out" words ("Women's", "U-17", "Highlights")
            return False, "", None, f"{h.why}, but it says “{bad}”", []
        if h.matched and a.from_event and not h.game:
            # an event channel's slot is three hours whatever it is: "The
            # Rebuild: Inside the Montreal Canadiens" isn't a game
            return False, "", None, f"{h.why} on an event channel, but no {lg.get('name')} game in it", []
        return h.matched, h.why, h.home, h.near, h.teams[1:]
    if rule.kind == "series":
        lg = LG.league(rule.league) or {}
        others = [w for o in LG.leagues() if o.get("kind") == "series" and o["id"] != rule.league
                  for w in o.get("terms") or [] if w not in (lg.get("terms") or [])]
        h = M.match_keywords(lst, lg.get("terms") or [], list(_OTHER_SERIES) + others + rule.excludes,
                             False, False)
        return h.matched, h.why, None, h.near, []
    h = M.match_keywords(lst, rule.terms, rule.excludes, rule.match_all, rule.search_description)
    return h.matched, h.why, None, h.near, []


def _follows_pregame(rule: Rule, pre, nxt, shortest: int) -> bool:
    """The listing after a matching pregame show is the game: right after it,
    long enough, and (team rules) about the league."""
    if nxt is None or nxt.minutes < shortest:
        return False
    if not (timedelta(0) <= nxt.start - pre.stop <= PREGAME_GAP):
        return False
    if rule.kind != "team":
        return True
    lg = LG.league(rule.league) or {}
    around = _listing(nxt).text() + M.fold(nxt.category) + M.fold(nxt.channel_raw)
    return M.league_context(around, lg) is not None


def _home_of(rule: Rule, a) -> Optional[bool]:
    """Home or away for a listing that doesn't name the team (the game after
    its pregame): another team's arena in it means away."""
    lg, tm = LG.league(rule.league), LG.team(rule.league, rule.team)
    if not lg or not tm:
        return None
    text = _listing(a).text()
    if M.any_of(text, tm.get("venue") or []):
        return True
    if any(M.any_of(text, t.get("venue") or []) for t in lg.get("teams") or [] if t["id"] != tm["id"]):
        return False
    return None


def _rank(o: Option):
    a = o.airing
    return (a.from_event, not M.is_live_title(a.title), a.start, a.number)


def plan_rule(rule: Rule, airings: list, now: datetime) -> RulePlan:
    shortest = min_minutes(rule)
    found: dict = {}
    games = []      # (airing, opponents named, home) of team listings that name a game
    near = []
    for a in airings:
        if a.stop <= now or (rule.channels and a.pool_key not in rule.channels):
            continue
        ok, why, home, miss, opponents = _hit(rule, a)
        if not ok:
            if miss and a.minutes >= shortest:
                near.append(NearMiss(a, miss))
            continue
        if a.minutes < shortest:
            nxt = a.next
            if rule.pregame_next and M.is_pregame(a.title) and _follows_pregame(rule, a, nxt, shortest):
                key = (nxt.pool_key, nxt.start)
                if key not in found:
                    found[key] = Option(nxt, f"follows “{M.strip_prefix(a.title)}”", _home_of(rule, nxt))
            else:
                near.append(NearMiss(a, f"{why}, but only {int(round(a.minutes))} min: too short to be "
                                        + ("the game" if rule.kind != "keywords" else "it")))
            continue
        found[(a.pool_key, a.start)] = Option(a, why, home)
        if opponents:
            games.append((a, opponents, home))

    # The same game on channels that don't name the team: an event channel
    # says "Canadiens at Blackhawks", Sportsnet lists "NHL Hockey: From the
    # United Center in Chicago." at the same time. That is the game too
    # (and usually the better recording: a regular channel).
    if rule.kind == "team" and games:
        lg = LG.league(rule.league) or {}
        for a in airings:
            if (a.pool_key, a.start) in found or a.stop <= now or a.minutes < shortest:
                continue
            if rule.channels and a.pool_key not in rule.channels:
                continue
            text = _listing(a).text()
            if M.league_context(text + M.fold(a.category) + M.fold(a.channel_raw), lg) is None:
                continue
            host = M.venue_team(text, lg)
            for g, opponents, home in games:
                if abs(a.start - g.start) > TEAM_WINDOW:
                    continue
                if host is not None and host["id"] in [rule.team, *opponents]:
                    name = host.get("short") or host["name"]
                    found[(a.pool_key, a.start)] = Option(
                        a, f"same time as “{M.strip_prefix(g.title)}”, from the {name}' arena",
                        host["id"] == rule.team)
                    break

    options = []
    for o in sorted(found.values(), key=lambda o: (o.airing.start, o.airing.number)):
        if rule.kind == "team" and rule.home_away in ("home", "away") and o.home is not None:
            if o.home != (rule.home_away == "home"):
                near.append(NearMiss(o.airing, "an away game" if rule.home_away == "home" else "a home game"))
                continue
        options.append(o)

    # Group: one game at a time for a team; one programme for words.
    groups: list = []
    for o in options:
        a = o.airing
        ident = M.identity(a.title, a.description)
        g = None
        for cand in groups:
            first = cand[0].airing
            window = TEAM_WINDOW if rule.kind in ("team", "series") else GAME_WINDOW
            if abs(a.start - first.start) <= window and (
                    rule.kind in ("team", "series") or any(M.identity(x.airing.title, x.airing.description) == ident
                                               for x in cand)):
                g = cand
                break
        if g is None:
            groups.append([o])
        else:
            g.append(o)

    events = []
    for g in groups:
        g.sort(key=_rank)
        best = g[0]
        for other in g[1:]:
            other.note = f"same {'game' if rule.kind != 'keywords' else 'programme'} as {best.airing.channel}"
        ev = Event(rule, g)
        # A replay: the same programme as an earlier event's, starting later.
        for prev in events:
            if prev.replay_of is not None:
                continue
            p_ids = {M.identity(x.airing.title, x.airing.description) for x in prev.options}
            if any(M.identity(x.airing.title, x.airing.description) in p_ids for x in g) \
                    and best.airing.start - prev.best.airing.start >= REPLAY_AFTER \
                    and not (M.is_live_title(best.airing.title) and not any(M.is_live_title(x.airing.title)
                                                                            for x in prev.options)):
                ev.replay_of = prev.best
                break
        if ev.replay_of is not None:
            a = ev.replay_of.airing
            ev.state = "replay" if rule.skip_replays else "record"
            ev.reason = f"a replay of {a.start:%H:%M} UTC on {a.channel}" if rule.skip_replays else ""
        events.append(ev)
    return RulePlan(rule, events, near[:40])


# ── Connections ─────────────────────────────────────────────────────────────

@dataclass
class Window:
    """A recording that holds a provider connection: padded start to padded end."""
    provider_id: Optional[int]
    pool_key: str
    start: datetime
    stop: datetime
    title: str


def _in_use(windows: list, provider_id, start: datetime, stop: datetime, pool_key: str) -> list:
    """Other channels this provider is recording during [start, stop) (two
    recordings of one channel share one stream)."""
    seen, out = set(), []
    for w in windows:
        if w.provider_id != provider_id or w.pool_key == pool_key or w.pool_key in seen:
            continue
        if w.start < stop and start < w.stop:
            seen.add(w.pool_key)
            out.append(w)
    return out


def fit(plans: list, held: list, limit_of) -> list:
    """Choose, for each event, the first airing a connection is free for:
    rules by priority, then earliest first. `held`: the windows recordings
    made elsewhere take (Jellyfin's Record button, series). `limit_of(provider
    id)`: Streams at once (0 = no limit). A first showing that can't get a
    connection is replaced by its replay; failing that the event waits, with
    the reason. Sets event.chosen / state / reason in place; returns every
    window taken (held ones included)."""
    windows = list(held)
    events = [e for p in plans for e in p.events]
    events.sort(key=lambda e: (-e.rule.priority, e.best.airing.start))
    replays: dict = {}
    for e in events:
        if e.replay_of is not None:
            replays.setdefault(id(e.replay_of), []).append(e)
    decided = set()
    for e in events:
        if e.state == "replay" or id(e) in decided:
            continue   # a skipped replay, or decided with its first showing below
        choice = _first_fitting(e, windows, limit_of)
        if choice is not None:
            e.chosen = choice
            windows.append(_window(e, e.options[choice]))
            if e.state == "waiting":
                e.state = "record"
            continue
        busy = _in_use(windows, e.best.airing.provider_id, *_padded(e, e.best), e.best.airing.pool_key)
        e.chosen, e.state = None, "waiting"
        e.reason = _busy_reason(limit_of(e.best.airing.provider_id), busy)
        # its later showing, when there is one
        for r in replays.get(id(e.best), ()):
            if r.rule is not e.rule:
                continue
            c = _first_fitting(r, windows, limit_of)
            if c is not None:
                r.chosen, r.state = c, "record"
                r.reason = "the first showing can't get a connection"
                windows.append(_window(r, r.options[c]))
                decided.add(id(r))
                break
    return windows


def _padded(e: Event, o: Option) -> tuple:
    pre, post = padding(e.rule, o.airing)
    return o.airing.start - timedelta(seconds=pre), o.airing.stop + timedelta(seconds=post)


def _window(e: Event, o: Option) -> Window:
    s, t = _padded(e, o)
    return Window(o.airing.provider_id, o.airing.pool_key, s, t, o.airing.title)


def _first_fitting(e: Event, windows: list, limit_of) -> Optional[int]:
    for i, o in enumerate(e.options):
        limit = limit_of(o.airing.provider_id)
        if not limit or o.airing.provider_id is None:
            return i
        s, t = _padded(e, o)
        if len(_in_use(windows, o.airing.provider_id, s, t, o.airing.pool_key)) < limit:
            return i
    return None


def _busy_reason(limit: int, busy: list) -> str:
    names = ", ".join(f"{M.strip_prefix(w.title)} {w.start:%H:%M}–{w.stop:%H:%M} UTC" for w in busy[:3])
    conn = "connection" if limit == 1 else f"{limit} connections"
    return f"Waiting: the provider's {conn} {'is' if limit == 1 else 'are'} in use then ({names})"
