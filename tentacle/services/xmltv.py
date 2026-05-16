"""
Tentacle - XMLTV Generator & Parser

Generates XMLTV XML from EPG data in the database.
Parses XMLTV from provider URLs and stores programs.
"""

import gzip
import io
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def generate_xmltv(channels: list[dict], programs: list[dict]) -> str:
    """
    Generate XMLTV XML string from channel and program data.

    channels: [{"id": "TSN1.ca", "name": "TSN 1 HD", "logo_url": "http://..."}]
    programs: [{"channel_id": "TSN1.ca", "title": "...", "description": "...",
                "start": datetime, "stop": datetime, "category": "...", "icon_url": "..."}]
    """
    root = ET.Element("tv", attrib={"generator-name": "Tentacle"})

    for ch in channels:
        channel_el = ET.SubElement(root, "channel", attrib={"id": ch["id"]})
        name_el = ET.SubElement(channel_el, "display-name")
        name_el.text = ch["name"]
        if ch.get("logo_url"):
            ET.SubElement(channel_el, "icon", attrib={"src": ch["logo_url"]})

    for prog in programs:
        start_str = prog["start"].strftime("%Y%m%d%H%M%S +0000")
        stop_str = prog["stop"].strftime("%Y%m%d%H%M%S +0000")
        prog_el = ET.SubElement(
            root, "programme",
            attrib={
                "start": start_str,
                "stop": stop_str,
                "channel": prog["channel_id"],
            },
        )
        title_el = ET.SubElement(prog_el, "title")
        title_el.text = prog.get("title", "")
        if prog.get("description"):
            desc_el = ET.SubElement(prog_el, "desc")
            desc_el.text = prog["description"]
        if prog.get("category"):
            cat_el = ET.SubElement(prog_el, "category")
            cat_el.text = prog["category"]
        if prog.get("icon_url"):
            ET.SubElement(prog_el, "icon", attrib={"src": prog["icon_url"]})

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def parse_xmltv(content: str) -> tuple[list[dict], list[dict]]:
    """
    Parse XMLTV XML string into channels and programs.

    Returns (channels, programs) where:
        channels: [{"id": str, "name": str, "logo_url": str|None}]
        programs: [{"channel_id": str, "title": str, "description": str|None,
                     "start": datetime, "stop": datetime, "category": str|None}]
    """
    root = ET.fromstring(content)
    channels = []
    programs = []

    for ch_el in root.findall("channel"):
        ch_id = ch_el.get("id", "")
        name_el = ch_el.find("display-name")
        icon_el = ch_el.find("icon")
        channels.append({
            "id": ch_id,
            "name": name_el.text if name_el is not None else ch_id,
            "logo_url": icon_el.get("src") if icon_el is not None else None,
        })

    for prog_el in root.findall("programme"):
        title_el = prog_el.find("title")
        desc_el = prog_el.find("desc")
        cat_el = prog_el.find("category")

        start = _parse_xmltv_time(prog_el.get("start", ""))
        stop = _parse_xmltv_time(prog_el.get("stop", ""))

        if start and stop:
            programs.append({
                "channel_id": prog_el.get("channel", ""),
                "title": title_el.text if title_el is not None else "",
                "description": desc_el.text if desc_el is not None else None,
                "start": start,
                "stop": stop,
                "category": cat_el.text if cat_el is not None else None,
            })

    return channels, programs


XMLTV_CACHE_DIR = "/data/xmltv_cache"
XMLTV_CACHE_MAX_AGE = 8 * 3600  # 8 hours


