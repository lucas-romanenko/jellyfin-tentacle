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
_COUNTRY_RE = re.compile(r"^\s*[|\[(]?\s*([A-Za-z]{2}|USA)(?:[ /-][A-Za-z]{2})?\s*[|\]):]")
# One country, two spellings.
_SAME_COUNTRY = {"uk": "gb", "usa": "us"}
_ID_COUNTRY_RE = re.compile(r"\.([A-Za-z]{2})$")


def channel_country(name: str) -> Optional[str]:
    """"CA EN: DISCOVERY HD" -> "ca", "|UK| SKY ONE" -> "gb"; None without a tag."""
    m = _COUNTRY_RE.match(unicodedata.normalize("NFKC", name or ""))
    if not m:
        return None
    code = m.group(1).lower()
    return _SAME_COUNTRY.get(code, code)


def feed_countries(feed_id: str, names) -> set:
    """The countries an XMLTV channel belongs to: its id's two-letter suffix
    ("SkyOne.de") and any tag its display names carry. Empty when it says none."""
    out = set()
    m = _ID_COUNTRY_RE.search(feed_id or "")
    if m:
        code = m.group(1).lower()
        out.add(_SAME_COUNTRY.get(code, code))
    for name in names or []:
        country = channel_country(name)
        if country:
            out.add(country)
    return out
