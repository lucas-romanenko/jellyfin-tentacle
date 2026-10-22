"""Which network a Live TV channel is, from its provider name.

A provider lists the same network many times: one per city ("US: NBC (KNBC)
LOS ANGELES", "TV: A3 NEW YORK 04 WNBC NBC NEW YORK"), one per quality ("CA EN:
AMC 4K", "TV: AMC ᴿᴬᵂ", "AMC HD ⁽ᴮᴷ⁾") and one per time zone feed ("CARTOON
NETWORK EAST HD"). `fold_key()` reduces a name to its network so those fold
into one pool entry (services/livetv_catalog.py).

Conservative on purpose: a name folds only when what is taken off is clearly
a country/language prefix, a quality tag, a call sign, a place or a time
zone feed. Words that make it another channel stay: "NBC Sports", "NBC
News", "NBC Golf" and "TSN 1" / "TSN 2" are all different networks. Sports
channels never lose their place ("Sportsnet Pacific", "Bally Sports
Ohio" carry different games), only their tags. Event channels (PPV slots)
never fold: their names change with every event.
"""
import functools
import re
import unicodedata
from typing import Optional

# ── Normalising ──────────────────────────────────────────────────────────────

# Superscript tags ("ᴴᴰ", "ᴿᴬᵂ", "⁶⁰ᶠᵖˢ", "⁽ᴮᴷ⁾", "ⱽᴵᴾ") are decoration: removed,
# never read as letters (NFKC would turn "⁽ᴮᴷ⁾" into "(BK)").
_SUPERSCRIPTS = set("¹²³") | {chr(c) for c in range(0x2070, 0x20A0)}


def _plain(name: str) -> str:
    out = []
    for ch in name or "":
        cat = unicodedata.category(ch)
        if cat == "Lm" or ch in _SUPERSCRIPTS or cat == "So" or cat.startswith("C"):
            out.append(" ")
            continue
        out.append(ch)
    text = unicodedata.normalize("NFKC", "".join(out))
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return text


# A leading country / language / source tag: "US:", "CA EN:", "|US|", "US|",
# "[UK]", "TV:", "PRIME:", "AT&T:", "NBA:". Repeated ("US: PRIME: ...").
_PREFIX_RE = re.compile(
    r"^\s*(?:[|\[(]\s*)?(?:[A-Z]{2,3}(?:[ /-][A-Z]{2})?|USA|EXYU|LATAM|ARAB|PRIME|SLING|TUBI|AT&T)\s*[|\]):]\s*",
    re.IGNORECASE)
# Source words in front of the network: "US: SLING MLB NETWORK", "PRIME CA ...".
_SOURCE_WORDS = {"SLING", "TUBI", "PRIME"}

# Quality / format / copy tags.
_QUALITY = {
    "HD", "FHD", "UHD", "SD", "HQ", "LQ", "4K", "8K", "HDR", "HEVC", "H264", "H265", "RAW",
    "1080P", "1080I", "720P", "576I", "2160P", "3840P", "50FPS", "60FPS", "FPS", "BK", "BACKUP",
    "VIP", "A3", "MULTI", "AUDIO", "EN", "ENG", "FR", "HD+", "FULLHD", "4KHDR",
}
_COUNTRIES = {"US", "USA", "CA", "CAN", "CANADA", "UK"}
# Time zone / regional feed words.
_FEEDS = {"EAST", "WEST", "PACIFIC", "MOUNTAIN", "ATLANTIC", "EASTERN", "WESTERN",
          "NORTH", "SOUTH", "LOCAL", "REGIONAL", "FEED"}