def _get_cache_path(url: str) -> str:
    """Get cache file path for a given XMLTV URL."""
    import hashlib, os
    os.makedirs(XMLTV_CACHE_DIR, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    return os.path.join(XMLTV_CACHE_DIR, f"xmltv_{url_hash}.xml")


def _is_cache_fresh(cache_path: str) -> bool:
    """Check if cached XMLTV file exists and is fresh enough."""
    import os, time
    if not os.path.exists(cache_path):
        return False
    age = time.time() - os.path.getmtime(cache_path)
    return age < XMLTV_CACHE_MAX_AGE


def stream_parse_xmltv(
    url: str,
    channel_ids: set[str],
    user_agent: str = "TiviMate/4.7.0 (Linux; Android 12)",
    timeout: int = 600,
    on_progress: callable = None,
    force_download: bool = False,
) -> list[dict]:
    """
    Download XMLTV (cached to disk for 8h), then stream-parse for matching channels.
    Uses iterparse to avoid loading full XML tree into memory.
    """
    import os, time

    logger.info(f"[XMLTV] Looking for channel IDs: {channel_ids}")
    cache_path = _get_cache_path(url)

    # Use cached file if fresh
    if not force_download and _is_cache_fresh(cache_path):
        cache_age_min = int((time.time() - os.path.getmtime(cache_path)) / 60)
        cache_size_mb = os.path.getsize(cache_path) / (1024 * 1024)
        logger.info(f"[XMLTV] Using cached file ({cache_size_mb:.1f}MB, {cache_age_min}min old): {cache_path}")
        if on_progress:
            on_progress(30, f"Using cached XMLTV ({cache_size_mb:.1f}MB, {cache_age_min}min old)")
    else:
        # Download to disk with progress
        logger.info(f"[XMLTV] Downloading XMLTV from {url[:80]}...")
        if on_progress:
            on_progress(5, "Downloading XMLTV from provider...")

        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=(30, timeout),
            stream=True,
        )
        resp.raise_for_status()
        resp.raw.decode_content = True

        total_bytes = 0
        with open(cache_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)
                    if total_bytes % (1024 * 1024) < 65536:  # ~every 1MB
                        mb = total_bytes / (1024 * 1024)
                        logger.info(f"[XMLTV] Downloaded {mb:.1f}MB...")
                        if on_progress:
                            on_progress(5 + min(25, int(mb)), f"Downloading... {mb:.1f}MB")

        # Decompress gzip if needed
        with open(cache_path, "rb") as f:
            magic = f.read(2)
        if magic == b'\x1f\x8b':
            logger.info("[XMLTV] Decompressing gzipped XMLTV...")
            if on_progress:
                on_progress(28, "Decompressing...")
            with open(cache_path, "rb") as f:
                raw = gzip.decompress(f.read())
            with open(cache_path, "wb") as f:
                f.write(raw)
            del raw

        size_mb = os.path.getsize(cache_path) / (1024 * 1024)
        logger.info(f"[XMLTV] Saved to cache: {size_mb:.1f}MB")

    if on_progress:
        on_progress(30, "Parsing guide data...")

    # Stream-parse from cached file — only keep matching channels
    programs = []
    total_seen = 0
    parse_start = time.time()

    with open(cache_path, "rb") as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == "programme":
                total_seen += 1
                ch_id = elem.get("channel", "")

                if ch_id in channel_ids:
                    title_el = elem.find("title")
                    desc_el = elem.find("desc")
                    cat_el = elem.find("category")
                    start = _parse_xmltv_time(elem.get("start", ""))
                    stop = _parse_xmltv_time(elem.get("stop", ""))

                    if start and stop:
                        programs.append({
                            "channel_id": ch_id,
                            "title": title_el.text if title_el is not None else "",
                            "description": desc_el.text if desc_el is not None else None,
                            "start": start,
                            "stop": stop,
                            "category": cat_el.text if cat_el is not None else None,
                        })

                elem.clear()

                if total_seen % 50000 == 0:
                    logger.info(f"[XMLTV] Parsed {total_seen} programmes, {len(programs)} matched")
                    if on_progress:
                        on_progress(30 + min(60, total_seen // 5000), f"Parsing... {total_seen} scanned, {len(programs)} matched")

    elapsed = time.time() - parse_start
    logger.info(f"[XMLTV] Done: {total_seen} total, kept {len(programs)} for {len(channel_ids)} channels in {elapsed:.1f}s")
    return programs


def download_xmltv(
    url: str,
    user_agent: str = "TiviMate/4.7.0 (Linux; Android 12)",
    timeout: int = 600,
) -> str:
    """Download XMLTV data from a URL. Returns raw XML string. Handles gzip."""
    logger.info(f"Downloading XMLTV from {url[:80]}...")
    resp = requests.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()

    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if chunk:
            chunks.append(chunk)
            total += len(chunk)

    content = b"".join(chunks)
    logger.info(f"Downloaded XMLTV: {total // 1024}KB")

    if content[:2] == b'\x1f\x8b':
        logger.info("Decompressing gzipped XMLTV...")
        content = gzip.decompress(content)
        logger.info(f"Decompressed XMLTV: {len(content) // 1024}KB")

    for encoding in ("utf-8", "latin-1", "iso-8859-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_xmltv_time(time_str: str) -> Optional[datetime]:
    """Parse XMLTV time format: 20260324060000 +0100 → convert to UTC."""
    if not time_str:
        return None
    try:
        clean = time_str.strip()
        # Parse the datetime portion
        dt = datetime.strptime(clean[:14], "%Y%m%d%H%M%S")
        # Parse timezone offset if present (e.g. +0100, -0500)
        rest = clean[14:].strip()
        if rest:
            sign = 1 if rest[0] == '+' else -1
            offset_str = rest[1:].replace(":", "")
            offset_hours = int(offset_str[:2])
            offset_minutes = int(offset_str[2:4]) if len(offset_str) >= 4 else 0
            from datetime import timedelta
            dt = dt - timedelta(hours=sign * offset_hours, minutes=sign * offset_minutes)
        return dt
    except (ValueError, IndexError):
        return None
