"""What a provider's channel group is, read from its name: the country or
language it is for, a readable title, and the tags around it.

Providers name groups like "ES| DAZN BALONCESTO ᴿᴬᵂ", "NL| TELE2 VERMAAK ᴿᴬᵂ
ᴳᴼᴸᴰ", "CA EN: SPORTS", "GR| ΑΘΛΗΤΙΚΑ/SPORTS ⱽᴵᴾ" or "AR| MOROCCO ᴿᴬᵂ ▶ المغرب".
`describe()` turns that into {"code": "ES", "title": "DAZN Baloncesto",
"tags": ["Raw"], ...} so the admin's review of new groups
(services/provider_new.py) can say "Spain" with a flag, fold groups by
country and put the household's own countries first.

The prefix is a provider's code, not always ISO: "AR" is Arabic, "LA" Latin
America, and one provider writes "SR| SERBIA" and "SU| SURINAME".
`CodeBook` reads a provider's whole group list: a code whose groups name one
country ("BH| BOSNIA ...") is that country; a code whose groups name several
("LA| ARGENTINA", "LA| CHILE") is a region.
"""
import re
import unicodedata
from collections import defaultdict
from typing import Iterable, Optional

from services.livetv_names import _SUPERSCRIPTS, tidy_case

# ISO 3166 code -> English name.
COUNTRIES = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AL": "Albania", "AM": "Armenia",
    "AO": "Angola", "AR": "Argentina", "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan",
    "BA": "Bosnia", "BD": "Bangladesh", "BE": "Belgium", "BF": "Burkina Faso", "BG": "Bulgaria",
    "BH": "Bahrain", "BJ": "Benin", "BO": "Bolivia", "BR": "Brazil", "BY": "Belarus", "CA": "Canada",
    "CD": "Congo (DRC)", "CG": "Congo", "CH": "Switzerland", "CI": "Côte d’Ivoire", "CL": "Chile",
    "CM": "Cameroon", "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba",
    "CV": "Cape Verde", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark",
    "DO": "Dominican Republic", "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt",
    "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GE": "Georgia", "GH": "Ghana", "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong",
    "HN": "Honduras", "HR": "Croatia", "HT": "Haiti", "HU": "Hungary", "ID": "Indonesia",
    "IE": "Ireland", "IL": "Israel", "IN": "India", "IQ": "Iraq", "IR": "Iran", "IS": "Iceland",
    "IT": "Italy", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya", "KH": "Cambodia",
    "KR": "South Korea", "KW": "Kuwait", "KZ": "Kazakhstan", "LB": "Lebanon", "LK": "Sri Lanka",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya", "MA": "Morocco",
    "MD": "Moldova", "ME": "Montenegro", "MK": "North Macedonia", "ML": "Mali", "MT": "Malta",
    "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands",
    "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama", "PE": "Peru",
    "PH": "Philippines", "PK": "Pakistan", "PL": "Poland", "PR": "Puerto Rico", "PS": "Palestine",
    "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar", "RO": "Romania", "RS": "Serbia", "RU": "Russia",
    "SA": "Saudi Arabia", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia",
    "SK": "Slovakia", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname", "SV": "El Salvador",
    "SY": "Syria", "TH": "Thailand", "TN": "Tunisia", "TR": "Turkey", "TT": "Trinidad and Tobago",
    "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VE": "Venezuela", "VN": "Vietnam", "YE": "Yemen",
    "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}
# Other names a group title uses for a country.
_COUNTRY_WORDS = {
    "USA": "US", "AMERICA": "US", "UK": "GB", "ENGLAND": "GB", "BRITAIN": "GB", "SCOTLAND": "GB",
    "BOSNIA": "BA", "HOLLAND": "NL", "KAZACHSTAN": "KZ", "VENEZULA": "VE", "R.DOMINICANA": "DO",
    "DOMINICANA": "DO", "ALGERIE": "DZ", "MAROC": "MA", "TURKIYE": "TR", "CZECH": "CZ", "KOREA": "KR",
    "IVORY COAST": "CI", "EMIRATES": "AE", "UAE": "AE", "KSA": "SA", "PERSIA": "IR", "BELGIQUE": "BE",
    "DEUTSCHLAND": "DE", "ESPANA": "ES", "ITALIA": "IT", "POLSKA": "PL", "SVERIGE": "SE", "NORGE": "NO",
    "DANMARK": "DK", "SUOMI": "FI", "CMA GORA": "ME", "CRNA GORA": "ME", "MACEDONIA": "MK",
}
_NAME_TO_CODE = {**{v.upper(): k for k, v in COUNTRIES.items()}, **_COUNTRY_WORDS}
_NAMES_LONGEST = sorted(_NAME_TO_CODE, key=len, reverse=True)