# US states and Canadian provinces (names; the two-letter codes only count
# right after a place: "MONTGOMERY, AL", "TORONTO ON").
_STATES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE",
    "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY",
    "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI",
    "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO",
    "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA",
    "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
    "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING", "PUERTO RICO",
    "ONTARIO", "QUEBEC", "BRITISH COLUMBIA", "ALBERTA", "SASKATCHEWAN", "MANITOBA", "NOVA SCOTIA",
    "NEW BRUNSWICK", "NEWFOUNDLAND", "PRINCE EDWARD ISLAND", "PEI", "MARITIMES", "PRAIRIES",
    "NEW ENGLAND", "MID ATLANTIC", "MIDWEST", "SOUTHWEST", "SOUTHEAST", "NORTHWEST", "NORTHEAST",
    "SOCAL", "NORCAL", "BAY AREA", "TRI STATE", "TRI CITIES", "GULF COAST",
}
_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "ON", "QC", "BC", "AB", "SK", "MB", "NS", "NB", "NL", "PE",
}
# TV markets and Canadian cities (US DMAs and the cities Canadian stations
# are named after).
_CITIES = {
    "NEW YORK", "NYC", "LOS ANGELES", "LA", "CHICAGO", "PHILADELPHIA", "PHILLY", "DALLAS",
    "FORT WORTH", "DALLAS FORT WORTH", "DFW", "SAN FRANCISCO", "OAKLAND", "SAN JOSE", "ATLANTA",
    "HOUSTON", "WASHINGTON DC", "BOSTON", "PHOENIX", "SEATTLE", "TACOMA", "TAMPA", "ST PETERSBURG",
    "MINNEAPOLIS", "ST PAUL", "DETROIT", "DENVER", "MIAMI", "FORT LAUDERDALE", "ORLANDO",
    "CLEVELAND", "SACRAMENTO", "CHARLOTTE", "PORTLAND", "RALEIGH", "DURHAM", "ST LOUIS", "SAINT LOUIS",
    "INDIANAPOLIS", "PITTSBURGH", "NASHVILLE", "BALTIMORE", "SALT LAKE CITY", "SAN DIEGO",
    "HARTFORD", "NEW HAVEN", "KANSAS CITY", "COLUMBUS", "MILWAUKEE", "CINCINNATI", "GREENVILLE",
    "SPARTANBURG", "ASHEVILLE", "SAN ANTONIO", "WEST PALM BEACH", "PALM BEACH", "GRAND RAPIDS",
    "BIRMINGHAM", "LAS VEGAS", "NORFOLK", "HARRISBURG", "AUSTIN", "OKLAHOMA CITY", "ALBUQUERQUE",
    "JACKSONVILLE", "LOUISVILLE", "MEMPHIS", "NEW ORLEANS", "BUFFALO", "PROVIDENCE", "RICHMOND",
    "FRESNO", "LITTLE ROCK", "KNOXVILLE", "TULSA", "WICHITA", "HONOLULU", "DES MOINES", "OMAHA",
    "SPOKANE", "ALBANY", "SYRACUSE", "ROCHESTER", "TUCSON", "TOLEDO", "MADISON", "COLUMBIA",
    "CHATTANOOGA", "SAVANNAH", "CHARLESTON", "MOBILE", "PENSACOLA", "DAYTON", "HUNTSVILLE",
    "GREEN BAY", "APPLETON", "SHREVEPORT", "JACKSON", "BATON ROUGE", "LEXINGTON", "SPRINGFIELD",
    "ROANOKE", "LYNCHBURG", "WACO", "TEMPLE", "EL PASO", "BOISE", "FORT MYERS", "NAPLES",
    "SIOUX FALLS", "SIOUX CITY", "RAPID CITY", "YAKIMA", "PASCO", "RICHLAND", "KENNEWICK",
    "SALISBURY", "ROCKFORD", "AUGUSTA", "GAINESVILLE", "MONTGOMERY", "TALLAHASSEE", "LAFAYETTE",
    "LANSING", "FLINT", "SAGINAW", "TRAVERSE CITY", "CEDAR RAPIDS", "WATERLOO", "DAVENPORT",
    "PEORIA", "CHAMPAIGN", "EVANSVILLE", "FORT WAYNE", "SOUTH BEND", "YOUNGSTOWN", "WHEELING",
    "ERIE", "JOHNSTOWN", "ALTOONA", "SCRANTON", "WILKES BARRE", "WILMINGTON", "PORTSMOUTH",
    "BANGOR", "BURLINGTON", "PLATTSBURGH", "UTICA", "BINGHAMTON", "ELMIRA", "WATERTOWN",
    "AMARILLO", "LUBBOCK", "ODESSA", "MIDLAND", "CORPUS CHRISTI", "HARLINGEN", "BROWNSVILLE",
    "MCALLEN", "LAREDO", "BEAUMONT", "TYLER", "LONGVIEW", "ABILENE", "SAN ANGELO", "SHERMAN",
    "BAKERSFIELD", "SANTA BARBARA", "MONTEREY", "SALINAS", "CHICO", "REDDING", "EUREKA", "MEDFORD",
    "EUGENE", "BEND", "RENO", "LAS CRUCES", "COLORADO SPRINGS", "PUEBLO", "GRAND JUNCTION",
    "CHEYENNE", "CASPER", "BILLINGS", "MISSOULA", "GREAT FALLS", "BUTTE", "FARGO", "BISMARCK",
    "DULUTH", "MANKATO", "ROCHESTER MN", "LA CROSSE", "EAU CLAIRE", "WAUSAU", "MARQUETTE",
    "TOPEKA", "JOPLIN", "COLUMBIA MO", "JEFFERSON CITY", "PADUCAH", "CAPE GIRARDEAU",
    "BOWLING GREEN", "TERRE HAUTE", "LIMA", "ZANESVILLE", "PARKERSBURG", "CLARKSBURG",
    "BLUEFIELD", "TRI CITIES", "JOHNSON CITY", "GREENSBORO", "WINSTON SALEM", "HIGH POINT",
    "GREENVILLE NEW BERN", "NEW BERN", "WILMINGTON NC", "MYRTLE BEACH", "FLORENCE", "MACON",
    "ALBANY GA", "COLUMBUS GA", "DOTHAN", "PANAMA CITY", "MERIDIAN", "HATTIESBURG", "LAUREL",
    "TUPELO", "GREENWOOD", "MONROE", "ALEXANDRIA", "LAKE CHARLES", "JONESBORO", "FORT SMITH",
    "FAYETTEVILLE", "OTTUMWA", "QUINCY", "HANNIBAL", "ST JOSEPH", "LINCOLN", "KEARNEY",
    "NORTH PLATTE", "SCOTTSBLUFF", "IDAHO FALLS", "POCATELLO", "TWIN FALLS", "ANCHORAGE",
    "FAIRBANKS", "JUNEAU", "TRI-CITIES", "HAMPTON", "NEWPORT NEWS", "CHARLOTTESVILLE",
    "HARRISONBURG", "MIAMI-FORT LAUDERDALE", "TAMPA BAY", "LONG BEACH", "SAN BERNARDINO",
    "PALM SPRINGS", "YUMA", "EL CENTRO", "FLAGSTAFF", "SANTA FE", "SAN JUAN", "TEXARKANA",
    # Canada
    "TORONTO", "MONTREAL", "VANCOUVER", "CALGARY", "EDMONTON", "OTTAWA", "WINNIPEG", "HALIFAX",
    "REGINA", "SASKATOON", "VICTORIA", "KITCHENER", "WATERLOO", "LONDON", "HAMILTON", "WINDSOR",
    "SUDBURY", "THUNDER BAY", "SAULT STE MARIE", "TIMMINS", "NORTH BAY", "KINGSTON", "PETERBOROUGH",
    "BARRIE", "OSHAWA", "QUEBEC CITY", "SHERBROOKE", "TROIS RIVIERES", "SAGUENAY", "CHICOUTIMI",
    "RIMOUSKI", "HULL", "GATINEAU", "MONCTON", "SAINT JOHN", "FREDERICTON", "CHARLOTTETOWN",
    "ST JOHNS", "SYDNEY", "LETHBRIDGE", "RED DEER", "KELOWNA", "KAMLOOPS", "PRINCE GEORGE",
    "BRANDON", "YORKTON", "PRINCE ALBERT", "LLOYDMINSTER", "MEDICINE HAT", "DAWSON CREEK",
    "OKANAGAN", "ATLANTIC CANADA", "GTA", "NIAGARA", "SAULT",
}

