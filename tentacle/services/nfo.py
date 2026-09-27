"""
Tentacle - NFO Writer Service
Writes complete Jellyfin-compatible NFO files from TMDB metadata.
Tags are written here — this is the single source of truth for NFO content.
"""

import hashlib
import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

# Characters that are illegal in XML 1.0 (everything below 0x20 except
# tab/newline/carriage-return). These must be stripped, not escaped, or the
# resulting NFO is not well-formed and Jellyfin will fail to parse it.
_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(text) -> str:
    """Escape text for safe inclusion in XML element content/attributes.

    Uses xml.sax.saxutils.escape (handles &, <, >) plus an explicit quote
    map, and strips control characters that are illegal in XML 1.0. This
    prevents both malformed output and injection of closing tags via raw
    metadata (e.g. a title containing '</movie>').
    """
    if not text:
        return ""
    cleaned = _INVALID_XML_CHARS.sub("", str(text))
    return _xml_escape(cleaned, {'"': "&quot;", "'": "&apos;"})


def write_movie_nfo(
    nfo_path: Path,
    metadata: dict,
    tags: List[str],
    stream_url: Optional[str] = None,
) -> bool:
    """
    Write a complete movie NFO file.
    metadata: dict from TMDBService.get_movie_details()
    tags: list of collection tags to apply
    """
    try:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<movie>']

        # Core identity
        lines.append(f'  <title>{_x(metadata.get("title", ""))}</title>')
        lines.append(f'  <originaltitle>{_x(metadata.get("title", ""))}</originaltitle>')

        if metadata.get("year"):
            lines.append(f'  <year>{metadata["year"]}</year>')

        if metadata.get("tmdb_id"):
            lines.append(f'  <tmdbid>{metadata["tmdb_id"]}</tmdbid>')

        if metadata.get("imdb_id"):
            lines.append(f'  <imdbid>{metadata["imdb_id"]}</imdbid>')

        # Ratings
        if metadata.get("rating"):
            lines.append(f'  <rating>{metadata["rating"]}</rating>')
            lines.append(f'  <votes>{metadata.get("vote_count", 0)}</votes>')

        # Content
        if metadata.get("overview"):
            lines.append(f'  <plot>{_x(metadata["overview"])}</plot>')
            lines.append(f'  <outline>{_x(metadata["overview"][:200])}</outline>')

        if metadata.get("tagline"):
            lines.append(f'  <tagline>{_x(metadata["tagline"])}</tagline>')

        if metadata.get("runtime"):
            lines.append(f'  <runtime>{metadata["runtime"]}</runtime>')

        if metadata.get("status"):
            lines.append(f'  <status>{_x(metadata["status"])}</status>')

        # Genres
        for genre in (metadata.get("genres") or []):
            lines.append(f'  <genre>{_x(genre)}</genre>')

        # Studios
        for studio in (metadata.get("studios") or []):
            lines.append(f'  <studio>{_x(studio)}</studio>')

        # Tags (collections)
        for tag in tags:
            lines.append(f'  <tag>{_x(tag)}</tag>')

        # Date added
        lines.append(f'  <dateadded>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</dateadded>')

        # Directors
        for director in (metadata.get("directors") or []):
            lines.append(f'  <director>{_x(director)}</director>')

        # Cast
        for actor in (metadata.get("cast") or []):
            lines.append('  <actor>')
            lines.append(f'    <name>{_x(actor.get("name", ""))}</name>')
            if actor.get("character"):
                lines.append(f'    <role>{_x(actor["character"])}</role>')
            lines.append('  </actor>')

        # Poster/artwork
        if metadata.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{metadata['poster_path']}"
            lines.append(f'  <thumb aspect="poster">{poster_url}</thumb>')

        if metadata.get("backdrop_path"):
            backdrop_url = f"https://image.tmdb.org/t/p/w1280{metadata['backdrop_path']}"
            lines.append(f'  <fanart><thumb>{backdrop_url}</thumb></fanart>')

        lines.append('</movie>')

        nfo_path.write_text('\n'.join(lines), encoding='utf-8')
        return True

    except Exception as e:
        logger.error(f"Failed to write movie NFO {nfo_path}: {e}")
        return False


