"""Team logos for the matchup cards (cards.py), and their cache.

For a leagues.json team, the first source that has its logo:

  1. ESPN's public team pages (no key): the league's team list
     (site.api.espn.com .../teams) gives every team's logo on ESPN's CDN,
     the one made for dark backgrounds when there is one. NHL, NBA, WNBA,
     NFL, MLB, MLS and the big five soccer leagues. National teams (FIFA,
     IIHF) get their country's flag from the same CDN, by the team's code.
     A soccer club that went down is found in the league below.
  2. EuroLeague's own public club list (api-live.euroleague.net): crests.
  3. TheSportsDB's free tier (its public key, no account): the team's badge,
     found by name within the league's sport. The CFL, and any team the
     others don't list. Its free tier allows few calls a minute: when it
     says "too many", the rest waits for the next pass.
  4. logo.dev by the team's name, only when the playlist logos' key is set
     (Settings → logodev_api_key).
  5. None: the card draws the team's code on a coloured badge, as before.

Every logo is fetched once and kept as a trimmed transparent PNG in
DATA_DIR/team_logos (`<league>-<team>.png`); `index.json` there says where
each came from, or when a team had none (asked again after MISS_RETRY).
ESPN's and EuroLeague's team lists are kept there too and fetched again after LIST_MAX_AGE.
Drawing a card never fetches: `fetch_missing` runs in the pass, before the
cards are drawn, at most FETCHES_PER_PASS logos each pass.
"""
import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 10
MAX_BYTES = 2 * 1024 * 1024
MIN_SIDE = 32                       # smaller: a placeholder, not a logo
KEEP_SIDE = 400                     # logos are kept at most this big
LIST_MAX_AGE = 30 * 86400           # ESPN's team lists
MISS_RETRY = 86400                  # a team with no logo anywhere: asked again a day later
FETCHES_PER_PASS = 8
UA = {"User-Agent": "Tentacle (+https://github.com/lucas-romanenko/jellyfin-tentacle)"}

ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams?limit=500"
ESPN_FLAG = "https://a.espncdn.com/i/teamlogos/countries/500/{code}.png"
SPORTSDB_SEARCH = "https://www.thesportsdb.com/api/v1/json/123/searchteams.php?t={name}"
LOGODEV_NAME = "https://img.logo.dev/name/{name}?size=300&format=png&token={token}"

EUROLEAGUE_CLUBS = "https://api-live.euroleague.net/v2/competitions/E/seasons/E{year}/clubs"

# league id → ESPN's sport/league paths (the league, then the one below), or
# "flag" for national teams
ESPN = {
    "nhl": ["hockey/nhl"], "nba": ["basketball/nba"], "wnba": ["basketball/wnba"],
    "nfl": ["football/nfl"], "mlb": ["baseball/mlb"], "mls": ["soccer/usa.1"],
    "epl": ["soccer/eng.1", "soccer/eng.2"], "laliga": ["soccer/esp.1", "soccer/esp.2"],
    "seriea": ["soccer/ita.1", "soccer/ita.2"], "bundesliga": ["soccer/ger.1", "soccer/ger.2"],
    "ligue1": ["soccer/fra.1", "soccer/fra.2"],
    "fifa": "flag", "iihf": "flag",
}
# leagues.json's sport → TheSportsDB's
SPORTSDB_SPORT = {"hockey": "Ice Hockey", "canadian football": "American Football"}



class TooMany(Exception):
    """A source said "too many requests": no more asking this pass."""


_lock = threading.Lock()
_mem: dict = {}                     # file name → PIL image (RGBA), drawn often


def cache_dir() -> str:
    return os.path.join(os.getenv("DATA_DIR", "/data"), "team_logos")


def _key(league: dict, team: dict) -> str:
    return f"{league.get('id')}-{team.get('id')}"


def _file(league: dict, team: dict) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "_", _key(league, team).lower())
    return os.path.join(cache_dir(), safe + ".png")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\b(fc|cf|sc|afc|ac|bc|bk|kk|sk)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# ── The index ───────────────────────────────────────────────────────────────

def _index_path() -> str:
    return os.path.join(cache_dir(), "index.json")


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def index() -> dict:
    return _read_json(_index_path()) or {}


# ── A logo from the cache (drawing) ─────────────────────────────────────────

def logo(league: Optional[dict], team: dict):
    """The team's logo (PIL RGBA, trimmed) from the cache, or None. Never fetches."""
    if not league or not team.get("id"):
        return None
    path = _file(league, team)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _mem.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        from PIL import Image
        img = Image.open(path)
        img.load()
        img = img.convert("RGBA")
    except Exception as e:
        logger.debug(f"[Team logos] {path}: {e}")
        return None
    img.info["plate"] = bool(index().get(_key(league, team), {}).get("plate"))
    _mem[path] = (mtime, img)
    return img


