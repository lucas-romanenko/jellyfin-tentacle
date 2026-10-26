"""The built-in list of leagues and their teams (leagues.json, edited by
hand: full names, short names, nicknames, codes, cities, French names and
home arenas, the words guides use). Static on purpose, so matching works
the same for everyone, anywhere; only the rerun check asks a schedule
(schedule.py), and does without it."""
import json
import os
from functools import lru_cache
from typing import Optional

_PATH = os.path.join(os.path.dirname(__file__), "leagues.json")


@lru_cache(maxsize=1)
def _data() -> dict:
    with open(_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for lg in data["leagues"]:
        for t in lg.get("teams") or []:
            # one- and two-letter nicknames match too much once folded ("CH")
            t["nicknames"] = [n for n in t.get("nicknames") or [] if len(n.strip()) >= 3]
    return data


def leagues() -> list:
    return _data()["leagues"]


def league(league_id: Optional[str]) -> Optional[dict]:
    return next((lg for lg in leagues() if lg["id"] == league_id), None)


def team(league_id: Optional[str], team_id: Optional[str]) -> Optional[dict]:
    lg = league(league_id)
    return next((t for t in (lg or {}).get("teams") or [] if t["id"] == team_id), None)


def summary() -> list:
    """For the team picker: every league with its teams (id, name, city, code)."""
    out = []
    for lg in leagues():
        out.append({
            "id": lg["id"], "name": lg["name"], "sport": lg.get("sport"), "kind": lg.get("kind", "teams"),
            "country": lg.get("country"),
            "min_minutes": lg.get("min_minutes"), "post_padding_minutes": lg.get("post_padding_minutes"),
            "teams": [{"id": t["id"], "name": t["name"], "short": t.get("short"), "abbr": t.get("abbr"),
                       # other names, for the picker's search ("Suède", "Ρεάλ Μαδρίτης")
                       "also": [n for n in [t.get("fr")] + list(t.get("nicknames") or []) if n]}
                      for t in sorted(lg.get("teams") or [], key=lambda t: t["name"])],
        })
    return out


@lru_cache(maxsize=64)
def foreign_names(league_id: str, team_id: str) -> tuple:
    """Full names of every other team, in every league, that contain one of
    this team's names or its city ("Toronto Raptors" for the Leafs): taken
    out of a listing before the Leafs' weaker names are looked for."""
    from services.recording_rules.match import fold
    t = team(league_id, team_id) or {}
    words = [fold(w).strip() for w in [t.get("city"), t.get("short")] + list(t.get("nicknames") or []) if w]
    if league(league_id) and league(league_id).get("needs_context"):
        words += [fold(w).strip() for w in (t.get("name"), t.get("fr")) if w]
    # the same name in another league ("Canada" in football and in hockey,
    # "Real Madrid" the football and the basketball club) is this team's own
    own = {fold(w) for w in [t.get("name"), t.get("fr"), t.get("short")] + list(t.get("nicknames") or []) if w}
    out = []
    for lg in leagues():
        for o in lg.get("teams") or []:
            if lg["id"] == league_id and o["id"] == team_id:
                continue
            names = [o.get("name"), o.get("fr")]
            if lg.get("needs_context"):
                names += list(o.get("nicknames") or [])   # "Irlanda del Norte" for Ireland
            for n in names:
                fn = fold(n)
                if n and fn not in own and any(f" {w} " in fn for w in words if w):
                    out.append(n)
    return tuple(out)
