"""Each followed team's real games, so a team rule records only a game that
is really played then, never a rerun of one (seen 26 Oct: an event
channel's "Canadiens at Blackhawks" slot, still named after a game played
three days earlier, was booked for that night; the Canadiens played the
Blues the next day).

The source is ESPN's public team schedule (site.api.espn.com, no key, the
same site the team logos come from): the season's games of one team with
start times and opponents. NHL, NBA, WNBA, NFL and MLB (a team's every
game: its season and playoffs), MLS and the big five soccer leagues (only
that league's games: a cup tie isn't in it). The CFL, EuroLeague and
national teams have none: their rules work as before.

What a listing is, against the schedule (`verdict`):
  - a game of the team within GAME_NEAR of it, against an opponent it
    names (or any, when it names none): the game.
  - it names an opponent the team played earlier, and no game against them
    is near: a rerun ("Canadiens played the Blackhawks on Fri 23 Oct").
  - it names another opponent than the one the team plays then: a rerun.
  - no game near at all, in a league whose schedule is every game, inside
    the schedule's dates: not a game of theirs ("no Canadiens game then").
  - anything else (nothing known, a cup tie, a date past the schedule's
    end such as playoffs not yet set): left to the listing, as before.

Fetched once per team every SCHEDULE_MAX_AGE (kept in DATA_DIR/team_logos
beside the team lists). When ESPN can't be reached the last copy is used;
with none, the rule plans as it did without schedules.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.recording_rules import leagues as LG

logger = logging.getLogger(__name__)

ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams?limit=500"
ESPN_SCHEDULE = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{id}/schedule{query}"

# league id → ESPN's sport/league path, and whether its schedule is every game the team plays
SOURCES = {
    "nhl": ("hockey/nhl", True), "nba": ("basketball/nba", True), "wnba": ("basketball/wnba", True),
    "nfl": ("football/nfl", True), "mlb": ("baseball/mlb", True),
    "mls": ("soccer/usa.1", False), "epl": ("soccer/eng.1", False), "laliga": ("soccer/esp.1", False),
    "seriea": ("soccer/ita.1", False), "bundesliga": ("soccer/ger.1", False), "ligue1": ("soccer/fra.1", False),
}
# what each fetch asks for: the season so far and to come; playoffs (US leagues); soccer's fixtures
QUERIES = {True: ("", "?seasontype=3"), False: ("", "?fixture=true")}

GAME_NEAR = timedelta(hours=4)          # a listing this close to a game's start is that game
PLAYED_BACK = timedelta(days=21)        # soccer: a listing of a game this recent with no fixture is a rerun
SCHEDULE_MAX_AGE = 6 * 3600
FAILED_RETRY = 15 * 60                  # ESPN unreachable: asked again this much later

_lock = threading.Lock()
_mem: dict = {}                         # (league, team) → (fetched at, Schedule or None)


@dataclass
class Game:
    start: datetime                     # UTC, naive
    opponent: Optional[str]             # leagues.json team id, None when not in the list
    opponent_name: str
    home: Optional[bool] = None


@dataclass
class Schedule:
    league: str
    team: str
    games: list                         # Game, by start
    complete: bool                      # every game the team plays (not one competition's)


def supported(league_id: Optional[str]) -> bool:
    return league_id in SOURCES


# ── Verdict (pure) ──────────────────────────────────────────────────────────

def _short(league_id: str, team_id: Optional[str], fallback: str = "") -> str:
    t = LG.team(league_id, team_id) or {}
    return t.get("short") or t.get("name") or fallback


def _day(dt: datetime) -> str:
    """The game's date where the server is (an evening game in North America
    is the next day in UTC)."""
    local = dt.replace(tzinfo=timezone.utc).astimezone()
    return f"{local:%a} {local.day} {local:%b}"


def verdict(s: Optional[Schedule], start: datetime, opponents: list) -> Optional[str]:
    """None: a game of the team then (or nothing known against it); else why
    the listing at `start` naming `opponents` (leagues.json ids) is no game
    of theirs then."""
    if s is None or not s.games:
        return None
    me = _short(s.league, s.team)
    near = [g for g in s.games if abs(g.start - start) <= GAME_NEAR]
    if near and (not opponents or any(g.opponent in opponents for g in near)):
        return None
    if opponents:
        played = [g for g in s.games if g.opponent in opponents and g.start < start - GAME_NEAR
                  and (s.complete or start - g.start <= PLAYED_BACK)]
        if played:
            g = played[-1]
            return f"rerun: the {me} played the {_short(s.league, g.opponent, g.opponent_name)} on {_day(g.start)}"
        if near:
            g = near[0]
            return (f"rerun: the {me} play the {_short(s.league, g.opponent, g.opponent_name)} then, "
                    f"not the {_short(s.league, opponents[0])}")
    if near or not s.complete:
        return None
    if not (s.games[0].start - GAME_NEAR <= start <= s.games[-1].start + GAME_NEAR):
        return None     # before the season, or after it (playoffs not set yet)
    nxt = next((g for g in s.games if g.start > start), None)
    after = f" (next: the {_short(s.league, nxt.opponent, nxt.opponent_name)} on {_day(nxt.start)})" if nxt else ""
    return f"rerun: no {me} game then{after}"


# ── Fetching ────────────────────────────────────────────────────────────────

def _parse_time(text: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _espn_ids(league_id: str, path: str, session=None) -> dict:
    """ESPN team id → leagues.json team id, for the league's teams."""
    from services.recording_art import logos as LO

    def fetch():
        body = LO._get(ESPN_TEAMS.format(path=path), session).json()
        out = []
        for lg in (body.get("sports") or [{}])[0].get("leagues") or []:
            for t in lg.get("teams") or []:
                t = t.get("team") or {}
                if t.get("id"):
                    out.append({"abbr": t.get("abbreviation") or "", "id": str(t["id"]),
                                "names": [n for n in (t.get("displayName"), t.get("shortDisplayName"),
                                                      t.get("name"), t.get("location")) if n]})
        return out
    listed = LO._cached_list("espn-ids-" + path.replace("/", "-"), fetch)
    out = {}
    for t in (LG.league(league_id) or {}).get("teams") or []:
        hit = LO.find(t, listed)
        if hit:
            out[hit["id"]] = t["id"]
    return out