def needs_plate(img, source: Optional[str]) -> bool:
    """A logo that would vanish on the cards' dark background gets a light
    plate behind it: EuroLeague's crests (made for white: black sponsors'
    names) and logos that are mostly dark, edge included (ASVEL, Metz)."""
    if source == "euroleague":
        return True
    from PIL import ImageChops, ImageFilter
    small = img.copy()
    small.thumbnail((160, 160))
    alpha = small.getchannel("A").point(lambda a: 255 if a > 128 else 0)
    edge = ImageChops.subtract(alpha, alpha.filter(ImageFilter.MinFilter(5)))
    luma = list(small.convert("L").getdata())
    inside = [l for l, a in zip(luma, alpha.getdata()) if a]
    rim = [l for l, e in zip(luma, edge.getdata()) if e]
    if not inside or not rim:
        return False
    dark = sum(1 for l in inside if l < 60) / len(inside)
    dark_rim = sum(1 for l in rim if l < 60) / len(rim)
    return dark >= 0.7 and dark_rim >= 0.9


# ── Fetching (the pass) ─────────────────────────────────────────────────────

def _get(url: str, session=None):
    r = (session or requests).get(url, timeout=TIMEOUT, headers=UA)
    if r.status_code == 429:
        raise TooMany(url.split("/")[2])
    r.raise_for_status()
    return r


def _clean(data: bytes):
    """A fetched picture as a trimmed transparent RGBA image, or None if it is
    no logo (not a picture, too small, all one colour, nothing opaque)."""
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGBA")
    except Exception:
        return None
    box = img.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
    if not box:
        return None
    img = img.crop(box)
    if min(img.size) < MIN_SIDE // 2 or max(img.size) < MIN_SIDE:
        return None
    lo, hi = img.convert("L").getextrema()
    alpha_lo, _ = img.getchannel("A").getextrema()
    if hi - lo < 8 and alpha_lo > 200:
        return None                  # a flat square: a placeholder
    if max(img.size) > KEEP_SIDE:
        img.thumbnail((KEEP_SIDE, KEEP_SIDE), Image.LANCZOS)
    return img


def _cached_list(name: str, fetch) -> list:
    """A source's team list [{abbr, names, logo}] from the cache, fetched
    again when older than LIST_MAX_AGE (the old copy stays when that fails)."""
    cache = os.path.join(cache_dir(), f"{name}.json")
    data = _read_json(cache)
    if data and time.time() - data.get("at", 0) < LIST_MAX_AGE:
        return data.get("teams") or []
    try:
        teams = fetch()
        if teams:
            _write_json(cache, {"at": time.time(), "teams": teams})
            return teams
    except TooMany:
        raise
    except Exception as e:
        logger.info(f"[Team logos] {name}: {e}")
    return (data or {}).get("teams") or []


def _espn_list(path: str, session=None) -> list:
    """ESPN's teams of a league, the logo for dark backgrounds when there is one."""
    def fetch():
        body = _get(ESPN_TEAMS.format(path=path), session).json()
        teams = []
        for league in (body.get("sports") or [{}])[0].get("leagues") or []:
            for t in league.get("teams") or []:
                t = t.get("team") or {}
                logos = t.get("logos") or []
                dark = [l["href"] for l in logos if "dark" in (l.get("rel") or []) and
                        "scoreboard" not in (l.get("rel") or []) and l.get("href")]
                plain = [l["href"] for l in logos if "default" in (l.get("rel") or []) and l.get("href")]
                url = (dark or plain or [None])[0]
                if not url:
                    continue
                teams.append({"abbr": t.get("abbreviation") or "", "logo": url,
                              "names": [n for n in (t.get("displayName"), t.get("shortDisplayName"),
                                                    t.get("name"), t.get("location")) if n]})
        return teams
    return _cached_list("espn-" + path.replace("/", "-"), fetch)


def _euroleague_list(session=None) -> list:
    """EuroLeague's clubs this season (the season before, early on)."""
    def fetch():
        now = time.gmtime()
        year = now.tm_year if now.tm_mon >= 8 else now.tm_year - 1
        for y in (year, year - 1):
            clubs = (_get(EUROLEAGUE_CLUBS.format(year=y), session).json() or {}).get("data") or []
            teams = [{"abbr": c.get("tvCode") or "", "logo": (c.get("images") or {}).get("crest"),
                      "names": [n for n in (c.get("clubPermanentName"), c.get("abbreviatedName"), c.get("name"),
                                            c.get("editorialName"), c.get("clubPermanentAlias")) if n]}
                     for c in clubs if (c.get("images") or {}).get("crest")]
            if teams:
                return teams
        return []
    return _cached_list("euroleague", fetch)


