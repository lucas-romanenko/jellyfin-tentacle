"""Channel names reduced to a key two sources can be compared on.

Provider channel names are decorated and inconsistent: "CA: TSN 5 ᴿᴬᵂ",
"CA EN: DISCOVERY HD", "LT: BTV HD", "|US| FOX". An XMLTV feed calls the same
channels "TSN 5" or "Discovery". Guide matching by name (#141) and artwork
lookups by name (#137) both need the two reduced to one form first.
"""
import re
import unicodedata

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