# Places as word tuples, longest first.
_PLACES = sorted({tuple(p.split()) for p in (_CITIES | _STATES)}, key=len, reverse=True)

# Broadcast networks whose local stations carry the network's name plus a
# call sign, a number and a city.
NETWORKS = {
    "ABC", "NBC", "CBS", "FOX", "CW", "PBS", "MNT", "MYNETWORK", "MY", "ION", "TELEMUNDO",
    "UNIVISION", "UNIMAS", "CTV", "CTV2", "GLOBAL", "CBC", "CITY", "CITYTV", "TVA", "RADIO",
    "NOOVO", "OMNI", "CHCH", "CPAC", "INDEPENDENT", "IND", "MeTV".upper(), "ANTENNA", "ESTRELLA",
}

# The networks a local station's name is reduced to when it has a call sign.
BROADCAST = {"ABC", "NBC", "CBS", "FOX", "CW", "PBS", "MNT", "MYNETWORK", "ION", "TELEMUNDO", "UNIVISION",
             "UNIMAS", "CTV", "CTV2", "GLOBAL", "CBC", "CITY", "TVA", "NOOVO", "OMNI", "METV", "ESTRELLA"}
# Words that make a station's channel something else than the network.
_CONTENT_WORDS = {"NEWS", "SPORTS", "SPORT", "WEATHER", "KIDS", "PLUS", "NOW", "LIVE", "BUSINESS", "MOVIES",
                  "DEPORTES", "LATINO", "COMEDY", "CLASSIC", "GOLF", "BRAVO", "SYFY", "E", "OXYGEN", "CNBC"}

