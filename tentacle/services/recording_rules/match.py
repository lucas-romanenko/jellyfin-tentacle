"""Does a guide listing name a team (or a rule's words)?

Guides write the same game many ways: "Live: NHL Hockey" with "From
Scotiabank Arena in Toronto." in the description, "Molson Habs Hockey:
Canadiens vs. Blackhawks", an event channel's "Penguins@Maple Leafs @7",
"Hockey LNH" with "En provenance du Centre Bell". So the text is folded
first (accents, case, "&" = "and", "Live:"/"New:" dropped) and compared as
whole words: "Leafs" never matches "Leaf", "Kings" never "Kingston".

A team's full name ("Toronto Maple Leafs", "Canadiens de Montréal") is
enough on its own. Every other name (Leafs, Kings, a city, an arena) only
counts when the listing is about that league: a league word ("NHL",
"hockey", "LNH") in it or in the channel's name, or a second team of the
same league ("Canadiens at Blackhawks"). That keeps "Maple Leaf Pro
Wrestling", the Toronto Raptors and a Montreal football club out of a
Leafs rule. Nothing here is stored: everything compares at match time.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

_PREFIX = re.compile(r"^\s*(live|new|en direct|direct|en vivo|nouveau|replay|encore)\s*[:\-|]\s*", re.I)
_PREGAME = ("pregame", "pre game", "pre-game", "avant match", "avant-match", "warm up", "countdown",
            "pre match", "prematch", "build up", "buildup", "pre race", "avant course")


# Context words that only say the sport, not the league ("hockey" is the
# AHL's and the Olympics' too).
_SPORT_WORDS = {"hockey", "basketball", "football", "soccer", "baseball", "futbol", "calcio", "fussball",
                "racing", "motorsport"}


# Leagues that aren't in the list but share cities, names and sports with
# ones that are: a listing that names one is theirs.
_OTHER_LEAGUES = ("AHL", "ECHL", "OHL", "QMJHL", "LHJMQ", "WHL", "CHL", "PWHL", "LPHF", "KHL", "SHL", "Liiga",
                  "NCAA", "college", "collegiate", "junior", "juniors", "U Sports", "high school", "G League",
                  "minor league", "MiLB", "Triple-A", "Double-A", "USL", "NWSL", "League One", "Championship",
                  "women's", "womens", "World Juniors", "Olympic", "Olympics", "spring training", "exhibition")


def fold(text: Optional[str]) -> str:
    """Lower case, no accents, "&" = "and", "@" and "vs" kept as words, any
    other punctuation a space: " toronto maple leafs pregame "."""
    if not text:
        return " "
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = t.replace("&", " and ").replace("@", " @ ").replace("’", "'")
    t = re.sub(r"[^\w@']+", " ", t)
    t = t.replace("'", "")   # d'Ottawa -> dottawa on both sides; "Saint-Laurent" -> saint laurent
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def strip_prefix(title: Optional[str]) -> str:
    """ "Live: NHL Hockey" -> "NHL Hockey" (guides flip between the two)."""
    t = title or ""
    for _ in range(2):
        t = _PREFIX.sub("", t)
    return t.strip()


def is_live_title(title: Optional[str]) -> bool:
    return bool(re.match(r"^\s*(live|en direct|direct|en vivo)\b", title or "", re.I))


def has(folded_text: str, phrase: str) -> bool:
    """A whole-word phrase in folded text."""
    p = fold(phrase)
    return p.strip() != "" and p in folded_text


def any_of(folded_text: str, phrases: Iterable[str]) -> Optional[str]:
    for p in phrases:
        if p and has(folded_text, p):
            return p
    return None


def is_pregame(title: Optional[str]) -> bool:
    return any_of(fold(title), _PREGAME) is not None


@dataclass
class Listing:
    """What a match looks at: the programme and the channel it is on."""
    title: str = ""
    sub_title: str = ""
    description: str = ""
    category: str = ""
    channel: str = ""          # the channel's name (event channels carry the league there)

    def text(self, description: bool = True) -> str:
        parts = [strip_prefix(self.title), self.sub_title or ""]
        if description:
            parts.append(self.description or "")
        return fold(" | ".join(parts))


@dataclass
class TeamHit:
    matched: bool
    why: str = ""
    home: Optional[bool] = None       # True home game, False away, None unknown
    near: str = ""                    # a near miss: why it didn't count
    teams: list = field(default_factory=list)   # this team, then the other teams named
    game: bool = False                # the league or an opponent is named: a game, not a show about the team


def _team_names(team: dict) -> tuple:
    """(names that are enough alone, names that need the league around them)."""
    alone = [team.get("name"), team.get("fr")]
    weak = [team.get("short")] + list(team.get("nicknames") or []) + [team.get("city")]
    return [n for n in alone if n], [n for n in weak if n]


def _abbr(raw: str, abbr: Optional[str]) -> bool:
    """The team's code as its own upper-case word ("TOR @ MTL"), never "tor"."""
    return bool(abbr) and re.search(r"(?<![A-Za-z])" + re.escape(abbr) + r"(?![A-Za-z])", raw or "") is not None


def teams_named(text: str, raw: str, league: dict, matchup_with: Iterable[str] = ()) -> dict:
    """team id -> the first of its names found in the text: its full, short
    or nickname, or its code; its city only in a matchup with one of
    `matchup_with` ("Pittsburgh @ Toronto"). Arenas don't count (a listing
    "from" an arena names one team, not two)."""
    out = {}
    mine = [fold(m).strip() for m in matchup_with if fold(m).strip()]
    for t in league.get("teams") or []:
        names = [t.get("name"), t.get("fr"), t.get("short")] + list(t.get("nicknames") or [])
        hit = any_of(text, [n for n in names if n])
        if hit is None and _abbr(raw, t.get("abbr")):
            hit = t["abbr"]
        city = fold(t.get("city")).strip()
        if hit is None and city and mine and any(
                re.search(rf" ({re.escape(city)} (at|@|vs|v) {re.escape(m)}|{re.escape(m)} (at|@|vs|v) {re.escape(city)}) ",
                          text) for m in mine):
            hit = t["city"]
        if hit:
            out[t["id"]] = hit
    return out


