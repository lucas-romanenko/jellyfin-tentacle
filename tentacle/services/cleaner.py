"""
Tentacle - Title Cleaner
Cleans raw IPTV provider titles for TMDB lookup.
Ported and improved from xtream_to_jellyfin.py
"""

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
    r'\b(AMZN|NF|DSNP|ATVP|HBO|HULU|PCOK)\b',
    r'\b(PROPER|REPACK|RERIP|REAL|INTERNAL)\b',
    r'\[.*?\]',
    r'\((?!(?:19|20)\d{2}\)).*?\)',  # Parens that aren't years
]


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

    name = raw_name.strip()

    # Step 1: Remove bracketed prefixes [NF] or (AMZ)
    name = re.sub(
        r'^\s*[\[\(]([A-Z0-9+]{1,6})[\]\)]\s*[-:]?\s*',
        '', name, flags=re.IGNORECASE
    )

    # Step 2: Remove standard "PREFIX - " patterns
    # Handles: "NF - ", "AMZ - ", "D+ - ", "A+ - ", "EN - ", "EN-TOP - ", "NF-DO - "
    prefix_pattern = '|'.join(
        re.escape(p) for p in sorted(STRIP_PREFIXES, key=len, reverse=True)
    )
    name = re.sub(
        rf'^(?:(?:{prefix_pattern})(?:[-+][A-Z0-9]{{1,4}})?\s*)+[-:.\s]+',
        '', name, flags=re.IGNORECASE
    )

    # Step 3: Remove numbered rankings "250. " or "86. "
    name = re.sub(r'^\d{1,3}\.\s*', '', name)

    # Step 4: Handle scene dot-notation (Movie.Name.2020.1080p)
    if re.search(r'^[A-Za-z0-9]+\.[A-Za-z0-9]+.*\.\d{4}\.', name):
        year_match = re.search(r'\.(\d{4})\.', name)
        if year_match:
            year = year_match.group(1)
            title_part = name[:year_match.start()].replace('.', ' ')
            name = f"{title_part} ({year})"

    # Step 5: Strip quality tags
    for pattern in QUALITY_PATTERNS:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE)

    # Step 6: Extract year
    year = None
    year_match = re.search(r'\((\d{4})\)\s*$', name)
    if year_match:
        year = year_match.group(1)
        name = name[:year_match.start()].strip()
    else:
        year_match = re.search(r'\s(\d{4})\s*$', name)
        if year_match:
            potential_year = year_match.group(1)
            if 1920 <= int(potential_year) <= 2035:
                year = potential_year
                name = name[:year_match.start()].strip()

    # Step 7: Cleanup
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'[._-]+$', '', name).strip(' -:.')

    # Step 8: Validate
    if not name or len(name) < 2:
        return None, None

    # Reject if starts with lowercase (truncated title like "aptain America")
    if name[0].islower():
        return None, None

    # Reject gibberish (long word with no vowels)
    first_word = re.split(r'[\s:,\-]', name)[0]
    first_alpha = re.sub(r'[^a-zA-Z]', '', first_word)
    if len(first_alpha) > 4 and not re.search(r'[aeiouAEIOU]', first_alpha):
        return None, None

    # Validate year range
    if year and not (1900 <= int(year) <= 2035):
        year = None

    return name, year