# Provider codes for a language or region (IPTV usage, not ISO).
REGIONS = {
    "AR": "Arabic", "ARAB": "Arabic", "ARABIC": "Arabic", "LA": "Latin America", "LATAM": "Latin America",
    "LATINO": "Latin America", "ASIA": "Asia", "AFR": "Africa", "AFRICA": "Africa", "EXYU": "Ex-Yugoslavia",
    "EX-YU": "Ex-Yugoslavia", "BALKAN": "Balkans", "KU": "Kurdish", "KURD": "Kurdish", "EU": "Europe",
    "INT": "International", "CARIB": "Caribbean", "CARIBBEAN": "Caribbean",
}
# Codes in front of a group that are a topic, a source or a quality, not a place.
_NOT_PLACES = {"4K", "8K", "UHD", "HD", "FHD", "VIP", "PPV", "VOD", "XXX", "ADULT", "MC", "RX", "TS", "WT",
               "MU", "24/7", "247", "RADIO", "MUSIC", "KIDS", "SPORT", "SPORTS", "NEWS"}
# Source words: "PRIME:", "SLING:" say where a channel comes from, not where.
_SOURCES = {"PRIME", "SLING", "TUBI", "PLUTO", "SAMSUNG", "ROKU", "STIRR", "XUMO"}
_SHORT = {"US": "US", "GB": "UK", "AE": "UAE"}
_SOURCE_COUNTRY = {"SLING": "US", "TUBI": "US", "STIRR": "US", "XUMO": "US"}

# Main language of a country or region: a household that speaks it gets that
# place's new groups among "Probably for you".
LANGUAGE_OF = {
    "US": "en", "CA": "en", "GB": "en", "IE": "en", "AU": "en", "NZ": "en", "JM": "en", "TT": "en",
    "FR": "fr", "BE": "fr", "LU": "fr", "SN": "fr", "CI": "fr", "HT": "fr", "ES": "es", "MX": "es",
    "CO": "es", "CL": "es", "PE": "es", "VE": "es", "EC": "es", "GT": "es", "CU": "es", "BO": "es",
    "DO": "es", "HN": "es", "PY": "es", "SV": "es", "NI": "es", "CR": "es", "PA": "es", "UY": "es",
    "PR": "es", "LA": "es", "DE": "de", "AT": "de", "CH": "de", "IT": "it", "PT": "pt", "BR": "pt",
    "AO": "pt", "CV": "pt", "NL": "nl", "SR": "nl", "SE": "sv", "NO": "no", "DK": "da", "FI": "fi",
    "PL": "pl", "GR": "el", "CY": "el", "TR": "tr", "RU": "ru", "UA": "uk", "IL": "he", "RO": "ro",
    "HU": "hu", "CZ": "cs", "SK": "sk", "HR": "hr", "RS": "sr", "BA": "bs", "ME": "sr", "AL": "sq",
    "BG": "bg", "IR": "fa", "AF": "fa", "IN": "hi", "PK": "ur", "JP": "ja", "KR": "ko", "CN": "zh",
    "TW": "zh", "HK": "zh", "VN": "vi", "TH": "th", "ID": "id", "PH": "en", "AR": "ar", "MA": "ar",
    "DZ": "ar", "TN": "ar", "EG": "ar", "SA": "ar", "AE": "ar", "QA": "ar", "KW": "ar", "BH": "ar",
    "IQ": "ar", "JO": "ar", "LB": "ar", "SY": "ar", "OM": "ar", "YE": "ar", "LY": "ar", "KU": "ku",
}
# A language written in a group's name ("CA| CINEMA FR", "CA EN:").
LANGUAGES = {"EN": "en", "ENG": "en", "FR": "fr", "FRA": "fr", "ES": "es", "ESP": "es", "DE": "de",
             "GER": "de", "IT": "it", "ITA": "it", "PT": "pt", "NL": "nl", "AR": "ar", "TR": "tr",
             "PL": "pl", "RU": "ru"}
LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian",
                  "pt": "Portuguese", "nl": "Dutch", "ar": "Arabic", "tr": "Turkish", "pl": "Polish",
                  "ru": "Russian"}

# Tags: what a provider writes around a name about its streams. Shown as
# small labels, never as part of the title.
_TAGS = {
    "RAW": "Raw", "GOLD": "Gold", "VIP": "VIP", "BK": "Backup", "(BK)": "Backup", "BACKUP": "Backup",
    "HD": "HD", "FHD": "Full HD", "UHD": "4K", "4K": "4K", "8K": "8K", "SD": "SD", "LQ": "Low quality",
    "HQ": "HQ", "HEVC": "HEVC", "60FPS": "60 fps", "50FPS": "50 fps", "FPS": "", "3840P": "", "2160P": "",
    "1080P": "", "720P": "", "CITY": "Local", "MULTI": "Multi audio", "DVB-T": "DVB-T", "SAT": "",
}
_EVENT_WORDS = {"PPV", "EVENT", "EVENTS"}
_CUT = "▶►▼▸"   # what follows is the name again in its own script