def league_context(text: str, league: dict) -> Optional[str]:
    return any_of(text, league.get("context") or [])


def without(text: str, names: Iterable[str]) -> str:
    """The text with other teams' full names taken out, so "Toronto" in
    "Toronto Raptors" or "Toronto Argonauts" isn't the Leafs' city."""
    for n in names:
        f = fold(n)
        if f.strip() and f in text:
            text = text.replace(f, " ")
    return text


def match_team(listing: Listing, league: dict, team: dict, foreign_names: Iterable[str] = ()) -> TeamHit:
    """Is this listing a game of `team`? Home when the description names the
    team's own arena, or the listing reads "<other> at/@ <team>".
    `foreign_names`: full names of teams of other leagues (and other teams
    of this one) that may contain this team's city or name."""
    full = listing.text()
    text = without(full, foreign_names)
    raw = " ".join([listing.title or "", listing.sub_title or "", listing.description or ""])
    alone, weak = _team_names(team)
    strong = any_of(full, alone)
    named = any_of(text, weak)
    venue = any_of(text, team.get("venue") or [])
    if not (strong or named or venue) and _abbr(raw, team.get("abbr")):
        named = team["abbr"]
    if not (strong or named or venue):
        return TeamHit(False)
    around = text + fold(listing.category) + fold(listing.channel)
    ctx = league_context(around, league)
    mine = [n for n in (alone + weak + [team.get("abbr")]) if n]
    others = {k: v for k, v in teams_named(text, raw, league, mine).items() if k != team["id"]}
    if not strong and not ctx and not others:
        what = named or venue
        return TeamHit(False, near=f"“{what}” but nothing says {league.get('name')}")
    other = any_of(around, _OTHER_LEAGUES)
    if other and not strong and (not ctx or fold(ctx).strip() in _SPORT_WORDS):
        # "AHL Hockey: Toronto Marlies at Belleville Senators"
        return TeamHit(False, near=f"“{named or venue}” in a {other} listing")
    if (not strong and not venue and named and named == team.get("city") and not others
            and fold(ctx).strip() in _SPORT_WORDS):
        # "AHL Hockey: Toronto Marlies" is hockey in Toronto, not the Leafs:
        # a city alone needs the league itself, or the other team.
        return TeamHit(False, near=f"“{named}” in a {ctx} listing that doesn't say {league.get('name')}")
    home = None
    if venue:
        home = True
    else:
        other_venue = next((t for t in league.get("teams") or []
                            if t["id"] != team["id"] and any_of(text, t.get("venue") or [])), None)
        if other_venue is not None:
            home = False
        else:
            home = _at_order(text, mine, others.values())
    what = strong or named or venue
    why = f"“{what}”" + (f" with {ctx}" if ctx and not strong else "") + \
        (f" vs {next(iter(others.values()))}" if others and not ctx and not strong else "")
    return TeamHit(True, why=why, home=home, teams=[team["id"], *others],
                   game=bool(ctx or others))


def venue_team(text: str, league: dict) -> Optional[dict]:
    """The team whose arena a listing is from ("From the United Center in Chicago.")."""
    return next((t for t in league.get("teams") or [] if any_of(text, t.get("venue") or [])), None)


def _at_order(text: str, mine: list, theirs: Iterable[str]) -> Optional[bool]:
    """ "Penguins at Maple Leafs" / "Penguins @ Maple Leafs": the second is at
    home. "vs" says nothing (guides write it both ways)."""
    for m in mine:
        fm = fold(m).strip()
        for o in theirs:
            fo = fold(o).strip()
            if not fm or not fo:
                continue
            if re.search(rf" {re.escape(fo)} (at|@) {re.escape(fm)} ", text):
                return True
            if re.search(rf" {re.escape(fm)} (at|@) {re.escape(fo)} ", text):
                return False
    return None


@dataclass
class KeywordHit:
    matched: bool
    why: str = ""
    near: str = ""


def match_keywords(listing: Listing, terms: list, excludes: list, match_all: bool,
                   description: bool) -> KeywordHit:
    """Typed words, whole words, accents and case folded: any (or every) term
    in the title (and the description when asked), and no excluded one."""
    text = listing.text(description=description)
    terms = [t for t in terms if fold(t).strip()]
    if not terms:
        return KeywordHit(False)
    found = [t for t in terms if has(text, t)]
    if not found or (match_all and len(found) < len(terms)):
        if found:
            return KeywordHit(False, near=f"“{found[0]}” but not every word")
        return KeywordHit(False)
    bad = any_of(listing.text(description=True), excludes)
    if bad:
        return KeywordHit(False, near=f"“{found[0]}” but it says “{bad}”")
    return KeywordHit(True, why=" + ".join(f"“{t}”" for t in found))


def identity(title: Optional[str], description: Optional[str]) -> str:
    """The same programme on another channel or at another time (a replay):
    the title without "Live:", and the description."""
    return (fold(strip_prefix(title)) + "|" + fold(description)).strip()


def is_playoff(listing: Listing, league: Optional[dict]) -> bool:
    words = (league or {}).get("playoff_words") or ["playoff", "playoffs"]
    return any_of(listing.text(), words) is not None