# 4-letter (and suffixed) US call signs: K/W + 3 letters, e.g. KNBC, WCBS,
# WIFR-LD. Words that look like one but are words.
_CALL_RE = re.compile(r"^[KW][A-Z]{2,3}(?:-(?:LD|TV|DT|CD|CA|HD))?$")
_CALL_WORDS = {
    "WEST", "WILD", "KIDS", "WORK", "WORD", "WIRE", "WINE", "WIND", "WAVE", "WALL", "WAYS", "WEEK",
    "WELL", "WOOD", "WISE", "WIFE", "WAKE", "WALK", "WANT", "WARM", "WASH", "WATT", "WEAR", "WIDE",
    "WILL", "WISH", "WOLF", "WOOL", "WRAP", "KING", "KEEP", "KICK", "KILL", "KIND", "KISS", "KITE",
    "KNOW", "KNOT", "WWE", "WGN", "WAR", "WAX", "WOW", "WEB", "WIN", "WHY", "KID", "KEY", "KIX",
    "WHO", "WAY", "WET", "WIG", "WOK", "WWF", "KOL", "WNBA", "WPT", "WSL", "WTA", "WRC", "WEC",
    "WBC", "WBA", "WBO", "WHL", "KHL", "WPLL", "WNY",
}
# Canadian call signs (CFTO, CBLT, CKCO, CHEK...) count only in brackets.
_CA_CALL_RE = re.compile(r"^C[BFHIJK][A-Z]{2}(?:-(?:DT|TV|HD))?$")

# Words that make a channel a sports channel: its place stays in the key.
_SPORTS_RE = re.compile(r"SPORT|\bTSN\d*\b|\bRSN\b|\bNESN\b|\bMSG\b|\bROOT\b|\bSNY\b|\bMASN\b|"
                        r"\bMARQUEE\b|\bYES\b|\bALTITUDE\b|\bSPECTRUM\b|\bNBCS\b|\bFANDUEL\b|\bRDS\b")

_SYNONYMS = {"JR": "JUNIOR", "CHNL": "CHANNEL", "TV.": "TV", "&": "AND", "+": "PLUS",
             "ENTERTAINMENT": "ENTERTAINMENT", "NETWORKS": "NETWORK", "INTL": "INTERNATIONAL",
             "SPORTSNET": "SPORTSNET", "CITYTV": "CITY", "MYNETWORKTV": "MYNETWORK", "MYTV": "MYNETWORK"}
# A trailing generic word that names nothing: "FOX NEWS CHANNEL" = "FOX NEWS".
_TRAILING = {"CHANNEL", "TV CHANNEL"}

_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9'.+&]*")


def _tokens(text: str) -> list:
    text = text.upper().replace("'", "").replace("`", "")
    out = []
    for t in _TOKEN_RE.findall(text):
        t = t.rstrip(".").replace(".", "")
        if t.endswith("+") and len(t) > 1:
            out += [t[:-1], "PLUS"]
            continue
        if "&" in t:
            parts = [p for p in t.split("&") if p]
            for i, p in enumerate(parts):
                if i:
                    out.append("AND")
                out.append(p)
            continue
        if not t:
            continue
        t = _SYNONYMS.get(t, t)
        # "ABC7" is ABC's channel 7; "CTV2" is a network of its own.
        m = re.fullmatch(r"([A-Z]{2,4})(\d{1,2})", t)
        if m and m.group(1) in NETWORKS and t not in NETWORKS:
            out += [m.group(1), m.group(2)]
            continue
        if t == "TV" and out and out[-1] == "CITY":
            continue   # "CITY TV" = Citytv
        if t == "ET" and out and out[-1] == "SPORTSN":
            out[-1] = "SPORTSNET"   # a provider's "SPORTSN ET"
            continue
        out.append(t)
    return out


def _is_call_sign(tok: str, bracketed: bool) -> bool:
    if tok in _CALL_WORDS:
        return False
    if _CALL_RE.match(tok) and (len(tok.split("-")[0]) == 4 or bracketed):
        return True
    return bracketed and bool(_CA_CALL_RE.match(tok))


def _strip_places(toks: list) -> tuple:
    """Removes place names (longest match first) and a state code right after
    one. Returns (tokens left, removed anything)."""
    out, i, removed = [], 0, False
    while i < len(toks):
        hit = None
        for p in _PLACES:
            if tuple(toks[i:i + len(p)]) == p:
                hit = p
                break
        if hit:
            i += len(hit)
            removed = True
            if i < len(toks) and toks[i] in _STATE_CODES:
                i += 1
            continue
        out.append(toks[i])
        i += 1
    return out, removed


def is_separator(name: str) -> bool:
    from routers.livetv import _is_separator
    return _is_separator(name)


