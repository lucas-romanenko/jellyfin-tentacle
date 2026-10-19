"""Channel names reduced to a key two sources can be compared on.

Provider channel names are decorated and inconsistent: "CA: TSN 5 ᴿᴬᵂ",
"CA EN: DISCOVERY HD", "LT: BTV HD", "|US| FOX". An XMLTV feed calls the same
channels "TSN 5" or "Discovery". Guide matching by name (#141) and artwork
lookups by name (#137) both need the two reduced to one form first.
"""
import re
import unicodedata
from typing import Optional

# A leading country / language tag: "CA:", "CA EN:", "US|", "|US|", "[UK]".
# Two-letter codes (plus a few longer ones panels use), so a brand such as
# "CNN: International" keeps its name.
_PREFIX_RE = re.compile(
    r"^\s*[|\[(]?\s*(?:[A-Za-z]{2}(?:[ /-][A-Za-z]{2})?|USA|EXYU|LATAM|ARAB)\s*[|\]):]\s*")

# Quality and format markers that say nothing about which channel it is.
_MARKERS = {
    "hd", "fhd", "uhd", "sd", "hq", "lq", "4k", "8k", "hdr", "hevc", "h264", "h265",
    "raw", "1080p", "1080i", "720p", "576i", "50fps", "60fps",
}


def channel_name_words(name: str) -> list:
    """channel_name_key() before the spaces are dropped: the name's words."""
    if not name:
        return []
    text = unicodedata.normalize("NFKC", name)
    stripped = _PREFIX_RE.sub("", text, count=1)
    if stripped.strip():
        text = stripped
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("+", " plus ")
    return [w for w in re.split(r"[^0-9a-z]+", text) if w and w not in _MARKERS]


def channel_name_key(name: str) -> str:
    """"CA: TSN 5 ᴿᴬᵂ" -> "tsn5"; "" when nothing distinctive is left."""
    if not name:
        return ""
    # Compatibility forms first: superscript ᴿᴬᵂ becomes RAW, full-width
    # letters become ASCII, so the prefix and markers below can see them.
    text = unicodedata.normalize("NFKC", name)
    stripped = _PREFIX_RE.sub("", text, count=1)
    if stripped.strip():
        text = stripped
    # Accents off: "Šiauliai" and "Siauliai" are the same channel name.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("+", " plus ")
    words = [w for w in re.split(r"[^0-9a-z]+", text) if w and w not in _MARKERS]
    # Spaces are dropped too, so "TSN 5" and "TSN5" meet.
    return "".join(words)


# The country a provider's leading tag names: "CA:", "CA EN:", "|UK|", "[US]".
_COUNTRY_RE = re.compile(r"^\s*[|\[(]?\s*([A-Za-z]{2}|USA)(?:[ /-]([A-Za-z]{2}))?\s*[|\]):]")
# One country, two spellings.
_SAME_COUNTRY = {"uk": "gb", "usa": "us"}
_ID_COUNTRY_RE = re.compile(r"\.([A-Za-z]{2})$")
# ISO 639-1 language codes that are no ISO 3166-1 country code ("EN:" names
# English, no country). Not "uk" (panels mean the United Kingdom) or "eu" (Europe).
_LANGUAGE_ONLY = frozenset(
    "aa ab ak an av ay ce cs da dv el en eo fa ff fy gv ha he hi ho hy hz ia ig ii ik iu ja jv "
    "ka kj kk kl ko ks ku kv lg ln lo mi nb nd nn nv ny oc oj or os pi qu rm rn sq su sw ta te "
    "ti ts ty ur vo wa wo xh yi yo zh zu".split())


def _tag_codes(name: str) -> list:
    """A leading tag's codes, lower case: "CA EN: X" -> ["ca", "en"]; [] without a tag."""
    m = _COUNTRY_RE.match(unicodedata.normalize("NFKC", name or ""))
    if not m:
        return []
    return [code.lower() for code in m.groups() if code]


def _first_tag(name: str) -> Optional[str]:
    """The leading tag's first code as a country, language codes included."""
    codes = _tag_codes(name)
    return _SAME_COUNTRY.get(codes[0], codes[0]) if codes else None


def channel_country(name: str) -> Optional[str]:
    """"CA EN: DISCOVERY HD" -> "ca", "EN CA: X" -> "ca", "|UK| SKY ONE" -> "gb";
    None without a tag or with only a language ("EN: X")."""
    for code in _tag_codes(name):
        if code not in _LANGUAGE_ONLY:
            return _SAME_COUNTRY.get(code, code)
    return None


def feed_countries(feed_id: str, names) -> set:
    """The countries an XMLTV channel belongs to: its id's two-letter suffix
    ("SkyOne.de") and any tag its display names carry. Empty when it says none.
    A language tag counts here ("JA: X" names "ja"), so such a channel never
    becomes another country's guide."""
    out = set()
    m = _ID_COUNTRY_RE.search(feed_id or "")
    if m:
        code = m.group(1).lower()
        out.add(_SAME_COUNTRY.get(code, code))
    for name in names or []:
        country = _first_tag(name)
        if country:
            out.add(country)
    return out
