"""What a recording is: a game (which league, which teams), a motor race or
another league event, or a show or film (its plain title).

From Tentacle's own booking when a recording rule booked it (the rule says
the league and the team; the listing and the booking's "why" name the
other team), otherwise from the title and description, the way guides
write games:

    NHL: Toronto Maple Leafs - Montreal Canadiens     (league, home - away)
    Premier League. Arsenal - Chelsea
    Molson Habs Hockey: Canadiens vs. Blackhawks
    Canadiens at Blackhawks / Penguins@Maple Leafs @7  (the second at home)
    Live: NHL Hockey  +  "TOR @ MTL" or "From the Bell Centre" in the description

Team names and codes come from the recording rules' leagues.json and are
compared folded (accents, case), as the rules do. Pure: no database, no
network.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from services.recording_rules import leagues as LG
from services.recording_rules import match as M


@dataclass
class Subject:
    kind: str                                   # game | event | show
    title: str                                  # without "Live:", the date or a split's number
    league: Optional[dict] = None               # a leagues.json league (game, event)
    teams: list = field(default_factory=list)   # leagues.json teams, left to right
    at: bool = False                            # "A @ B": the second team is at home
    year: Optional[int] = None                  # a show's or film's year, from the title
    film: bool = False                          # the guide says it's a film
    why: str = ""                               # how it was told (logs, the status page)

    def describe(self) -> str:
        if self.kind == "game" and self.teams:
            names = f" {'@' if self.at else 'vs'} ".join(t["name"] for t in self.teams)
            return f"{self.league['name']}: {names}"
        if self.kind == "event":
            return f"{self.league['name']}: {self.title}"
        return self.title + (f" ({self.year})" if self.year else "")


# ── The title ───────────────────────────────────────────────────────────────

# Jellyfin's file stamp ("Live  NHL Hockey 2026_10_23_20_00_00"), with the
# number of a recording that was split ("..._1")
_FILE_STAMP = re.compile(r"\s+\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}(?:_\d{1,2})?$")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|déc)[a-zéû]*\.?"
_DAYS = r"(?:mon|tue|wed|thu|fri|sat|sun|lun|mar|mer|jeu|ven|sam|dim)[a-z]*\.?"
_DATE_TAIL = re.compile(
    r"\s*(?:[-–|,:]\s*)?\(?(?:"
    r"\d{4}-\d{1,2}-\d{1,2}"                                  # 2026-10-23
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"                          # 10/23, 23/10/2026
    r"|\d{1,2}\.\d{1,2}\.\d{2,4}"                             # 23.10.2026
    rf"|(?:{_DAYS},?\s+)?{_MONTHS}\s+\d{{1,2}}(?:,?\s+\d{{4}})?"     # Fri, Oct 23
    rf"|(?:{_DAYS},?\s+)?\d{{1,2}}\s+{_MONTHS}(?:\s+\d{{4}})?"       # ven. 23 oct.
    r")(?:\s+\d{1,2}[:h]\d{2}(?:\s*[ap]\.?m\.?)?)?\)?$", re.I)
# the number of a split recording or a part
_SPLIT_TAIL = re.compile(r"(?:\s*\((?:part|pt\.?|partie|teil)?\s*\d{1,2}\)"
                         r"|\s*[-–:,]?\s*(?:part|pt\.?|partie|teil)\s*\d{1,2}"
                         r"|\s+[-–]\s+\d{1,2}|_\d{1,2})$", re.I)
_YEAR_TAIL = re.compile(r"\s*\(((?:19|20)\d{2})\)$")
# an event channel's start time after the game ("Penguins@Maple Leafs @7")
_TIME_TAIL = re.compile(r"\s*@\s*\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?|[ap])?\s*(?:[ecmp][sd]?t)?$", re.I)


def clean_title(name: Optional[str]) -> tuple:
    """(the title people know, its year or None): "Live: NHL Hockey" ->
    "NHL Hockey"; "Jeopardy! 2026_10_23_19_30_00_1" -> "Jeopardy!";
    "Heat (1995)" -> ("Heat", 1995)."""
    raw = (name or "").replace(" ", " ").strip()
    if _FILE_STAMP.search(raw):
        # a name from the file: Jellyfin wrote "Live: X" as "Live  X"
        raw = re.sub(r"(?<=\S)\s{2,}(?=\S)", ": ", raw)
    t = re.sub(r"\s+", " ", raw).strip()
    year = None
    for _ in range(4):
        before = t
        t = _FILE_STAMP.sub("", t)
        t = _TIME_TAIL.sub("", t).strip()
        t = _SPLIT_TAIL.sub("", t).strip()
        t = _DATE_TAIL.sub("", t).strip()
        m = _YEAR_TAIL.search(t)
        if m and year is None:
            year, t = int(m.group(1)), t[:m.start()].strip()
        t = t.rstrip(" -–|,:").strip()
        if t == before:
            break
    t = M.strip_prefix(t) or t
    return t.strip(), year


# ── Games ───────────────────────────────────────────────────────────────────

_SEP = re.compile(r"\s+(?:-|–|—|vs\.?|v\.?|c\.|versus|contre|contra|at|à)\s+|\s*@\s*", re.I)
_AT = re.compile(r"^\s*(?:@|at|à)\s*$", re.I)
_CODES = re.compile(r"(?<![A-Za-z])([A-Z]{2,4})\s*(@|at|vs\.?|v\.?|-|–)\s*([A-Z]{2,4})(?![A-Za-z])")
_PREFIXED = re.compile(r"^\s*([^:]{2,60}?)\s*(?::|\.(?=\s))\s+(.+)$")


def _team_in(part: str, league: dict) -> Optional[dict]:
    """The one team of `league` a piece of a title names (full, short or
    nickname; its code as an upper-case word), else None."""
    found = M.teams_named(M.fold(part), part, league)
    if len(found) != 1:
        # "Los Angeles Kings" also has the nickname-less "Kings": the longest name wins
        if not found:
            return None
        best = max(found.items(), key=lambda kv: len(M.fold(kv[1])))
        if sum(1 for v in found.values() if len(M.fold(v)) == len(M.fold(best[1]))) > 1:
            return None
        found = dict([best])
    tid = next(iter(found))
    return next(t for t in league["teams"] if t["id"] == tid)


def _matchup(text: str, league: dict) -> Optional[tuple]:
    """(team, team, at) when `text` is "<A> <sep> <B>" with a team of
    `league` on each side; else None."""
    text = _TIME_TAIL.sub("", text or "").strip()
    seps = list(_SEP.finditer(text))
    for m in seps:
        left, right = text[:m.start()], text[m.end():]
        # the separator closest to the names: "NHL - Leafs - Habs" splits at the second
        a, b = _team_in(left, league), _team_in(right, league)
        if a and b and a["id"] != b["id"]:
            return a, b, bool(_AT.match(m.group(0)))
    return None


def _codes_in(description: str, league: dict) -> Optional[tuple]:
    """ "TOR @ MTL" in a description: two of the league's codes."""
    if not league.get("match_abbr", True):
        return None
    by_code = {t.get("abbr"): t for t in league.get("teams") or [] if t.get("abbr")}
    for m in _CODES.finditer(description or ""):
        a, b = by_code.get(m.group(1)), by_code.get(m.group(3))
        if a and b and a["id"] != b["id"]:
            return a, b, m.group(2).lower() in ("@", "at")
    return None