def _is_sup(ch: str) -> bool:
    return ch in _SUPERSCRIPTS or unicodedata.category(ch) == "Lm"


def _split_superscripts(name: str) -> tuple:
    """(the name with superscript runs taken out, the runs read as text)."""
    main, runs, cur = [], [], []
    for ch in name:
        if _is_sup(ch) or (cur and ch in " /"):
            if _is_sup(ch) or cur:
                cur.append(ch)
            main.append(" ")
            continue
        if cur:
            runs.append(unicodedata.normalize("NFKC", "".join(cur)))
            cur = []
        main.append(ch)
    if cur:
        runs.append(unicodedata.normalize("NFKC", "".join(cur)))
    words = [w.replace("ɪ", "i") for r in runs for w in re.split(r"[\s/]+", r) if w]
    return "".join(main), words


_PREFIX_RE = re.compile(r"^\s*[|\[(]?\s*([A-Z0-9][A-Z0-9/-]{0,6}?)(?:[ /]([A-Z]{2,3}))?\s*[|\]):]\s*", re.I)


def _latin(word: str) -> bool:
    return any("LATIN" in unicodedata.name(c, "") for c in word if c.isalpha())


def _foreign(word: str) -> bool:
    return any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in word)


def _tag_label(word: str) -> Optional[str]:
    w = word.upper().strip("()[]")
    if word.upper() in _TAGS:
        return _TAGS[word.upper()]
    if w in _TAGS:
        return _TAGS[w]
    if re.fullmatch(r"\d{3,4}P|\d{2}FPS", w):
        return ""
    return None


_CASE = {"MOTOGP": "MotoGP", "MILB": "MiLB", "NCAAB": "NCAAB", "NCAAF": "NCAAF", "GAA": "GAA", "ITV": "ITV",
         "EPL": "EPL", "ACB": "ACB", "SFL": "SFL", "SPFL": "SPFL", "NRL": "NRL", "AFL": "AFL", "UFC": "UFC",
         "WWE": "WWE", "F1": "F1", "AFC": "AFC", "UEFA": "UEFA", "FIFA": "FIFA", "IPLAYER": "iPlayer", "BEIN": "beIN", "DAZN": "DAZN"}


def _recase(word: str, tidied: str) -> str:
    """tidy_case() plus: letters it can't read ("TELEFÓNICA" ->
    "Telefónica"), a long word with a digit ("TELE2" -> "Tele2"), and
    names spelled their own way ("MOTOGP" -> "MotoGP")."""
    if word.upper() in _CASE and word.isupper():
        return _CASE[word.upper()]
    if not word.isupper():
        return tidied
    if not word.isascii():
        return word.capitalize()
    letters = re.sub(r"[^A-Z]", "", word)
    if any(c.isdigit() for c in word) and len(letters) >= 4 and re.search(r"[AEIOU]", letters):
        return word.capitalize()
    return tidied


