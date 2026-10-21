"""Where each person watches from, for local and regional channels.

A provider lists a network once per city ("CA EN: CTV TORONTO HD", "CA EN: CTV
VANCOUVER HD", "US: NBC (KING) SEATTLE"); the catalog folds them into one
network (services/livetv_catalog.py) whose default variant is the
household's: the admin's place. An admin sets each person's place when
inviting them (default: the household's) and can change it on People.

A place is a city with the words that find its variants, nearest first:
the city ("VANCOUVER"), its province or state ("BRITISH COLUMBIA"), the
other cities of the province by distance, then nearby cities over the
border by distance ("SEATTLE", for US networks), then its time zone feed
("PACIFIC", "WEST"). The list people choose from is the places the
provider's channel names mention (plus the household's), each with its
country.

The household's place: the admin's (the first admin's `livetv_location`),
else the setting `livetv_location`, else the one nearest the setting
`livetv_region` / the server's time zone (livetv_catalog._region()).
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from models.database import TentacleUser, get_setting

# Jellyfin tag on lineup channels a person at <slug> doesn't see (they block it).
NOT_LOCAL_PREFIX = "tentacle-notlocal-"

_COUNTRY_NAMES = {"CA": "Canada", "US": "United States"}
_PROVINCES = {
    "ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta", "SK": "Saskatchewan",
    "MB": "Manitoba", "NS": "Nova Scotia", "NB": "New Brunswick", "PE": "Prince Edward Island",
    "NL": "Newfoundland",
}
# Time zone -> the feed words of a channel for it.
_FEEDS = {
    "NT": ["ATLANTIC", "EAST", "EASTERN"], "AT": ["ATLANTIC", "EAST", "EASTERN"], "ET": ["EAST", "EASTERN"],
    "CT": ["CENTRAL"], "MT": ["MOUNTAIN", "WEST", "WESTERN"], "PT": ["PACIFIC", "WEST", "WESTERN"],
    "AKT": ["PACIFIC", "WEST", "WESTERN"], "HT": ["PACIFIC", "WEST", "WESTERN"],
}
# Extra region words: a region a place is in ("MARITIMES"), other spellings.
_ALSO = {
    "toronto": ["GTA"], "kitchener": ["WATERLOO"], "gatineau": ["HULL"], "halifax": ["MARITIMES", "ATLANTIC CANADA"],
    "moncton": ["MARITIMES", "ATLANTIC CANADA"], "saint-john": ["MARITIMES", "ATLANTIC CANADA"],
    "fredericton": ["MARITIMES", "ATLANTIC CANADA"], "charlottetown": ["PEI", "MARITIMES", "ATLANTIC CANADA"],
    "sydney": ["MARITIMES", "ATLANTIC CANADA"], "st-johns": ["ATLANTIC CANADA"], "kelowna": ["OKANAGAN"],
    "winnipeg": ["PRAIRIES"], "regina": ["PRAIRIES"], "saskatoon": ["PRAIRIES"], "sault-ste-marie": ["SAULT"],
    "new-york": ["NYC", "TRI STATE"], "los-angeles": ["LA", "SOCAL", "LONG BEACH"], "philadelphia": ["PHILLY"],
    "dallas": ["FORT WORTH", "DALLAS FORT WORTH", "DFW"], "san-francisco": ["BAY AREA", "OAKLAND", "SAN JOSE", "NORCAL"],
    "seattle": ["TACOMA"], "minneapolis": ["ST PAUL"], "miami": ["FORT LAUDERDALE", "MIAMI-FORT LAUDERDALE"],
    "tampa": ["TAMPA BAY", "ST PETERSBURG"], "st-louis": ["SAINT LOUIS"], "raleigh": ["DURHAM"],
    "washington-dc": ["WASHINGTON"], "boston": ["NEW ENGLAND"], "norfolk": ["HAMPTON", "NEWPORT NEWS"],
}

# (city as channel names spell it, province/state code, country, time zone, latitude, longitude)
_PLACES = [
    ("TORONTO", "ON", "CA", "ET", 43.65, -79.38), ("OTTAWA", "ON", "CA", "ET", 45.42, -75.70),
    ("HAMILTON", "ON", "CA", "ET", 43.26, -79.87), ("KITCHENER", "ON", "CA", "ET", 43.45, -80.49),
    ("LONDON", "ON", "CA", "ET", 42.98, -81.25), ("WINDSOR", "ON", "CA", "ET", 42.31, -83.04),
    ("BARRIE", "ON", "CA", "ET", 44.39, -79.69), ("OSHAWA", "ON", "CA", "ET", 43.90, -78.86),
    ("KINGSTON", "ON", "CA", "ET", 44.23, -76.49), ("PETERBOROUGH", "ON", "CA", "ET", 44.30, -78.32),
    ("SUDBURY", "ON", "CA", "ET", 46.49, -80.99), ("TIMMINS", "ON", "CA", "ET", 48.48, -81.33),
    ("NORTH BAY", "ON", "CA", "ET", 46.31, -79.46), ("SAULT STE MARIE", "ON", "CA", "ET", 46.52, -84.33),
    ("THUNDER BAY", "ON", "CA", "ET", 48.38, -89.25),
    ("MONTREAL", "QC", "CA", "ET", 45.50, -73.57), ("QUEBEC CITY", "QC", "CA", "ET", 46.81, -71.21),
    ("SHERBROOKE", "QC", "CA", "ET", 45.40, -71.89), ("GATINEAU", "QC", "CA", "ET", 45.48, -75.70),
    ("TROIS RIVIERES", "QC", "CA", "ET", 46.34, -72.54), ("SAGUENAY", "QC", "CA", "ET", 48.43, -71.07),
    ("RIMOUSKI", "QC", "CA", "ET", 48.45, -68.52),
    ("VANCOUVER", "BC", "CA", "PT", 49.28, -123.12), ("VICTORIA", "BC", "CA", "PT", 48.43, -123.37),
    ("KELOWNA", "BC", "CA", "PT", 49.89, -119.50), ("KAMLOOPS", "BC", "CA", "PT", 50.67, -120.33),
    ("PRINCE GEORGE", "BC", "CA", "PT", 53.92, -122.75),
    ("CALGARY", "AB", "CA", "MT", 51.05, -114.07), ("EDMONTON", "AB", "CA", "MT", 53.55, -113.49),
    ("LETHBRIDGE", "AB", "CA", "MT", 49.69, -112.84), ("RED DEER", "AB", "CA", "MT", 52.27, -113.81),
    ("MEDICINE HAT", "AB", "CA", "MT", 50.04, -110.68),
    ("WINNIPEG", "MB", "CA", "CT", 49.90, -97.14), ("BRANDON", "MB", "CA", "CT", 49.85, -99.95),
    ("REGINA", "SK", "CA", "CT", 50.45, -104.61), ("SASKATOON", "SK", "CA", "CT", 52.13, -106.67),
    ("HALIFAX", "NS", "CA", "AT", 44.65, -63.58), ("SYDNEY", "NS", "CA", "AT", 46.14, -60.19),
    ("MONCTON", "NB", "CA", "AT", 46.09, -64.78), ("SAINT JOHN", "NB", "CA", "AT", 45.27, -66.06),
    ("FREDERICTON", "NB", "CA", "AT", 45.96, -66.64), ("CHARLOTTETOWN", "PE", "CA", "AT", 46.24, -63.13),
    ("ST JOHNS", "NL", "CA", "NT", 47.56, -52.71),
    # United States: the larger TV markets, and those near Canada
    ("NEW YORK", "NY", "US", "ET", 40.71, -74.01), ("BUFFALO", "NY", "US", "ET", 42.89, -78.88),
    ("ROCHESTER", "NY", "US", "ET", 43.16, -77.61), ("SYRACUSE", "NY", "US", "ET", 43.05, -76.15),
    ("ALBANY", "NY", "US", "ET", 42.65, -73.75), ("BINGHAMTON", "NY", "US", "ET", 42.10, -75.92),
    ("WATERTOWN", "NY", "US", "ET", 43.97, -75.91), ("PLATTSBURGH", "NY", "US", "ET", 44.70, -73.45),
    ("BURLINGTON", "VT", "US", "ET", 44.48, -73.21), ("BOSTON", "MA", "US", "ET", 42.36, -71.06),
    ("HARTFORD", "CT", "US", "ET", 41.76, -72.67), ("PROVIDENCE", "RI", "US", "ET", 41.82, -71.41),
    ("BANGOR", "ME", "US", "ET", 44.80, -68.77), ("PHILADELPHIA", "PA", "US", "ET", 39.95, -75.17),
    ("PITTSBURGH", "PA", "US", "ET", 40.44, -79.99), ("HARRISBURG", "PA", "US", "ET", 40.27, -76.88),
    ("ERIE", "PA", "US", "ET", 42.13, -80.09), ("BALTIMORE", "MD", "US", "ET", 39.29, -76.61),
    ("WASHINGTON DC", "DC", "US", "ET", 38.91, -77.04), ("RICHMOND", "VA", "US", "ET", 37.54, -77.44),
    ("NORFOLK", "VA", "US", "ET", 36.85, -76.29), ("CHARLOTTE", "NC", "US", "ET", 35.23, -80.84),
    ("RALEIGH", "NC", "US", "ET", 35.78, -78.64), ("GREENSBORO", "NC", "US", "ET", 36.07, -79.79),
    ("ATLANTA", "GA", "US", "ET", 33.75, -84.39), ("SAVANNAH", "GA", "US", "ET", 32.08, -81.09),
    ("MIAMI", "FL", "US", "ET", 25.76, -80.19), ("TAMPA", "FL", "US", "ET", 27.95, -82.46),
    ("ORLANDO", "FL", "US", "ET", 28.54, -81.38), ("JACKSONVILLE", "FL", "US", "ET", 30.33, -81.66),
    ("DETROIT", "MI", "US", "ET", 42.33, -83.05), ("GRAND RAPIDS", "MI", "US", "ET", 42.96, -85.67),
    ("LANSING", "MI", "US", "ET", 42.73, -84.56), ("FLINT", "MI", "US", "ET", 43.01, -83.69),
    ("CLEVELAND", "OH", "US", "ET", 41.50, -81.69), ("COLUMBUS", "OH", "US", "ET", 39.96, -83.00),
    ("CINCINNATI", "OH", "US", "ET", 39.10, -84.51), ("TOLEDO", "OH", "US", "ET", 41.65, -83.54),
    ("DAYTON", "OH", "US", "ET", 39.76, -84.19), ("INDIANAPOLIS", "IN", "US", "ET", 39.77, -86.16),
    ("LOUISVILLE", "KY", "US", "ET", 38.25, -85.76), ("NASHVILLE", "TN", "US", "CT", 36.16, -86.78),
    ("MEMPHIS", "TN", "US", "CT", 35.15, -90.05), ("CHICAGO", "IL", "US", "CT", 41.88, -87.63),
    ("MILWAUKEE", "WI", "US", "CT", 43.04, -87.91), ("MADISON", "WI", "US", "CT", 43.07, -89.40),
    ("GREEN BAY", "WI", "US", "CT", 44.51, -88.02), ("MINNEAPOLIS", "MN", "US", "CT", 44.98, -93.27),
    ("DULUTH", "MN", "US", "CT", 46.79, -92.10), ("ST LOUIS", "MO", "US", "CT", 38.63, -90.20),
    ("KANSAS CITY", "MO", "US", "CT", 39.10, -94.58), ("DES MOINES", "IA", "US", "CT", 41.59, -93.62),
    ("OMAHA", "NE", "US", "CT", 41.26, -95.93), ("FARGO", "ND", "US", "CT", 46.88, -96.79),
    ("SIOUX FALLS", "SD", "US", "CT", 43.55, -96.73), ("DALLAS", "TX", "US", "CT", 32.78, -96.80),
    ("HOUSTON", "TX", "US", "CT", 29.76, -95.37), ("SAN ANTONIO", "TX", "US", "CT", 29.42, -98.49),
    ("AUSTIN", "TX", "US", "CT", 30.27, -97.74), ("OKLAHOMA CITY", "OK", "US", "CT", 35.47, -97.52),
    ("TULSA", "OK", "US", "CT", 36.15, -95.99), ("NEW ORLEANS", "LA", "US", "CT", 29.95, -90.07),
    ("LITTLE ROCK", "AR", "US", "CT", 34.75, -92.29), ("BIRMINGHAM", "AL", "US", "CT", 33.52, -86.80),
    ("DENVER", "CO", "US", "MT", 39.74, -104.99), ("SALT LAKE CITY", "UT", "US", "MT", 40.76, -111.89),
    ("PHOENIX", "AZ", "US", "MT", 33.45, -112.07), ("ALBUQUERQUE", "NM", "US", "MT", 35.08, -106.65),
    ("BOISE", "ID", "US", "MT", 43.62, -116.20), ("BILLINGS", "MT", "US", "MT", 45.78, -108.50),
    ("GREAT FALLS", "MT", "US", "MT", 47.50, -111.30), ("EL PASO", "TX", "US", "MT", 31.76, -106.49),
    ("SEATTLE", "WA", "US", "PT", 47.61, -122.33), ("SPOKANE", "WA", "US", "PT", 47.66, -117.43),
    ("PORTLAND", "OR", "US", "PT", 45.52, -122.68), ("SAN FRANCISCO", "CA", "US", "PT", 37.77, -122.42),
    ("SACRAMENTO", "CA", "US", "PT", 38.58, -121.49), ("LOS ANGELES", "CA", "US", "PT", 34.05, -118.24),
    ("SAN DIEGO", "CA", "US", "PT", 32.72, -117.16), ("FRESNO", "CA", "US", "PT", 36.74, -119.79),
    ("LAS VEGAS", "NV", "US", "PT", 36.17, -115.14), ("RENO", "NV", "US", "PT", 39.53, -119.81),
    ("HONOLULU", "HI", "US", "HT", 21.31, -157.86), ("ANCHORAGE", "AK", "US", "AKT", 61.22, -149.90),
]

# Other cities that count as "nearby": within this distance, at most this many.
NEAR_KM = 700
NEAR_MAX = 12

def slug_of(city: str) -> str:
    return "-".join(city.lower().replace("'", "").split())


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[4], a[5], b[4], b[5]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _state_name(code: str) -> Optional[str]:
    from services.livetv_names import STATE_NAMES
    return STATE_NAMES.get(code)


def _build() -> dict:
    out = {}
    for p in _PLACES:
        city, code, country, tz = p[0], p[1], p[2], p[3]
        slug = slug_of(city)
        words = [city] + _ALSO.get(slug, [])
        state = _state_name(code)
        if state and state != city:
            words.append(state)
        others = sorted((q for q in _PLACES if q is not p), key=lambda q: _km(p, q))
        same = [q[0] for q in others if q[1] == code]
        near = [q[0] for q in others if q[1] != code and _km(p, q) <= NEAR_KM][:NEAR_MAX]
        for w in same + near + _FEEDS.get(tz, []):
            if w not in words:
                words.append(w)
        # How near each word is: 4 the city, 3 its province or state, 2 a
        # nearby city or the time zone feed; a higher second number is nearer.
        # Each with the country and state it is in (None: a time zone feed,
        # any country), so "US: NBC (KMOL) VICTORIA" (Texas) is not Victoria
        # BC and "PORTLAND MAINE" not Portland, Oregon.
        where = {q[0]: (q[2], _state_name(q[1])) for q in _PLACES}
        tiers = {}
        for i, w in enumerate(words):
            if w in tiers:
                continue
            c, st = where.get(w, (country, state))
            if w in _FEEDS.get(tz, []):
                c, st = None, None
            if i == 0 or w in _ALSO.get(slug, []):
                tiers[w] = (4, 0, country, state)
            elif w == state or w in same:
                tiers[w] = (3, -i, c, st)
            else:
                tiers[w] = (2, -i, c, st)
        region = _PROVINCES.get(code) or (state.title() if state else code)
        if code == "DC":
            region = "District of Columbia"
        label_city = "Washington" if city == "WASHINGTON DC" else title(city)
        out[slug] = {
            "slug": slug, "city": label_city, "region": region, "country": country,
            "country_name": _COUNTRY_NAMES.get(country, country), "time_zone": tz,
            "label": f"{label_city}, {code}", "words": words, "tiers": tiers,
        }
    return out


def title(city: str) -> str:
    """A place word as people read it ("SAULT STE MARIE" -> "Sault Ste Marie")."""
    small = {"STE": "Ste", "ST": "St."}
    return " ".join(small.get(w, w.capitalize()) for w in city.split()).replace("St. Johns", "St. John's")


PLACES = _build()


def get(slug: Optional[str]) -> Optional[dict]:
    return PLACES.get((slug or "").strip().lower())


def _nearest_to_region(db: Session) -> Optional[str]:
    """The place the household's region setting / time zone names first."""
    try:
        from services.livetv_catalog import _region
        country, words = _region(db)
    except Exception:
        return None
    for w in words:
        for slug, p in PLACES.items():
            if p["words"][0] == w and (not country or p["country"] == country):
                return slug
    return None


