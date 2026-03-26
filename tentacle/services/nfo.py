"""
Tentacle - NFO Writer Service
Writes complete Jellyfin-compatible NFO files from TMDB metadata.
Tags are written here — this is the single source of truth for NFO content.
"""

import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger(__name__)


def _x(text) -> str:
    """XML escape"""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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


def update_nfo_tags(nfo_path: Path, tags: List[str]) -> bool:
    """
    Update only the <tag> entries in an existing NFO.
    Preserves all other content.
    """
    if not nfo_path.exists():
        return False

    try:
        content = nfo_path.read_text(encoding='utf-8')

        # Remove existing tags
        import re
        content = re.sub(r'\s*<tag>.*?</tag>\n?', '', content)

        # Insert new tags before closing tag
        tag_xml = '\n'.join(f'  <tag>{_x(t)}</tag>' for t in tags)
        close_tag = '</movie>' if '</movie>' in content else '</tvshow>'
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