def _strip_prefixes(text: str) -> str:
    for _ in range(3):
        m = _PREFIX_RE.match(text)
        if not m or not text[m.end():].strip():
            break
        text = text[m.end():]
    return text


_BRACKET_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]")


def analyse(name: str) -> dict:
    """The parts of a channel name: {"key", "words", "removed": set of what
    was taken off ("place", "call", "quality", "feed", "country")}."""
    text = _strip_prefixes(_plain(name).strip())
    removed = set()
    sports = bool(_SPORTS_RE.search(text.upper()))

    # Bracketed parts: "(KNBC)", "[NEW YORK]", "(A)", "(D11)", "(EAST)".
    kept_brackets = []

    def bracket(m):
        inner = _tokens(m.group(1))
        if not inner:
            return " "
        rest, placed = (_strip_places(inner) if not sports else (inner, False))
        rest2 = []
        for t in rest:
            if _is_call_sign(t, True):
                removed.add("call")
            elif t in _QUALITY or t in _COUNTRIES or (t in _FEEDS and not sports):
                removed.add("quality" if t in _QUALITY else "country" if t in _COUNTRIES else "feed")
            elif len(t) <= 2 or re.fullmatch(r"[A-Z]?\d+[A-Z]?", t):
                removed.add("quality")
            else:
                rest2.append(t)
        if placed:
            removed.add("place")
        if rest2:
            kept_brackets.extend(rest2)
            if sports:
                return " " + " ".join(rest2) + " "
            removed.add("bracket")
        return " "

    text = _BRACKET_RE.sub(bracket, text)
    toks = _tokens(text)
    # Source words in front: "SLING MLB NETWORK", "PRIME CA MLB NETWORK".
    while len(toks) > 1 and toks[0] in _SOURCE_WORDS:
        toks = toks[1:]
        removed.add("source")

    out = []
    for i, t in enumerate(toks):
        if t in _QUALITY or (t.endswith("FPS") and t[:-3].isdigit()):
            removed.add("quality")
            continue
        if t in _COUNTRIES and len(toks) > 1 and (len(t) == 2 or i == len(toks) - 1):
            removed.add("country")
            continue
        if _is_call_sign(t, False):
            removed.add("call")
            continue
        out.append(t)
    toks = out
    if not sports:
        before = toks
        toks, placed = _strip_places(toks)
        if placed and toks:
            removed.add("place")
        elif placed:
            toks = before
        feeds = [t for t in toks if t in _FEEDS]
        if feeds and len(feeds) < len(toks):
            toks = [t for t in toks if t not in _FEEDS]
            removed.add("feed")
    # A local station's channel number goes with its call sign / city:
    # "ABC 13 (KTRK) HOUSTON" -> ABC. Never otherwise: "TSN 2" is not "TSN".
    if removed & {"call", "place"} and toks and toks[0] in ("ST", "STATION"):
        toks = toks[1:]
    toks = [t for i, t in enumerate(toks) if not (t == "ST" and "place" in removed and i == len(toks) - 1)]
    if ("call" in removed or ("place" in removed and toks and toks[0] in NETWORKS)) and not sports:
        nums = [t for t in toks if t.isdigit() and len(t) <= 2]
        if nums and len(nums) < len(toks):
            toks = [t for t in toks if not (t.isdigit() and len(t) <= 2)]
    # A local station: a call sign and a network name; whatever else is
    # there is the town ("NBC 3 (WSAZ) HUNTINGTON" -> NBC), unless it says
    # what kind of channel it is ("ABC TAMPA NEWS (WFTS)" stays ABC News).
    nets = [t for t in toks if t in BROADCAST]
    if "call" in removed and nets and not sports and not any(t in _CONTENT_WORDS for t in toks):
        toks = [nets[-1]]
    if len(toks) > 1 and toks[0] == "THE":
        toks = toks[1:]
    # A city's newscast is not the national news channel: "CTV NEWS MONTREAL",
    # "ABC TAMPA NEWS (WFTS)" fold among themselves only, as "CTV News (local)".
    if not sports and "NEWS" in toks and removed & {"place", "call"}:
        toks = toks + ["LOCAL"]
    display = list(toks)
    while len(toks) > 1 and toks[-1] in ("CHANNEL",):
        toks = toks[:-1]
    if toks in (["IND"], ["INDEPENDENT"], ["MY"]):
        # Independent stations have nothing in common but the word.
        return {"key": "", "words": toks, "display": display, "removed": removed, "sports": sports}
    return {"key": "".join(toks).lower(), "words": toks, "display": display, "removed": removed, "sports": sports}


def fold_key(name: str) -> str:
    """The network a channel name reduces to ("" when nothing is left)."""
    return analyse(name)["key"]