def write_series_nfo(
    nfo_path: Path,
    metadata: dict,
    tags: List[str],
) -> bool:
    """Write a complete tvshow.nfo file"""
    try:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tvshow>']

        lines.append(f'  <title>{_x(metadata.get("title", ""))}</title>')
        lines.append(f'  <originaltitle>{_x(metadata.get("title", ""))}</originaltitle>')

        if metadata.get("year"):
            lines.append(f'  <year>{metadata["year"]}</year>')

        if metadata.get("tmdb_id"):
            lines.append(f'  <tmdbid>{metadata["tmdb_id"]}</tmdbid>')

        if metadata.get("tvdb_id"):
            # Preserve the TheTVDB id so Jellyfin keeps the cross-reference (and a
            # TheTVDB metadata plugin, if installed, can use it).
            lines.append(f'  <tvdbid>{metadata["tvdb_id"]}</tvdbid>')
            lines.append(f'  <uniqueid type="tvdb">{metadata["tvdb_id"]}</uniqueid>')

        if metadata.get("rating"):
            lines.append(f'  <rating>{metadata["rating"]}</rating>')
            lines.append(f'  <votes>{metadata.get("vote_count", 0)}</votes>')

        if metadata.get("overview"):
            lines.append(f'  <plot>{_x(metadata["overview"])}</plot>')

        if metadata.get("status"):
            lines.append(f'  <status>{_x(metadata["status"])}</status>')

        for genre in (metadata.get("genres") or []):
            lines.append(f'  <genre>{_x(genre)}</genre>')

        for studio in (metadata.get("studios") or []):
            lines.append(f'  <studio>{_x(studio)}</studio>')

        for tag in tags:
            lines.append(f'  <tag>{_x(tag)}</tag>')

        lines.append(f'  <dateadded>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</dateadded>')

        for creator in (metadata.get("creators") or []):
            lines.append(f'  <director>{_x(creator)}</director>')

        for actor in (metadata.get("cast") or []):
            lines.append('  <actor>')
            lines.append(f'    <name>{_x(actor.get("name", ""))}</name>')
            if actor.get("character"):
                lines.append(f'    <role>{_x(actor["character"])}</role>')
            lines.append('  </actor>')

        if metadata.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{metadata['poster_path']}"
            lines.append(f'  <thumb aspect="poster">{poster_url}</thumb>')

        if metadata.get("backdrop_path"):
            backdrop_url = f"https://image.tmdb.org/t/p/w1280{metadata['backdrop_path']}"
            lines.append(f'  <fanart><thumb>{backdrop_url}</thumb></fanart>')

        lines.append('</tvshow>')

        nfo_path.write_text('\n'.join(lines), encoding='utf-8')
        return True

    except Exception as e:
        logger.error(f"Failed to write series NFO {nfo_path}: {e}")
        return False


def update_nfo_tags(nfo_path: Path, tags: List[str], owned: Optional[set] = None) -> bool:
    """
    Update only the <tag> entries in an existing NFO. Preserves all other
    content. True when the file was written.

    With `owned` (Tentacle's own tag names, services.tagger.tentacle_owned_tags),
    only those tags are replaced by `tags`: an NFO Jellyfin's NFO saver wrote
    also carries its own tags (TMDB keywords, hand tags), and they stay. Without
    it every <tag> line is replaced. A file whose tags already match is not
    written at all: every Refresh Tags press rewrote about 25k NFOs, and
    Jellyfin re-read each of them (#165).
    """
    if not nfo_path.exists():
        return False

    try:
        import html
        import re
        content = nfo_path.read_text(encoding='utf-8')
        existing = [html.unescape(t) for t in re.findall(r'^[ \t]*<tag>(.*?)</tag>', content, flags=re.MULTILINE)]
        kept = [t for t in existing if owned is not None and t not in owned]
        final = kept + [t for t in tags if t not in kept]
        if set(final) == set(existing) and len(final) == len(existing):
            return False

        # Remove existing tags, whole lines only. The old pattern also ate the
        # newline BEFORE each tag, so tags in the middle of the file glued the
        # lines around them together, a little more on every rewrite.
        content = re.sub(r'^[ \t]*<tag>.*?</tag>[ \t]*(?:\r?\n|$)', '', content, flags=re.MULTILINE)

        # Insert new tags before closing tag
        close_tag = '</movie>' if '</movie>' in content else '</tvshow>'
        if final:
            tag_xml = '\n'.join(f'  <tag>{_x(t)}</tag>' for t in final)
            content = content.replace(close_tag, f'{tag_xml}\n{close_tag}')

        nfo_path.write_text(content, encoding='utf-8')
        return True

    except Exception as e:
        logger.error(f"Failed to update NFO tags {nfo_path}: {e}")
        return False