def match(team: dict, listed: list) -> Optional[str]:
    """The logo of `team` (a leagues.json team) in a source's list: by full
    name, then by code when only one has it, then by any of its names, then
    by a name one starts with the other ("1. FC Heidenheim 1846")."""
    want = {_fold(n) for n in [team.get("name"), team.get("short")] + list(team.get("nicknames") or []) if n}
    want.discard("")
    # the full name first: "Rangers" or "Kings" alone is two teams
    for t in listed:
        if _fold(t["names"][0]) in want:
            return t["logo"]
    abbr = (team.get("abbr") or "").upper()
    hits = [t for t in listed if abbr and t["abbr"].upper() == abbr]
    if len(hits) == 1:
        return hits[0]["logo"]
    for t in listed:
        if any(_fold(n) in want for n in t["names"][1:3]):
            return t["logo"]
    full = _fold(team.get("name"))
    hits = [t for t in listed if len(full) >= 6 and any(
        len(_fold(n)) >= 6 and (_fold(n).startswith(full) or full.startswith(_fold(n))) for n in t["names"][:1])]
    return hits[0]["logo"] if len(hits) == 1 else None


def _espn_url(league: dict, team: dict, session=None) -> Optional[str]:
    paths = ESPN.get(league.get("id"))
    if not paths:
        return None
    if paths == "flag":
        code = (team.get("abbr") or "").strip().lower()
        return ESPN_FLAG.format(code=code) if re.fullmatch(r"[a-z]{3}", code) else None
    for path in paths:
        url = match(team, _espn_list(path, session))
        if url:
            return url
    return None


def _euroleague_url(league: dict, team: dict, session=None) -> Optional[str]:
    return match(team, _euroleague_list(session)) if league.get("id") == "euroleague" else None


def _sportsdb_url(league: dict, team: dict, session=None) -> Optional[str]:
    sport = (league.get("sport") or "").lower()
    sport = SPORTSDB_SPORT.get(sport, league.get("sport") or "")
    names = []
    for n in (team.get("name"), (team.get("name") or "").replace("-", " "), team.get("short")):
        if n and n not in names:
            names.append(n)
    for name in names[:3]:
        body = _get(SPORTSDB_SEARCH.format(name=quote(name.replace(" ", "_"))), session).json()
        found = [t for t in (body.get("teams") or []) if (t.get("strSport") or "").lower() == sport.lower()
                 and t.get("strBadge")]
        league_name = (league.get("name") or "").lower()
        found.sort(key=lambda t: (league_name not in (t.get("strLeague") or "").lower(),
                                  _fold(t.get("strTeam")) != _fold(name)))
        if found:
            return found[0]["strBadge"]
    return None


def _picture(url: str, session=None):
    return _clean(_get(url, session).content)


def fetch_one(league: dict, team: dict, logodev_token: Optional[str] = None, session=None) -> Optional[str]:
    """Fetches the team's logo into the cache; returns the source that had it
    (espn | euroleague | sportsdb | logo.dev), or None (written down: asked again after
    MISS_RETRY)."""
    img, source, tried = None, None, []
    for name, find in (("espn", _espn_url), ("euroleague", _euroleague_url), ("sportsdb", _sportsdb_url)):
        try:
            url = find(league, team, session)
            if url:
                img = _picture(url, session)
                if img is not None:
                    source = name
                    break
                tried.append(f"{name}: no picture")
        except TooMany:
            raise
        except Exception as e:
            tried.append(f"{name}: {type(e).__name__}")
    if img is None and logodev_token:
        try:
            img = _picture(LOGODEV_NAME.format(name=quote(team.get("name") or ""), token=logodev_token), session)
            source = "logo.dev" if img is not None else None
        except TooMany:
            raise
        except Exception as e:
            tried.append(f"logo.dev: {type(e).__name__}")    # never the address: it holds the key
    with _lock:
        idx = index()
        if img is not None:
            path = _file(league, team)
            os.makedirs(cache_dir(), exist_ok=True)
            img.save(path + ".tmp", "PNG", optimize=True)
            os.replace(path + ".tmp", path)
            idx[_key(league, team)] = {"source": source, "at": time.time(), "plate": needs_plate(img, source)}
        else:
            idx[_key(league, team)] = {"source": None, "at": time.time(), "why": "; ".join(tried)[:200]}
        _write_json(_index_path(), idx)
    logger.info(f"[Team logos] {league.get('name')}: {team.get('name')}: {source or 'none'}")
    return source


def missing(league: Optional[dict], teams: list) -> list:
    """The teams whose logo isn't in the cache and is due to be asked for."""
    if not league:
        return []
    idx = index()
    out = []
    for t in teams:
        if not t.get("id") or os.path.exists(_file(league, t)):
            continue
        seen = idx.get(_key(league, t))
        if seen and not seen.get("source") and time.time() - seen.get("at", 0) < MISS_RETRY:
            continue
        out.append(t)
    return out


def fetch_missing(league: Optional[dict], teams: list, budget: list, logodev_token: Optional[str] = None,
                  session=None) -> int:
    """Fetches the logos `teams` still need, while `budget[0]` (fetches left
    this pass) lasts. Returns how many were fetched."""
    n = 0
    for t in missing(league, teams):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            if fetch_one(league, t, logodev_token, session):
                n += 1
        except TooMany as e:
            logger.info(f"[Team logos] {e} says too many requests: the rest next pass")
            budget[0] = 0
    return n


def has_logos(league: Optional[dict], teams: list) -> bool:
    return bool(teams) and all(logo(league, t) is not None for t in teams)