# ── A readable network name ──────────────────────────────────────────────────

_UPPER_WORDS = {
    "HGTV", "ESPN", "ESPN2", "ESPNU", "CNBC", "MSNBC", "CNN", "HLN", "TSN", "WWE", "NFL", "NBA", "MLB",
    "NHL", "CBC", "CTV", "TVA", "AMC", "TLC", "BET", "CMT", "MTV", "VH1", "TNT", "TBS", "USA", "FX",
    "FXX", "FXM", "IFC", "OWN", "PBS", "ABC", "NBC", "CBS", "BBC", "HBO", "AXS", "TV", "UFC", "ION",
    "APTN", "BNN", "CPAC", "RDS", "MLS", "NCAA", "SEC", "ACC", "AMI", "TCM", "MAV", "ID", "HSN", "QVC",
    "EWTN", "TBN", "MGM", "PPV", "DAZN", "CW", "MNT", "CNBC", "TMC", "GSN", "CHCH", "OLN", "YTV",
    "CP24", "CMT", "WGN", "NESN", "SNY", "MSG", "MASN", "BTN", "TUDN", "FS1", "FS2", "DIY", "A&E",
}


_SMALL_WORDS = {"AT", "OF", "THE", "AND", "IN", "ON", "FOR", "WITH", "A", "AN", "TO", "DE", "LA", "DU", "ET",
                "LE", "LES", "DES", "VS", "BY"}
_UPPER_WORDS |= {"CTV2", "TVO", "CPAC", "CNN", "HBO", "NBA TV", "SN", "SN1", "TSN1", "TSN2", "TSN3", "TSN4", "TSN5",
                 "TVA", "RDS", "RDS2", "BNN", "CHCH", "OMNI", "APTN", "TV5", "TFO", "ICI", "AMC+", "E!", "BBC",
                 "HGTV", "MTV2", "NHL", "WNBA", "PGA", "ATP", "WTA", "UEFA", "FIFA", "MSNBC", "CMT", "OLN", "BTN",
                 "NBCSN", "CNBC", "FOX", "ABC", "TBS", "TNT", "TCM", "USA", "GSN", "INSP", "CBC", "CBS", "NBC", "PBS"}


def _case_word(w: str, first: bool) -> str:
    """One word of a name in capitals, readable: acronyms stay, words get a capital."""
    core = re.sub(r"[^A-Z0-9&+!]", "", w.upper())
    if not core:
        return w
    if core in _UPPER_WORDS or core.rstrip("+!") in _UPPER_WORDS or any(c.isdigit() for c in core):
        return w.upper()
    if core in _SMALL_WORDS and not first:
        return w.lower()
    if core in ("ST", "MT", "FT"):
        return w[:1].upper() + w[1:].lower()
    if (_CALL_RE.match(core) and core not in _CALL_WORDS) or not re.search(r"[AEIOUY]", core):
        return w.upper()
    if len(core) <= 2 and core not in ("TV",) and not first:
        return w.upper()
    return re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0).capitalize(), w.lower())


def tidy_case(text: str) -> str:
    """A name in capitals ("SPORTSNET ONTARIO") read as one ("Sportsnet
    Ontario"); a name the provider wrote in mixed case is left alone."""
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) < 0.8 * len(letters):
        return text
    return " ".join(_case_word(w, i == 0) for i, w in enumerate(text.split()))


def display_name(words: list) -> str:
    out = []
    for i, w in enumerate(words):
        if w == "LOCAL" and out and out[-1].upper() == "NEWS":
            out.append("(local)")
        elif w == "AND":
            out.append("&")
        elif w == "PLUS":
            if out:
                out[-1] += "+"
            else:
                out.append("+")
        else:
            out.append(_case_word(w, i == 0))
    return " ".join(out)


_TAG_ONLY = re.compile(r"^(?:[A-Z]|[A-Z]?\d+[A-Z]?|PC|HD|SD|FHD|UHD|4K|8K|BK|RAW|HEVC|CA|US|USA|UK|EN|FR|R|D|H|A|S|Y|GT|"
                       r"BACKUP|MULTI|LIVE|VIP)$")