def describe(name: str) -> dict:
    """A group name read: `codes` (the prefix codes, outermost first),
    `title`, `tags` (labels), `language` (a language the name states),
    `event` (a group of event slots, PPV), `source` ("PRIME"...)."""
    text = "".join(" " if unicodedata.category(c) in ("So", "Sk", "Cf") or c == "️" else c
                   for c in (name or ""))
    for c in _CUT:
        text = text.split(c, 1)[0]
    text, sup = _split_superscripts(text)
    text = unicodedata.normalize("NFKC", text)
    codes, language, source = [], None, None
    while True:
        m = _PREFIX_RE.match(text)
        if not m:
            break
        code, lang = m.group(1).upper(), (m.group(2) or "").upper()
        if code in _SOURCES:
            source = source or code
        else:
            codes.append(code)
        if lang in LANGUAGES:
            language = LANGUAGES[lang]
        text = text[m.end():]
    tags, event = [], False
    for w in sup:
        lab = _tag_label(w)
        if lab is None:
            lab = w.upper() if len(w) <= 3 else w.capitalize()
        if lab and lab not in tags:
            tags.append(lab)
    # "ΓΕΝΙΚΟΣ/GENERAL": the Latin side; "NOVA/SPORTS": both words.
    text = re.sub(r"(?<=[^\W\d_])\s*/\s*(?=[^\W\d_])", " ", text)
    words = []
    for w in text.split():
        parts = w.split("/")
        words.extend(parts if len(parts) > 1 and all(_tag_label(x) is not None for x in parts) else [w])
    if any(_latin(w) for w in words):
        words = [w for w in words if not _foreign(w)]
    kept = []
    for w in words:
        up = w.upper()
        if up in _EVENT_WORDS:
            event = True
            kept.append(w)
            continue
        lab = _tag_label(w)
        if lab is not None:
            if lab and lab not in tags:
                tags.append(lab)
            continue
        if up.strip("+-") and re.fullmatch(r"[+-]\d+H", up):
            continue   # "+6H": a time shift
        kept.append(w)
    while kept and kept[-1].upper() in LANGUAGES and len(kept) > 1:
        language = language or LANGUAGES[kept[-1].upper()]
        kept.pop()
    while kept and kept[-1] in ("-", "|", "/", "&"):
        kept.pop()
    # The event word goes when the rest still names the group ("F1 PPV" ->
    # "F1"); "PPV EVENT" alone stays.
    rest = [w for w in kept if w.upper() not in _EVENT_WORDS]
    if event and rest:
        kept = rest
    title = " ".join(_recase(w, t) for w, t in zip(kept, tidy_case(" ".join(kept)).split()))
    title = title.strip(" -|/")
    title = re.sub(r"\b([A-Za-z]{1,2}&[A-Za-z]{1,2})\b", lambda m: m.group(1).upper(), title)
    if not title:
        title = tidy_case(" ".join(words)) or (name or "").strip()
    return {"codes": codes, "title": title, "tags": tags, "language": language, "event": event,
            "source": source}


def _country_in(title: str) -> Optional[str]:
    """The country (ISO code) or region (REGIONS key) a title starts with:
    "Bosnia", "Montenegro Cma Gora", "Caribbean Hub"."""
    up = re.sub(r"\s+", " ", title.upper()).strip()
    for n in _PLACE_WORDS:
        if up == n or up.startswith(n + " "):
            return _NAME_TO_CODE.get(n) or n
    return None


_PLACE_WORDS = sorted(set(_NAME_TO_CODE) | {k for k in REGIONS if len(k) > 3}, key=len, reverse=True)


def _place(key: str, code: str) -> dict:
    if key in COUNTRIES and not (key == code and code in REGIONS):
        return {"code": code, "iso": key, "name": COUNTRIES[key], "kind": "country"}
    return {"code": code, "iso": None, "name": REGIONS.get(key, key), "kind": "region"}


class CodeBook:
    """What each prefix code means for one provider (or several), from the
    titles of all its groups."""

    def __init__(self, names: Iterable[str]):
        self._named = defaultdict(lambda: defaultdict(int))
        self._groups = defaultdict(int)
        for n in names:
            d = describe(n)
            if not d["codes"]:
                continue
            code = d["codes"][0]
            self._groups[code] += 1
            c = _country_in(d["title"])
            if c:
                self._named[code][c] += 1

    def place(self, code: Optional[str]) -> Optional[dict]:
        """What a code stands for: {"code": the key groups fold by, "iso":
        the flag's country or None, "name", "kind": "country" | "region"},
        or None (not a place: "4K|", "TS|")."""
        if not code:
            return None
        code = code.upper()
        if code in _NOT_PLACES:
            return None
        if code in REGIONS:
            return _place(code, code)
        if code == "UK":
            return _place("GB", code)
        # The place most of a code's groups name ("SR| SERBIA ...").
        named = self._named.get(code) or {}
        if named:
            top, n = max(named.items(), key=lambda kv: kv[1])
            if n >= 2 or n * 2 >= self._groups.get(code, 1):
                return _place(top, code)
        if code in COUNTRIES:
            return _place(code, code)
        return None

    def read(self, name: str) -> dict:
        """describe() plus `place` (as place(); a title that starts with a
        country is that country: "CH| CHINA") and `lang` (the language its
        place speaks, or the one its name states)."""
        d = describe(name)
        place = None
        for code in d["codes"]:
            place = self.place(code)
            if place:
                break
        if place is not None and place["kind"] == "country":
            own = _country_in(d["title"])
            if own in COUNTRIES and own != place["iso"]:
                place = _place(own, own)
        if place is None and d["source"] in _SOURCE_COUNTRY:
            place = _place(_SOURCE_COUNTRY[d["source"]], _SOURCE_COUNTRY[d["source"]])
        d["place"] = place
        if d["title"].upper() == "TV" and place is not None:
            d["title"] = f"{_SHORT.get(place['iso'], place['name'])} TV"   # "US| TV": "US TV"
        lang = d["language"]
        if lang is None and place is not None:
            lang = LANGUAGE_OF.get(place["iso"] or place["code"])
        d["lang"] = lang
        return d