def _quoted(text: str) -> list:
    """Titles a booking's "why" quotes (“Molson Habs Hockey: Canadiens vs. Blackhawks”)."""
    return re.findall(r"[“\"]([^”\"]{3,120})[”\"]", text or "")


def _heads(title: str, sub_title: str) -> list:
    """The parts of a title that may be a matchup: the whole title, what
    follows "League:" or "League.", and the sub-title."""
    out = []
    for t in (title, sub_title):
        t = M.strip_prefix(t or "")
        if not t:
            continue
        out.append(t)
        m = _PREFIXED.match(t)
        if m:
            out.append(m.group(2))
    return out


def _context(listing: M.Listing, league: dict) -> tuple:
    """(the league's own word in the listing, its category or channel; or
    only its sport's word)."""
    around = listing.text() + M.fold(listing.category) + M.fold(listing.channel)
    words = league.get("context") or []
    own = [w for w in words if M.fold(w).strip() not in M._SPORT_WORDS
           and w not in (league.get("weak_context") or [])]
    strong = M.any_of(around, own + [league.get("name")])
    loose = M.any_of(around, [w for w in words if w not in own] + [league.get("sport")])
    return strong, loose


def _other_league(listing: M.Listing, league: dict) -> Optional[str]:
    around = listing.text() + M.fold(listing.category) + M.fold(listing.channel)
    return M.any_of(around, league["others"] if "others" in league else M._OTHER_LEAGUES)


def _game_in(listing: M.Listing, league: dict, why: str = "") -> Optional[Subject]:
    """This league's game in the listing, or None."""
    if not league.get("teams") or M.excluded(listing, league):
        return None
    strong, loose = _context(listing, league)
    if league.get("needs_context") and not strong:
        return None    # "Canada vs. Sweden" is rugby as often as it is hockey
    if _other_league(listing, league) and not strong:
        return None    # "AHL Hockey: Toronto Marlies at Belleville Senators"
    title = clean_title(listing.title)[0]
    home = M.venue_team(M.fold(listing.description), league)
    hit, said = None, ""
    for head in _heads(listing.title, listing.sub_title) + _quoted(why):
        hit, said = _matchup(head, league), "teams in the title"
        if hit:
            break
    if not hit and (strong or loose):
        hit, said = _codes_in(listing.description, league), "team codes in the description"
    if not hit and strong:
        # "Live: MLB Baseball" / "Blue Jays at Yankees."
        for sentence in re.split(r"(?<=[.!?;])\s+|\n", listing.description or ""):
            hit, said = _matchup(sentence.strip().rstrip(".!?;"), league), "teams in the description"
            if hit:
                break
    if hit:
        a, b, at = hit
        if home is not None and home["id"] == a["id"]:
            a, b, at = b, a, True      # "from the Bell Centre": the Canadiens are at home
        elif home is not None and home["id"] == b["id"]:
            at = True
        return Subject("game", title, league, [a, b], at=at, why=f"{league['name']} {said}")
    if strong and home:
        return Subject("game", title, league, [home], why=f"{league['name']}, from {home['name']}'s arena")
    return None