def clean_name(name: str) -> str:
    """The name people see: "CA EN: SPORTSNET ONTARIO HD" -> "Sportsnet Ontario",
    "US: NBC (KNBC) LOS ANGELES" -> "NBC Los Angeles", "TV: AMC ᴿᴬᵂ" -> "AMC".
    Prefixes, quality tags and tag brackets go; the place stays."""
    if not name:
        return name or ""
    text = _strip_prefixes(_plain(name).strip())
    text = re.sub(r"\bSPORTSN ET\b", "SPORTSNET", text.replace("_", " "))

    def bracket(m):
        inner = m.group(1).strip()
        toks = _tokens(inner)
        if not toks or all(_TAG_ONLY.match(t) or t in _QUALITY or _is_call_sign(t, True) for t in toks):
            return " "
        rest, placed = _strip_places(toks)
        if placed and not rest:
            return " " + inner + " "      # "[BUFFALO]" -> Buffalo
        return " (" + inner + ") "

    text = _BRACKET_RE.sub(bracket, text)
    words = text.split()
    while len(words) > 1 and words[0].upper().rstrip(":") in _SOURCE_WORDS:
        words = words[1:]
    kept = []
    for w in words:
        core = re.sub(r"[^A-Z0-9+]", "", w.upper())
        if core and (core in _QUALITY or (core.endswith("FPS") and core[:-3].isdigit())) and core not in ("EN", "FR"):
            continue
        if core in ("EN", "FR") and kept:
            continue
        kept.append(w)
    text = " ".join(kept).strip(" -:|.") or _plain(name).strip()
    return tidy_case(re.sub(r"\s+", " ", text))


# A US/Canadian state or province by its code, after a place ("BOSTON, MA").
STATE_NAMES = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS", "CA": "CALIFORNIA", "CO": "COLORADO",
    "CT": "CONNECTICUT", "DE": "DELAWARE", "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO",
    "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY", "LA": "LOUISIANA",
    "ME": "MAINE", "MD": "MARYLAND", "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA",
    "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK", "NC": "NORTH CAROLINA",
    "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON", "PA": "PENNSYLVANIA",
    "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA", "TN": "TENNESSEE", "TX": "TEXAS",
    "UT": "UTAH", "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN", "WY": "WYOMING", "ON": "ONTARIO", "QC": "QUEBEC", "BC": "BRITISH COLUMBIA",
    "AB": "ALBERTA", "SK": "SASKATCHEWAN", "MB": "MANITOBA", "NS": "NOVA SCOTIA", "NB": "NEW BRUNSWICK",
    "NL": "NEWFOUNDLAND", "PE": "PRINCE EDWARD ISLAND",
}
# "TV: ATLANTA, GA WABE PBS 30 A3 HD ATLANTA": a provider's local station, its
# market first. The place after "A3 HD" is often another city altogether
# ("TV: SPRINGFIELD, MA WGBY PBS 57 A3 HD DETROIT"): only the market counts.
_MARKET_RE = re.compile(r"^\s*TV\s*:\s*([A-Z][A-Z .'-]*?)\s*,\s*([A-Z]{2})\b")
_CITY_PLACES = {tuple(p.split()) for p in _CITIES}
# The US stations Canadians watch over the border, by call sign: a provider
# often names one by its call sign alone ("US: PBS 17 (WNED)"), so the call
# sign tells its city. Buffalo for Toronto, Detroit for Windsor, Seattle for
# Vancouver (KING is a word: "US: NBC 5 (KING) SEATTLE" names its city).
_CALL_CITIES = {
    "WGRZ": "BUFFALO", "WIVB": "BUFFALO", "WKBW": "BUFFALO", "WUTV": "BUFFALO", "WNED": "BUFFALO",
    "WNLO": "BUFFALO", "WNYO": "BUFFALO",
    "WDIV": "DETROIT", "WWJ": "DETROIT", "WXYZ": "DETROIT", "WJBK": "DETROIT", "WTVS": "DETROIT",
    "WKBD": "DETROIT", "WMYD": "DETROIT",
    "KIRO": "SEATTLE", "KOMO": "SEATTLE", "KCPQ": "SEATTLE", "KCTS": "SEATTLE", "KSTW": "SEATTLE",
    "KZJO": "SEATTLE",
}


@functools.lru_cache(maxsize=50000)
def region_words(name: str) -> frozenset:
    """Place / feed / country words in a channel name, for choosing the
    variant nearest a place ("TORONTO", "EAST", "CA"); a state or province
    code right after a city adds its name ("BOSTON, MA": "MASSACHUSETTS"); a
    border station's call sign its city ("(WNED)": "BUFFALO")."""
    text = _plain(name).upper()
    out = set()
    m = re.match(r"^\s*[|\[(]?\s*([A-Z]{2,3})(?:[ /-]([A-Z]{2}))?\s*[|\]):]", text)
    if m:
        out.add(m.group(1))
    out.update(_CALL_CITIES[t] for t in _tokens(text) if t in _CALL_CITIES)
    market = _MARKET_RE.match(text)
    if market:
        text = f"{market.group(1)} {market.group(2)}"
    toks = _tokens(text)
    in_place = set()   # "WEST PALM BEACH" is a city, not a West feed
    for i in range(len(toks)):
        for p in _PLACES:
            if tuple(toks[i:i + len(p)]) == p:
                out.add(" ".join(p))
                in_place.update(range(i, i + len(p)))
                after = toks[i + len(p)] if i + len(p) < len(toks) else None
                if p in _CITY_PLACES and after in STATE_NAMES:
                    out.add(STATE_NAMES[after])
    if not market:
        for i, t in enumerate(toks):
            # "WEST DES MOINES" is a city too
            if (t in _FEEDS and i not in in_place and i + 1 not in in_place) or t in _COUNTRIES:
                out.add(t)
    return frozenset(out)


