"""
Tentacle - Title Cleaner
Cleans raw IPTV provider titles for TMDB lookup.
Ported and improved from xtream_to_jellyfin.py
"""

import html
import re
from typing import Tuple, Optional

# All known provider prefixes
STRIP_PREFIXES = [
    # Streaming services
    'NF', 'AMZ', 'ATVP', 'DSNP', 'HBO', 'MAX', 'HULU', 'PMTP', 'PCOK',
    'SHWT', 'AMZN', 'NFLX', 'DNSP', 'APTV', 'A+', 'D+',
    # Quality/source
    'EN', '4K', 'UHD', 'HD', 'SD', 'TOP', 'NEW', 'CAM', 'TS', 'TC',
    'HDCAM', 'HDTS', 'DVDSCR', 'WEBDL', 'WEBRIP', 'BLURAY', 'BDRIP',
    'HDRIP', 'DVDRIP', 'EN-TOP',
    # Studios
    'MRVL', 'UNV', 'DWA', 'VP', 'NICK', 'CR', 'STAN', 'PCOK',
    # Other
    'MULTI', 'DUAL', 'DUBBED', 'SUBBED', 'SUBS',
]

# Quality patterns to strip from end/middle
QUALITY_PATTERNS = [
    r'\b(1080p|720p|2160p|4K|UHD|HDR|HDR10|DV|DOLBY\.?VISION)\b',
    r'\b(WEB[-.]?DL|WEB[-.]?RIP|BLU[-.]?RAY|BD[-.]?RIP|HD[-.]?RIP|DVD[-.]?RIP)\b',
    r'\b(x264|x265|HEVC|H\.?264|H\.?265|AVC|REMUX)\b',
    r'\b(AAC|AC3|DTS|ATMOS|TRUEHD|DD5\.?1|DDP5\.?1)\b',
    r'\[.*?\]',
    r'\((?!(?:19|20)\d{2}\)).*?\)',  # Parens that aren't years
]

# Tags that are also ordinary words in catalogue titles ("Dan in Real Life",
# "The Real Housewives of ...", "Love with the Proper Stranger"). They are only
# stripped from names that look like a scene release, never from a plain
# provider title.
SCENE_ONLY_PATTERNS = [
    r'\b(AMZN|NF|DSNP|ATVP|HBO|HULU|PCOK)\b',
    r'\b(PROPER|REPACK|RERIP|REAL|INTERNAL)\b',
]

# What makes a name "scene-like": a release tag no catalogue title carries.
# Scene-only tags (any mix of them) trailing a "(YYYY)" group.
_TRAILING_SCENE_TAGS_RE = re.compile(
    r'(\(\d{4}\))(?:\s+(?:AMZN|NF|DSNP|ATVP|HBO|HULU|PCOK|PROPER|REPACK|RERIP|REAL|INTERNAL))+\s*$',
    re.IGNORECASE,
)
SCENE_MARKER_RE = re.compile(
    r'\b(480p|720p|1080p|2160p|x264|x265|HEVC|H\.?264|H\.?265|AVC|REMUX|HDTV|'
    r'WEB[-.]?DL|WEB[-.]?RIP|BLU[-.]?RAY|BD[-.]?RIP|HD[-.]?RIP|DVD[-.]?RIP)\b',
    re.IGNORECASE,
)

_SUPERSCRIPTS = '⁰¹²³⁴⁵⁶⁷⁸⁹'


def _is_known_tag(word: str) -> bool:
    """A service, quality or release tag ("NF", "4K", "HEVC", "x265")."""
    if word.upper() in STRIP_PREFIXES or SCENE_MARKER_RE.fullmatch(word):
        return True
    return any(re.fullmatch(p, word, re.IGNORECASE)
               for p in QUALITY_PATTERNS[:4] + SCENE_ONLY_PATTERNS)