def household(db: Session) -> Optional[str]:
    """The household's place: the first admin's, else the setting
    `livetv_location`, else the one the region / time zone names (None when
    that is none of the places: a server outside North America)."""
    admin = (db.query(TentacleUser).filter(TentacleUser.is_admin == True)  # noqa: E712
             .filter(TentacleUser.livetv_location.isnot(None)).order_by(TentacleUser.id).first())
    if admin is not None and get(admin.livetv_location):
        return admin.livetv_location
    saved = (get_setting(db, "livetv_location", "") or "").strip().lower()
    if get(saved):
        return saved
    return _nearest_to_region(db)


_UNSET = object()


def of_person(db: Session, user: Optional[TentacleUser], home=_UNSET) -> Optional[str]:
    """Someone's place (the household's when none is set)."""
    if user is not None and get(user.livetv_location):
        return user.livetv_location
    return household(db) if home is _UNSET else home


def by_user(db: Session) -> dict:
    """Normalized Jellyfin user id -> place, for everyone Tentacle knows."""
    home = household(db)
    return {(u.jellyfin_user_id or "").replace("-", "").lower(): of_person(db, u, home)
            for u in db.query(TentacleUser).all()}


def in_use(db: Session) -> set:
    """The places people watch from, the household's included (None: the
    household has no place of the list)."""
    return set(by_user(db).values()) | {household(db)}


def not_local_tag(slug: str) -> str:
    return NOT_LOCAL_PREFIX + slug


def choices(db: Session) -> list:
    """The places to choose from: those the provider's channel names mention,
    and any someone has (the household's too), sorted by country then name."""
    from models.database import LiveChannel
    from services import livetv_names
    found = set()
    for (name,) in db.query(LiveChannel.name).all():
        found |= livetv_names.region_words(name or "")
    keep = in_use(db)
    out = [p for slug, p in PLACES.items() if slug in keep or p["words"][0] in found]
    out.sort(key=lambda p: (0 if p["country"] == "CA" else 1, p["label"]))
    return [{k: p[k] for k in ("slug", "label", "city", "region", "country", "country_name")} for p in out]


def public(slug: Optional[str]) -> Optional[dict]:
    p = get(slug)
    if p is None:
        return None
    return {k: p[k] for k in ("slug", "label", "city", "region", "country", "country_name")}
