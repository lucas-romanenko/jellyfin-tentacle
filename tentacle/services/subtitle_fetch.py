"""Subtitles in a language the file doesn't carry, from a subtitle site.

Through Jellyfin: its remote subtitle search asks the subtitle plugins it
has (Open Subtitles) by the title's ids (IMDb/TMDB, season and episode), and
hands over the file. Tentacle picks one, lines it up with the film's sound
(services.subtitle_sync, from the speech pattern the extraction kept) and
writes it beside the title like its own. "Subtitles wrong? Get another"
(`another`) remembers the one that was wrong and takes the next.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from models.database import SubtitleFile, SubtitleTried
from services import watch_prefs

logger = logging.getLogger(__name__)


def _search(db, item_id: str, lang: str) -> list:
    import requests
    from services.subtitles import _jellyfin
    url, key = _jellyfin(db)
    if not url or not key:
        return []
    r = requests.get(f"{url}/Items/{item_id}/RemoteSearch/Subtitles/{lang}", params={"isPerfectMatch": "false"},
                     headers={"X-Emby-Token": key}, timeout=60)
    r.raise_for_status()
    return r.json() or []


def _download(db, subtitle_id: str) -> bytes:
    import requests
    from services.subtitles import _jellyfin
    url, key = _jellyfin(db)
    r = requests.get(f"{url}/Providers/Subtitles/Subtitles/{subtitle_id}", headers={"X-Emby-Token": key}, timeout=60)
    r.raise_for_status()
    return r.content


def rank(results: list, tried: set, hearing_impaired: bool = False) -> list:
    """The site's answers worth trying, best first: SRT, not tried before,
    not forced-only, the kind asked for (plain or SDH) first, then the most downloaded."""
    ok = [r for r in results if str(r.get("Id")) not in tried and (r.get("Format") or "srt").lower() == "srt"
          and not r.get("IsForced")]
    return sorted(ok, key=lambda r: (bool(r.get("HearingImpaired")) != hearing_impaired,
                                     not r.get("IsHashMatch"), -(r.get("DownloadCount") or 0)))


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


async def fetch(db, item: dict, media: Path, lang: str, another: bool = False) -> Optional[str]:
    """Fetch, line up and write the `lang` subtitle for a title; what it
    wrote, in words ("English from Open Subtitles, moved 2.4 s"), or None."""
    from services import subtitle_sync
    from services.subtitles import _place, _record, sidecar_path
    item_id = item["Id"].replace("-", "")
    current = (db.query(SubtitleFile).filter(SubtitleFile.jellyfin_item_id == item_id, SubtitleFile.language == lang,
                                             SubtitleFile.forced.is_(False)).first())
    if another and current is not None and current.source != "embedded" and current.source_ref:
        db.add(SubtitleTried(jellyfin_item_id=item_id, language=lang, source_ref=current.source_ref))
        db.commit()
    tried = {t.source_ref for t in db.query(SubtitleTried).filter(SubtitleTried.jellyfin_item_id == item_id,
                                                                    SubtitleTried.language == lang).all()}
    try:
        results = await asyncio.to_thread(_search, db, item["Id"], lang)
    except Exception as e:
        logger.info(f"[Subtitles] {item.get('Name')}: the subtitle site search failed ({type(e).__name__})")
        return None
    choices = rank(results, tried)
    if not choices:
        logger.info(f"[Subtitles] {item.get('Name')}: no {watch_prefs.name(lang)} subtitles on the subtitle sites"
                    + (" that weren't tried already" if tried else ""))
        return None
    speech_file = subtitle_sync.speech_path(item_id)
    speech = speech_file.read_bytes() if speech_file.exists() else b""
    for choice in choices[:3]:
        try:
            text = decode(await asyncio.to_thread(_download, db, str(choice["Id"])))
        except Exception as e:
            logger.info(f"[Subtitles] {item.get('Name')}: subtitle {choice.get('Id')} not downloaded ({type(e).__name__})")
            continue
        cues = subtitle_sync.parse_srt(text)
        if not cues:
            continue
        if not subtitle_sync.belongs(cues, speech):
            logger.info(f"[Subtitles] {item.get('Name')}: subtitle {choice.get('Id')} runs past the end of the film; "
                        f"it's for another cut, the next one is tried")
            continue
        lined, shifted = subtitle_sync.line_up(text, speech) if speech else (text, None)
        dest = sidecar_path(media, lang)
        tmp = dest.with_name("." + dest.name + ".fetched")
        tmp.write_text(lined, encoding="utf-8")
        try:
            await asyncio.to_thread(_place, tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        if current is not None and current.path != str(dest):
            Path(current.path).unlink(missing_ok=True)
            db.delete(current)
            db.commit()
        _record(db, item_id, dest, (lang, False, False), (choice.get("ProviderName") or "site").lower().replace(" ", ""),
                str(choice["Id"]), shifted)
        where = choice.get("ProviderName") or "a subtitle site"
        moved = f", moved {abs(shifted) / 1000:.1f} s {'earlier' if shifted < 0 else 'later'}" if shifted else ""
        return f"{watch_prefs.name(lang)} from {where}{moved}"
    return None