def sanitize_filename(name: str) -> str:
    """Make string filesystem-safe"""
    import re
    if not name:
        return "Unknown"
    s = re.sub(r'[<>:"/\\|?*]', '', name)
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.')
    return s[:200] if s else "Unknown"


def make_folder_name(title: str, year: Optional[str]) -> str:
    safe = sanitize_filename(title)
    return f"{safe} ({year})" if year else safe


# One path component is at most 255 BYTES on ext4, XFS, btrfs and SMB, while
# sanitize_filename caps characters: 200 characters of CJK is 600 bytes, and
# such a title raised ENAMETOOLONG on every sync and never imported (#156).
MAX_NAME_BYTES = 255


def vod_folder_name(title: str, year: Optional[str], tag: str = "") -> str:
    """Folder (and file-stem) name for a title Tentacle writes into a VOD folder.

    Never starts with a dot. Jellyfin's library scanner ignores every path
    matching `**/.*` (Emby.Server.Implementations/Library/IgnorePatterns.cs),
    so "...And Justice for All (1979)" or ".hack//Sign" — both real titles —
    was written to disk and then never appeared in Jellyfin, with no error
    anywhere. Only for Tentacle's own VOD output: make_folder_name is also used
    to find folders Radarr/Sonarr named, which must not change.

    Always fits a path component with ".strm" on the end (#156): a name that
    fits is returned unchanged, so no existing folder moves; a longer one is
    cut on a UTF-8 boundary and given a stable hash, keeping " (year)" and
    `tag` (e.g. " [tmdbid-2002]", see sync._claim_vod_name).
    """
    safe = sanitize_filename(title).lstrip(". ")
    if not safe:
        # (The old check tested the whole name for a leading "(", which also
        # turned "(500) Days of Summer" into "Unknown (2009)".)
        safe = "Unknown"
    tail = (f" ({year})" if year else "") + tag
    name = f"{safe}{tail}"
    if len(name.encode("utf-8")) + _STEM_SUFFIX_BYTES <= MAX_NAME_BYTES:
        return name
    # Too long for the filesystem once ".strm" is added: ext4, XFS, btrfs and
    # SMB cap one name at 255 BYTES, and sanitize_filename's 200-character cap
    # is 600 bytes of CJK. mkdir raised ENAMETOOLONG and the title failed with
    # an ERROR on every sync. Only such names change — every name that fits
    # stays exactly as it was, so no existing Jellyfin item moves. The hash of
    # the full name keeps two long titles that share a prefix apart, and is
    # stable, so the same title lands in the same folder on every sync.
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    tail = f" {digest}{tail}"
    budget = MAX_NAME_BYTES - _STEM_SUFFIX_BYTES - len(tail.encode("utf-8"))
    return fit_bytes(safe, budget).rstrip(" .") + tail


# The longest suffix added to a VOD stem: ".strm" (".nfo" is shorter).
_STEM_SUFFIX_BYTES = len(".strm")


def fit_bytes(text: str, budget: int) -> str:
    """Truncate to `budget` bytes of UTF-8 without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    return encoded[:max(budget, 0)].decode("utf-8", errors="ignore")


def fit_file_stem(stem: str, tail: str, suffix_bytes: int) -> str:
    """stem + tail, unchanged if it fits in one name with the suffix.

    Otherwise `stem` alone is shortened (plus a stable hash) and `tail` — an
    episode's " S01E02", which Jellyfin parses the episode from — is kept whole.
    """
    full = f"{stem}{tail}"
    if len(full.encode("utf-8")) + suffix_bytes <= MAX_NAME_BYTES:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    budget = MAX_NAME_BYTES - suffix_bytes - len(tail.encode("utf-8")) - len(digest) - 1
    return f"{fit_bytes(stem, budget).rstrip(' .')} {digest}{tail}"