def clean_list_title(title: Optional[str], year: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Clean a list-sourced title (IMDb/Trakt/Letterboxd/Servarr imports).

    Unescapes HTML entities (e.g. "Guess Who&#039;s Coming to Dinner") and
    strips a trailing " (YYYY)" year suffix (e.g. "Midnight in Paris (2011)").
    If year is empty/None, it's populated from the stripped suffix.
    Returns (clean_title, year).
    """
    if not title:
        return title, year

    name = html.unescape(title).strip()

    match = re.search(r'\s*\(((?:19|20)\d{2})\)\s*$', name)
    if match:
        stripped = name[:match.start()].strip()
        if stripped:  # never strip a title down to nothing (e.g. "(2011)")
            name = stripped
            if not year:
                year = match.group(1)

    return name, year


def clean_title(raw_name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Clean a raw provider title for TMDB search.
    Returns (clean_title, year) or (None, None) if invalid.

    Handles formats like:
    - "NF - The Dark Knight (2008)"
    - "AMZ - Movie Name (2024)"
    - "D+ - Film Title (2023)"
    - "A+ - Show Name (2022)"
    - "EN - Action Movie (2021)"
    - "EN-TOP - 250. Movie Name (2019)"
    - "Movie.Name.2020.1080p.WEB-DL"
    """
    if not raw_name:
        return None, None

    raw = name = raw_name.strip()

    # Step 1: Remove bracketed prefixes [NF] or (AMZ)
    # A bracketed word that is no known tag and has a letter in it may be the
    # title itself ("[REC] (2007)", "[REC]³ Genesis"): it is kept aside and
    # put back in step 8 when nothing usable is left without it.
    lead = None
    lead_match = re.match(
        r'^\s*[\[\(]([A-Z0-9+]{1,6})[\]\)]\s*[-:]?\s*', name, flags=re.IGNORECASE
    )
    if lead_match:
        word = lead_match.group(1)
        if re.search(r'[A-Za-z]', word) and not _is_known_tag(word):
            lead_end = lead_match.end(1) + 1
            lead = name[:lead_end]
            lead_sep = ' ' if lead_match.end() > lead_end else ''
        name = name[lead_match.end():]

    # Step 2: Remove standard "PREFIX - " patterns
    # Handles: "NF - ", "AMZ - ", "D+ - ", "A+ - ", "EN - ", "EN-TOP - ", "NF-DO - "
    #
    # A separator is required. Many prefixes are also the first word of real
    # titles ("Top Gun", "New Girl", "Max Steel", "Cam", "Max"), so a prefix
    # followed by nothing but a space is left alone — otherwise the title is
    # searched on TMDB truncated, or drops out of the sync entirely.
    prefix_pattern = '|'.join(
        re.escape(p) for p in sorted(STRIP_PREFIXES, key=len, reverse=True)
    )
    prefix_re = rf'^(?:(?:{prefix_pattern})(?:[-+][A-Z0-9]{{1,4}})?\s*)+'
    stripped = re.sub(prefix_re + r'[-:|]+\s*', '', name, flags=re.IGNORECASE)
    if stripped != name:
        name = stripped
    elif re.match(prefix_re + r'\.', name) and re.search(r'\.(?:19|20)\d{2}\.', name):
        # Scene dot-notation with a prefix ("NF.The.Matrix.1999.1080p").
        # Case-sensitive on purpose: "Top.Gun.1986.1080p" is a title, not a prefix.
        name = re.sub(prefix_re + r'\.', '', name)

    # Step 3: Remove numbered rankings "250. " or "86. "
    name = re.sub(r'^\d{1,3}\.\s*', '', name)

    # Steps 1-3 only cut from the front: what they cut is the head of raw.
    head = raw[:len(raw) - len(name)]
    after_head = name

    # Step 4: Handle scene dot-notation (Movie.Name.2020.1080p)
    scene_like = bool(SCENE_MARKER_RE.search(name))
    if re.search(r'^[A-Za-z0-9]+\.[A-Za-z0-9]+.*\.\d{4}\.', name):
        year_match = re.search(r'\.(\d{4})\.', name)
        if year_match:
            year = year_match.group(1)
            title_part = name[:year_match.start()].replace('.', ' ')
            name = f"{title_part} ({year})"
            scene_like = True

    # Step 5: Strip quality tags (scene-only tags only on scene-like names).
    # A scene-only tag that TRAILS a "(YYYY)" group is a release tag too —
    # "Show (2023) HBO", "Movie (2020) PROPER" — never part of the title, and
    # leaving it there put the year out of reach of step 6 and the title out of
    # reach of the TMDB matcher.
    patterns = QUALITY_PATTERNS + (SCENE_ONLY_PATTERNS if scene_like else [])
    for pattern in patterns:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE)
    if not scene_like:
        name = _TRAILING_SCENE_TAGS_RE.sub(r'\1', name)

    # Step 6: Extract year
    year = None
    year_match = re.search(r'\((\d{4})\)\s*$', name)
    if year_match:
        year = year_match.group(1)
        name = name[:year_match.start()].strip()
    else:
        # Not right after a colon: "Space: 1999" is a title, not "Space" + 1999.
        year_match = re.search(r'(?<![:\s])\s+(\d{4})\s*$', name)
        if year_match:
            potential_year = year_match.group(1)
            if 1920 <= int(potential_year) <= 2035:
                year = potential_year
                name = name[:year_match.start()].strip()

    # Step 7: Cleanup
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'[._-]+$', '', name).strip(' -:.')

    # Step 8: Validate
    if lead and (len(name) < 2 or name[0] in _SUPERSCRIPTS):
        name = f"{lead}{lead_sep}{name}".strip()

    if not name or len(name) < 2:
        return None, None

    # Reject a lower-case start only where the cleaner cut into the title
    # ("12.to.Midnight.2024" -> "to Midnight"); "iCarly", "mother!" and
    # "EN - eXistenZ" start lower-case on the provider's side.
    if name[0].islower():
        first = re.match(r'\w+', name).group(0)
        clean_cut = not head or head[-1].isspace() or head[-1] in '])|'
        if not (clean_cut and after_head.startswith(first)):
            return None, None

    # Reject gibberish (long word with no vowels; y counts: "Psych", "Flynn")
    first_word = re.split(r'[\s:,\-]', name)[0]
    first_alpha = re.sub(r'[^a-zA-Z]', '', first_word)
    if len(first_alpha) > 4 and not re.search(r'[aeiouyAEIOUY]', first_alpha):
        return None, None

    # Validate year range
    if year and not (1900 <= int(year) <= 2035):
        year = None

    return name, year