# ── Country and language of a feed ──────────────────────────────────────────

# ISO 3166 country codes as providers put them in front of names and groups
# ("GR| DOCUMENTARY", "BE: NICK JR.", "CA EN: TLC"). Left out: "TV" (Tuvalu,
# but "TV:" is a generic tag) and codes that are tags or words more often
# than countries.
_ISO_COUNTRIES = set(
    "AD AE AF AG AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ CA CD CF CG "
    "CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET FI FJ FR GA GB GD GE GH GM GN "
    "GQ GR GT GW GY HK HN HR HT HU ID IE IL IN IQ IR IS IT JM JO JP KE KG KH KM KN KP KR KW KZ LA LB LC "
    "LI LK LR LS LT LU LV LY MA MC MD ME MG MK ML MM MN MO MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP "
    "NZ OM PA PE PG PH PK PL PR PT PY QA RO RS RU RW SA SC SD SE SG SI SK SL SM SN SO SR SS ST SV SY SZ "
    "TD TG TH TJ TL TM TN TR TT TW TZ UA UG US UY UZ VA VE VN YE ZA ZM ZW".split())
_COUNTRY_ALIASES = {"UK": "GB", "USA": "US", "CAN": "CA", "EXYU": "RS"}
_LANGUAGES = {"EN": "en", "ENG": "en", "FR": "fr", "FRA": "fr", "ES": "es", "ESP": "es", "DE": "de",
              "IT": "it", "PT": "pt", "NL": "nl", "AR": "ar"}


def _country_code(code: Optional[str]) -> Optional[str]:
    code = (code or "").upper()
    code = _COUNTRY_ALIASES.get(code, code)
    return code if code in _ISO_COUNTRIES else None


_LEAD_RE = re.compile(r"^\s*[|\[(]?\s*([A-Z]{2,4})(?:[ /-]([A-Z]{2,3}))?\s*[|\]):]")


def feed_country(name: str, group: Optional[str] = None) -> Optional[str]:
    """The country a channel's feed is from, by the code in front of its name
    ("BE: NICK JR." -> "BE"), else in front of its group ("US| TV ᴿᴬᵂ" for
    "TV: TLC ᴿᴬᵂ"); None when neither names one. "UK" is "GB"."""
    for text in (name, group):
        m = _LEAD_RE.match(_plain(text or "").upper())
        if m:
            c = _country_code(m.group(1))
            if c:
                return c
    return None


def feed_language(name: str, group: Optional[str] = None) -> Optional[str]:
    """The language a channel names for itself ("CA EN: TLC", "CA FR: ...",
    a group "CA| GENERAL EN"), as a two-letter code; None when it names none."""
    m = _LEAD_RE.match(_plain(name or "").upper())
    if m and m.group(2) in _LANGUAGES:
        return _LANGUAGES[m.group(2)]
    gtext = _plain(group or "").upper()
    m = _LEAD_RE.match(gtext)
    if m and m.group(2) in _LANGUAGES:
        return _LANGUAGES[m.group(2)]
    toks = _tokens(gtext)
    if m and toks and toks[-1] in ("EN", "ENG", "FR", "FRA"):
        return _LANGUAGES[toks[-1]]
    return None


_GUIDE_TLD_RE = re.compile(r"\.([A-Za-z]{2})(?:@[^.]*)?$")


def guide_country(guide_id: Optional[str]) -> Optional[str]:
    """The country an XMLTV channel id is for, by its ending ("TLC.rs" ->
    "RS", "tlc.ca" -> "CA", "CNBC.uk" -> "GB"); None without one ("TS")."""
    m = _GUIDE_TLD_RE.search((guide_id or "").strip())
    return _country_code(m.group(1)) if m else None


def feed_words(name: str) -> set:
    """The time zone feed words in a name ("WEST", "PACIFIC")."""
    return {w for w in region_words(name) if w in _FEEDS}