def _event_in(listing: M.Listing, league: dict) -> Optional[Subject]:
    """A motor race (a series league: its words in the title)."""
    if league.get("kind") != "series" or M.excluded(listing, league):
        return None
    head = M.fold(M.strip_prefix(listing.title)) + M.fold(listing.sub_title)
    words = (league.get("terms") or []) + [league.get("name")]
    if M.any_of(head, words):
        return Subject("event", _without_league(clean_title(listing.title)[0], words), league,
                       why=f"{league['name']} in the title")
    return None


def _without_league(title: str, words: list) -> str:
    """ "Formula 1: Mexico City Grand Prix" -> "Mexico City Grand Prix" (the
    card says the league already)."""
    m = _PREFIXED.match(title or "")
    if m and M.any_of(M.fold(m.group(1)), words) and M.fold(m.group(1)).strip() in {
            M.fold(w).strip() for w in words}:
        return m.group(2).strip()
    return title


def _film(category: str, genres: list) -> bool:
    words = {M.fold(g).strip() for g in [category] + list(genres or []) if g}
    return bool(words & {"movie", "movies", "film", "films", "feature film", "cinema"})


def identify(title: str, sub_title: str = "", description: str = "", channel: str = "",
             category: str = "", genres: Optional[list] = None, booking: Optional[dict] = None) -> Subject:
    """What a recording is. `booking`: what Tentacle's recording rule knew
    when it booked it: {"kind": "team"|"series"|"keywords", "league":
    "nhl", "team": "mtl", "why": "..."}."""
    listing = M.Listing(title=title or "", sub_title=sub_title or "", description=description or "",
                        category=category or "", channel=channel or "")
    plain, year = clean_title(title)
    b = booking or {}
    lg = LG.league(b.get("league")) if b.get("league") else None
    if lg and b.get("kind") == "series":
        return Subject("event", _without_league(plain, (lg.get("terms") or []) + [lg["name"]]), lg,
                       why=f"booked by the {lg['name']} rule")
    if lg and b.get("kind") == "team":
        ours = LG.team(lg["id"], b.get("team"))
        found = _game_in(M.Listing(listing.title, listing.sub_title, listing.description, listing.category,
                                   listing.channel + " " + (lg.get("name") or "")), lg, b.get("why") or "")
        if found and found.teams and (ours is None or any(t["id"] == ours["id"] for t in found.teams)):
            if len(found.teams) == 2 or ours is None:
                found.why = f"booked by the {ours['name'] if ours else lg['name']} rule"
                return found
        if ours is not None:
            # the rule's team and the one whose arena the listing is from
            home = M.venue_team(M.fold(listing.description), lg)
            other = None
            for text in [listing.title, listing.sub_title] + _quoted(b.get("why") or ""):
                named = M.teams_named(M.fold(text), text, lg)
                other = next((t for t in lg["teams"] if t["id"] in named and t["id"] != ours["id"]), None)
                if other:
                    break
            if home and home["id"] != ours["id"]:
                teams, at = [ours, home], True
            elif home and other:
                teams, at = [other, ours], True
            elif other:
                teams, at = [ours, other], False
            else:
                teams, at = [ours], False
            return Subject("game", plain, lg, teams, at=at, why=f"booked by the {ours['name']} rule")
    # not booked by a team rule: from the listing alone
    best = None
    for league in LG.leagues():
        s = _event_in(listing, league) or _game_in(listing, league)
        if s is None:
            continue
        strong, _loose = _context(listing, league)
        score = len(s.teams) * 2 + (3 if strong else 0) + (1 if s.kind == "event" else 0)
        if best is None or score > best[0]:
            best = (score, s)
    if best is not None:
        return best[1]
    # a league named, no team ("Live: NHL Hockey"): its card
    for league in LG.leagues():
        if league.get("kind") == "teams" and league.get("teams") and not league.get("needs_context"):
            head = M.fold(M.strip_prefix(listing.title))
            if M.has(head, league["name"]) and not M.excluded(listing, league):
                return Subject("event", plain, league, why=f"{league['name']} in the title")
    return Subject("show", plain, year=year, film=_film(category, genres or []), why="the title")