def fetch(league_id: str, team_id: str, session=None) -> Optional[Schedule]:
    """The team's games from ESPN, or None when they can't be had."""
    from services.recording_art import logos as LO
    src = SOURCES.get(league_id)
    if not src:
        return None
    path, complete = src
    ids = _espn_ids(league_id, path, session)
    mine = next((e for e, t in ids.items() if t == team_id), None)
    if mine is None:
        return None
    games: dict = {}
    got = False
    for q in QUERIES[complete]:
        try:
            body = LO._get(ESPN_SCHEDULE.format(path=path, id=mine, query=q), session).json()
        except Exception as e:
            if not q:
                raise
            logger.info(f"[Recordings] {league_id} {team_id} schedule{q}: {e}")
            continue
        got = True
        for ev in body.get("events") or []:
            start = _parse_time(ev.get("date"))
            comp = (ev.get("competitions") or [{}])[0]
            sides = comp.get("competitors") or []
            if start is None or len(sides) != 2:
                continue
            me = next((c for c in sides if str((c.get("team") or {}).get("id")) == mine), None)
            other = next((c for c in sides if c is not me), None)
            if me is None or other is None:
                continue
            ot = other.get("team") or {}
            games[(start, str(ot.get("id")))] = Game(
                start, ids.get(str(ot.get("id"))), ot.get("shortDisplayName") or ot.get("displayName") or "",
                (me.get("homeAway") == "home") if me.get("homeAway") else None)
    if not got:
        return None
    return Schedule(league_id, team_id, sorted(games.values(), key=lambda g: g.start), complete)


def _disk(league_id: str, team_id: str) -> str:
    from services.recording_art import logos as LO
    return os.path.join(LO.cache_dir(), f"schedule-{league_id}-{team_id}.json")


def _save(s: Schedule) -> None:
    from services.recording_art import logos as LO
    LO._write_json(_disk(s.league, s.team), {
        "at": time.time(), "complete": s.complete,
        "games": [[g.start.isoformat(), g.opponent, g.opponent_name, g.home] for g in s.games]})


def _load(league_id: str, team_id: str) -> tuple:
    """(fetched at, Schedule) from the disk copy, or (0, None)."""
    from services.recording_art import logos as LO
    data = LO._read_json(_disk(league_id, team_id))
    if not data:
        return 0, None
    try:
        games = [Game(datetime.fromisoformat(s), o, n, h) for s, o, n, h in data.get("games") or []]
    except (TypeError, ValueError):
        return 0, None
    return data.get("at", 0), Schedule(league_id, team_id, games, bool(data.get("complete")))


def schedule(league_id: Optional[str], team_id: Optional[str], session=None) -> Optional[Schedule]:
    """The team's games, from memory, the disk copy or ESPN (when older than
    SCHEDULE_MAX_AGE); the last copy when ESPN fails; None when there is none."""
    if not supported(league_id) or not team_id:
        return None
    key = (league_id, team_id)
    with _lock:
        at, s = _mem.get(key) or (0, None)
        if not at:
            at, s = _load(league_id, team_id)
            if at:
                _mem[key] = (at, s)
        if at and time.time() - at < SCHEDULE_MAX_AGE:
            return s
        try:
            fresh = fetch(league_id, team_id, session)
        except Exception as e:
            logger.info(f"[Recordings] {league_id} {team_id} schedule not read: {e}")
            fresh = None
        if fresh is not None:
            _mem[key] = (time.time(), fresh)
            try:
                _save(fresh)
            except OSError as e:
                logger.info(f"[Recordings] schedule not kept: {e}")
            return fresh
        # keep the old copy (or nothing) and ask again in a while, not every scan
        _mem[key] = (time.time() - SCHEDULE_MAX_AGE + FAILED_RETRY, s)
        return s


def for_rules(rules: list) -> dict:
    """(league, team) → Schedule or None, for every team rule."""
    out = {}
    for r in rules:
        if r.kind == "team" and (r.league, r.team) not in out:
            out[(r.league, r.team)] = schedule(r.league, r.team)
    return out
