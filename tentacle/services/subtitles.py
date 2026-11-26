"""Subtitles Tentacle gets ready ahead of time, for streams and downloads.

Why: Jellyfin reads a film's embedded subtitles only when someone picks
one, with one ffmpeg over the whole file. For a provider title that is the
whole film through Tentacle while the person waits ("fetching"), all 48
tracks of it, sharing the viewer's one provider connection (the player goes
first), and it died with the playback: the episode ended or the player moved
on, Tentacle closed the playback, and minutes of reading were thrown away
(25 Nov 2026, The Altruists S01E02).

What happens instead:
- A title is queued (`queue`): when someone starts it (its season's next
  episodes come after it), when a downloaded title arrives, when someone
  picks a subtitle that isn't ready (first in line), or when someone asks
  for another.
- One title at a time (`_worker`): the household's languages only
  (`watch_prefs.household_languages`) are read out of the file with one
  ffmpeg. A provider title is read through the relay on a "background"
  slot (routers/vod.py BACKGROUND_HEADER): the lowest rank, someone who
  wants to watch takes it, the read waits and carries on from its byte; it
  belongs to no playback, so nobody stopping or moving on cuts it. A
  downloaded file is read from disk.
- Each language is written beside the title as `<name>.<lang>[.sdh|.forced].srt`,
  where every Jellyfin app finds it, and Jellyfin is asked to look again.
  The plugin then shows only those (and only the person's languages).
- A language the file doesn't have is fetched from a subtitle site through
  Jellyfin (services/subtitle_fetch.py) and lined up with the sound.

The same pass keeps the film's speech pattern (where people talk,
services/subtitle_sync.py), so lining up a fetched subtitle, now or after a
"Get another", needs no second read of a provider title.
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.database import SessionLocal, SubtitleFile, SubtitleJob, get_setting
from services import watch_prefs

logger = logging.getLogger(__name__)

# Text subtitles ffmpeg can turn into SRT. Picture subtitles (PGS, DVD) can't be.
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "tx3g"}
SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".smi")
_HI_TOKENS = {"sdh", "hi", "cc", "hearing", "hearingimpaired"}
_FORCED_TOKENS = {"forced", "foreign"}
# Priorities (lower first).
ASKED, WATCHING, SEASON, ADDED = 0, 1, 2, 3
# A queued season reaches this many episodes past the one being watched.
SEASON_AHEAD = 6
# A failed title is tried again this many times (a Tentacle restart, the provider refusing).
MAX_ATTEMPTS = 3
# Where Tentacle reaches its own relay (one uvicorn on 8888 in the container).
RELAY_BASE = os.getenv("TENTACLE_SELF_URL", "http://127.0.0.1:8888")
# How long one title's read may take (a 4K film at the provider's pace).
READ_TIMEOUT_SECONDS = 3 * 3600
# Roots where Tentacle sees the media Jellyfin plays (Jellyfin may see them
# under other paths: the same files are found by their last folders).
MEDIA_ROOTS = ["/media/vod/movies", "/media/vod/shows", "/media/movies", "/media/shows", "/media"]

_wake: Optional[asyncio.Event] = None
_worker_task: Optional[asyncio.Task] = None
# item id -> asyncio.Event set when its job finishes (people waiting in the player)
_done_events: dict = {}
# item id -> {"read": bytes, "size": bytes, "started": time}: the running read's progress
progress: dict = {}


# ── where things are ────────────────────────────────────────────────────────

def _jellyfin(db) -> tuple:
    return ((get_setting(db, "jellyfin_url", "") or "").rstrip("/"), get_setting(db, "jellyfin_api_key", "") or "")


def jellyfin_items(db, ids: list) -> list:
    import requests
    url, key = _jellyfin(db)
    if not url or not key or not ids:
        return []
    r = requests.get(f"{url}/Items", params={
        "Ids": ",".join(ids), "Fields": "Path,MediaSources,MediaStreams,ParentId,SeasonId,SeriesId",
        "EnableImages": "false", "EnableUserData": "false"}, headers={"X-Emby-Token": key}, timeout=20)
    r.raise_for_status()
    return r.json().get("Items") or []


def local_path(jellyfin_path: str, roots: Optional[list] = None) -> Optional[Path]:
    """Tentacle's path for a file Jellyfin names: Jellyfin may mount the
    media elsewhere (/data/media/...), so the file is looked for under each
    of Tentacle's roots by its last 4, 3 and 2 path parts."""
    parts = [p for p in re.split(r"[\\/]", jellyfin_path or "") if p]
    if not parts:
        return None
    direct = Path(jellyfin_path)
    if direct.is_absolute() and direct.exists():
        return direct
    for k in (4, 3, 2):
        if len(parts) < k:
            continue
        for root in roots or MEDIA_ROOTS:
            candidate = Path(root, *parts[-k:])
            if candidate.exists():
                return candidate
    return None


def relay_title(source_path: str, db=None) -> Optional[str]:
    """`movie/<token file>` of a Tentacle VOD address (or of a .strm that
    points straight at one of the providers)."""
    from services import vod_tokens
    title = vod_tokens.title_in_url(source_path or "")
    if title:
        return title
    if db is not None:
        from routers.vod import relay_path_for
        path = relay_path_for(db, source_path or "")
        if path:
            return path.split("/api/vod/", 1)[1]
    return None


# ── what is in a file ───────────────────────────────────────────────────────

def sidecar_key(path: Path, stem: str) -> Optional[tuple]:
    """(language, hearing impaired, forced) of a subtitle file beside a
    title (`<stem>.<tokens>.<ext>`); None when it isn't one of its subtitles."""
    name = path.name
    if not name.lower().endswith(SUBTITLE_SUFFIXES) or not name.startswith(stem + "."):
        return None
    tokens = [t.lower() for t in name[len(stem) + 1:].rsplit(".", 1)[0].split(".") if t]
    lang = next((watch_prefs.normalize(t) for t in tokens if watch_prefs.normalize(t)), "")
    hi = any(t in _HI_TOKENS for t in tokens)
    forced = any(t in _FORCED_TOKENS for t in tokens)
    return (lang, hi, forced)


def sidecars(media: Path) -> dict:
    """{(language, hi, forced): path} of the subtitle files beside a title."""
    out = {}
    stem = media.stem
    try:
        for f in media.parent.iterdir():
            key = sidecar_key(f, stem)
            if key and key[0]:
                out.setdefault(key, f)
    except OSError:
        pass
    return out


def sidecar_path(media: Path, lang: str, hi: bool = False, forced: bool = False) -> Path:
    """`Film (2020).en.sdh.srt`: the two-letter code Bazarr and Jellyfin's own downloads use."""
    code = watch_prefs.TWO_LETTER.get(lang, lang)
    flags = (".sdh" if hi else "") + (".forced" if forced else "")
    return media.with_name(f"{media.stem}.{code}{flags}.srt")


def _stream_key(s: dict) -> tuple:
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    disp = s.get("disposition") or {}
    title = (tags.get("title") or "").lower()
    hi = bool(disp.get("hearing_impaired")) or bool(re.search(r"\b(sdh|cc|hearing|hoh)\b", title))
    forced = bool(disp.get("forced")) or "forced" in title
    return (watch_prefs.normalize(tags.get("language")), hi, forced)


def choose(streams: list, languages: list, have: dict) -> list:
    """The subtitle streams to read: per wanted language, its first text
    track of each kind (plain, SDH, forced) the title doesn't already have
    beside it. [(stream index, (language, hi, forced))]."""
    picks, seen = [], set(have)
    for s in streams:
        if s.get("codec_type") != "subtitle" or (s.get("codec_name") or "").lower() not in TEXT_CODECS:
            continue
        key = _stream_key(s)
        if key[0] not in languages or key in seen:
            continue
        seen.add(key)
        picks.append((int(s["index"]), key))
    return picks


async def _run(cmd: list, timeout: float) -> tuple:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode, out, err


def _input_args(source: str, background: bool) -> list:
    if background:
        from routers.vod import BACKGROUND_HEADER
        # Never give up on a read the relay holds while someone watches
        # (it sends nothing until a slot is free again).
        return ["-headers", f"{BACKGROUND_HEADER}: subtitles\r\n", "-rw_timeout", str(4 * 3600 * 1_000_000), "-i", source]
    return ["-i", source]


async def probe(source: str, background: bool) -> tuple:
    """(the file's streams, its format: duration, size) — ffprobe reads its head."""
    import json
    code, out, err = await _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json"]
                                + _input_args(source, background), 600)
    if code != 0:
        raise RuntimeError(f"ffprobe failed: {(err or b'').decode(errors='replace').strip()[-200:]}")
    data = json.loads(out or b"{}")
    return data.get("streams") or [], data.get("format") or {}


_INPUT_ERRORS = ("Stream ends prematurely", "Error during demuxing", "Input/output error",
                 "Connection reset by peer", "Invalid data found")


async def extract(source: str, background: bool, picks: list, workdir: Path, speech_out: Optional[Path],
                  item_id: str = "") -> dict:
    """One ffmpeg over the whole file: each picked stream to `<index>.srt`
    in `workdir`, and the sound's speech pattern to `speech_out` (16 kHz
    power frames, made into a pattern by subtitle_sync). {index: path}.
    Raises when the read stopped short: nothing half-read is kept."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
    cmd += _input_args(source, background)
    outs = {}
    for index, _ in picks:
        path = workdir / f"{index}.srt"
        cmd += ["-map", f"0:{index}", "-c:s", "srt", "-f", "srt", str(path)]
        outs[index] = path
    if speech_out is not None:
        # The voice band's power, 100 frames a second (subtitle_sync.RATE).
        cmd += ["-map", "0:a:0?", "-vn", "-ac", "1", "-af",
                "highpass=f=200,lowpass=f=3400,aeval=val(0)*val(0):c=same,aresample=100",
                "-ar", "100", "-c:a", "pcm_f32le", "-f", "f32le", str(speech_out)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    errors = []

    async def read_progress():
        async for line in proc.stdout:
            m = re.match(rb"out_time_us=(\d+)", line)
            if m and item_id in progress:
                progress[item_id]["at"] = int(m.group(1)) / 1e6

    async def read_errors():
        async for line in proc.stderr:
            errors.append(line.decode(errors="replace").strip())

    try:
        await asyncio.wait_for(asyncio.gather(read_progress(), read_errors(), proc.wait()), READ_TIMEOUT_SECONDS)
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    bad = next((e for e in errors if any(x in e for x in _INPUT_ERRORS)), None)
    if proc.returncode != 0 or bad:
        raise RuntimeError(f"the read stopped short ({bad or f'ffmpeg exited {proc.returncode}'})")
    return outs


# ── the queue ───────────────────────────────────────────────────────────────

def queue(db, item_id: str, priority: int, name: str = "", force: bool = False) -> SubtitleJob:
    """Put a title in line (or move it up). A title already done for the
    household's languages isn't read again unless `force`."""
    item_id = item_id.replace("-", "").lower()
    job = db.query(SubtitleJob).filter(SubtitleJob.jellyfin_item_id == item_id).first()
    langs = watch_prefs.household_languages(db)
    if job is None:
        job = SubtitleJob(jellyfin_item_id=item_id, name=name or None, priority=priority, state="queued")
        db.add(job)
    else:
        covered = set((job.languages or "").split(",")) >= set(langs)
        if job.state == "done" and covered and not force:
            return job
        if job.state in ("done", "failed") or force:
            if job.state != "running":
                job.attempts = 0
                job.state = "queued"
                job.error = None
                job.queued_at = datetime.utcnow()
        job.priority = min(job.priority, priority)
        if name and not job.name:
            job.name = name
    db.commit()
    wake()
    return job


def wake() -> None:
    if _wake is not None:
        _wake.set()


def _episode_name(item: dict) -> str:
    if item.get("Type") == "Episode":
        return f"{item.get('SeriesName') or ''} S{item.get('ParentIndexNumber') or 0:02d}E{item.get('IndexNumber') or 0:02d}".strip()
    return f"{item.get('Name') or ''}" + (f" ({item['ProductionYear']})" if item.get("ProductionYear") else "")


def on_play(db, item_id: str) -> int:
    """Someone started a title: it first, then the next episodes of its
    season (subtitles); and its season's intros and credits
    (services/segments.py). Returns how many titles were queued."""
    import requests
    items = jellyfin_items(db, [item_id])
    if not items:
        return 0
    item = items[0]
    queue(db, item["Id"], WATCHING, _episode_name(item))
    n = 1
    if item.get("Type") == "Episode" and item.get("SeasonId"):
        url, key = _jellyfin(db)
        try:
            r = requests.get(f"{url}/Shows/{item['SeriesId']}/Episodes", params={"SeasonId": item["SeasonId"], "Fields": "Path"},
                             headers={"X-Emby-Token": key}, timeout=20)
            r.raise_for_status()
            eps = r.json().get("Items") or []
        except Exception as e:
            logger.info(f"[Subtitles] the season of {_episode_name(item)} not listed: {type(e).__name__}")
            eps = []
        ids = [e["Id"] for e in eps]
        at = ids.index(item["Id"]) if item["Id"] in ids else -1
        for e in eps[at + 1:at + 1 + SEASON_AHEAD]:
            queue(db, e["Id"], SEASON, _episode_name(e))
            n += 1
        from services import segments
        segments.on_play(db, item, eps)
    return n


def _next_job(db) -> Optional[SubtitleJob]:
    return (db.query(SubtitleJob).filter(SubtitleJob.state == "queued")
            .order_by(SubtitleJob.priority, SubtitleJob.queued_at).first())


async def run_job(job_id: int) -> None:
    """Get one title's subtitles ready (see the module's doc)."""
    db = SessionLocal()
    try:
        job = db.get(SubtitleJob, job_id)
        job.state, job.started_at, job.attempts = "running", datetime.utcnow(), (job.attempts or 0) + 1
        db.commit()
        item_id = job.jellyfin_item_id
        try:
            summary = await _do(db, job)
            job.state, job.error = "done", None
            logger.info(f"[Subtitles] {job.name or item_id}: {summary}")
        except Exception as e:
            why = str(e) or type(e).__name__
            job.error = why[:300]
            job.state = "failed" if (job.attempts or 0) >= MAX_ATTEMPTS else "queued"
            if job.state == "queued":
                job.priority = max(job.priority, SEASON)     # after what people are waiting for
                job.queued_at = datetime.utcnow()
            logger.warning(f"[Subtitles] {job.name or item_id}: not ready ({why})"
                           + (" — will try again" if job.state == "queued" else " — gave up"))
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        j = db.get(SubtitleJob, job_id)
        ev = None
        if j is not None:
            progress.pop(j.jellyfin_item_id, None)
            ev = _done_events.pop(j.jellyfin_item_id, None)
        db.close()
        if ev is not None:
            ev.set()


async def _do(db, job: SubtitleJob) -> str:
    from services import subtitle_fetch, subtitle_sync
    items = await asyncio.to_thread(jellyfin_items, db, [job.jellyfin_item_id])
    if not items:
        raise RuntimeError("Jellyfin doesn't know this title any more")
    item = items[0]
    job.name = job.name or _episode_name(item)
    media = local_path(item.get("Path") or "")
    if media is None:
        raise RuntimeError(f"its file isn't where Tentacle can see it ({item.get('Path')})")
    sources = item.get("MediaSources") or []
    src_path = (sources[0].get("Path") if sources else "") or ""
    title = relay_title(src_path, db) if media.suffix.lower() == ".strm" else None
    if media.suffix.lower() == ".strm" and title is None:
        raise RuntimeError("a .strm that doesn't play through Tentacle")
    background = title is not None
    source = f"{RELAY_BASE}/api/vod/{title}" if background else str(media)
    langs = watch_prefs.household_languages(db)
    have = sidecars(media)
    streams, fmt = await probe(source, background)
    picks = choose(streams, langs, have)
    covered = {k[0] for k in have} | {key[0] for _, key in picks}
    missing = [l for l in langs if l not in covered]
    speech = subtitle_sync.speech_path(job.jellyfin_item_id)
    need_speech = bool(missing) and not speech.exists()
    wrote = []
    job.method = "stream" if background else "file"
    if picks or need_speech:
        work = Path(os.getenv("DATA_DIR", "/data")) / "cache" / "subtitle-work" / job.jellyfin_item_id
        work.mkdir(parents=True, exist_ok=True)
        raw = work / "speech.pcm" if need_speech else None
        try:
            duration = float(fmt.get("duration") or 0)
        except ValueError:
            duration = 0.0
        progress[job.jellyfin_item_id] = {"at": 0.0, "duration": duration, "started": time.time()}
        try:
            outs = await extract(source, background, picks, work, raw, job.jellyfin_item_id)
            job.bytes_read = int(fmt["size"]) if str(fmt.get("size") or "").isdigit() else None
            if raw is not None and raw.exists():
                await asyncio.to_thread(subtitle_sync.save_speech, raw, speech)
            for index, key in picks:
                out = outs[index]
                if not out.exists() or out.stat().st_size == 0:
                    continue
                dest = sidecar_path(media, *key)
                await asyncio.to_thread(_place, out, dest)
                _record(db, job.jellyfin_item_id, dest, key, "embedded", str(index))
                wrote.append(watch_prefs.name(key[0]) + (" SDH" if key[1] else "") + (" forced" if key[2] else ""))
        finally:
            for f in work.glob("*"):
                f.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass
    fetched = []
    for lang in missing:
        got = await subtitle_fetch.fetch(db, item, media, lang)
        if got:
            fetched.append(got)
    job.languages = ",".join(langs)
    db.commit()
    if wrote or fetched:
        await asyncio.to_thread(tell_jellyfin, db, job.jellyfin_item_id)
    parts = []
    if wrote:
        parts.append("from the file: " + ", ".join(wrote))
    if fetched:
        parts.append("fetched: " + ", ".join(fetched))
    if not parts:
        parts.append("nothing new (already beside it, or no subtitles in "
                     + ", ".join(watch_prefs.name(l) for l in langs) + ")")
    return "; ".join(parts) + (f" ({job.bytes_read / 1e6:.0f} MB read)" if job.bytes_read else "")


def _place(src: Path, dest: Path) -> None:
    """Copy into place through a temporary name (a scan never sees half a file)."""
    tmp = dest.with_name("." + dest.name + ".tmp")
    tmp.write_bytes(src.read_bytes())
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)


def _record(db, item_id: str, dest: Path, key: tuple, source: str, ref: str, shifted_ms: Optional[int] = None) -> None:
    row = db.query(SubtitleFile).filter(SubtitleFile.path == str(dest)).first()
    if row is None:
        row = SubtitleFile(path=str(dest))
        db.add(row)
    row.jellyfin_item_id, row.language, row.hearing_impaired, row.forced = item_id, key[0], key[1], key[2]
    row.source, row.source_ref, row.shifted_ms, row.created_at = source, ref, shifted_ms, datetime.utcnow()
    db.commit()


def tell_jellyfin(db, item_id: str) -> None:
    """Ask Jellyfin to look at the title again, so it lists the new files
    (no new metadata, no images; the plugin keeps a provider title from
    being probed again)."""
    import requests
    url, key = _jellyfin(db)
    if not url or not key:
        return
    try:
        requests.post(f"{url}/Items/{item_id}/Refresh", params={
            "metadataRefreshMode": "Default", "imageRefreshMode": "None",
            "replaceAllMetadata": "false", "replaceAllImages": "false"}, headers={"X-Emby-Token": key}, timeout=20)
    except Exception as e:
        logger.info(f"[Subtitles] Jellyfin not told about {item_id}: {type(e).__name__}")


# ── the worker ──────────────────────────────────────────────────────────────

async def _worker() -> None:
    global _wake
    _wake = asyncio.Event()
    db = SessionLocal()
    try:
        # A read cut by a restart starts over.
        for job in db.query(SubtitleJob).filter(SubtitleJob.state == "running").all():
            job.state = "queued"
        db.commit()
    finally:
        db.close()
    while True:
        db = SessionLocal()
        try:
            job = _next_job(db)
            job_id = job.id if job is not None else None
        finally:
            db.close()
        if job_id is None:
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), 600)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await run_job(job_id)
        except Exception as e:      # the worker outlives any one title
            logger.error(f"[Subtitles] job {job_id} failed: {e}")
            await asyncio.sleep(5)


def start() -> None:
    """Start the worker (main.py's lifespan)."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.get_running_loop().create_task(_worker())


async def wait_for(db, item_id: str, timeout: float) -> Optional[SubtitleJob]:
    """Put the title first in line and wait (at most `timeout`) until its job ends."""
    item_id = item_id.replace("-", "").lower()
    job = queue(db, item_id, ASKED)
    if job.state == "done":
        return job
    ev = _done_events.setdefault(item_id, asyncio.Event())
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    db.expire_all()
    return db.query(SubtitleJob).filter(SubtitleJob.jellyfin_item_id == item_id).first()


def status(db, item_id: str) -> dict:
    """Where a title's subtitles are: for the player's "getting subtitles" note."""
    item_id = item_id.replace("-", "").lower()
    job = db.query(SubtitleJob).filter(SubtitleJob.jellyfin_item_id == item_id).first()
    files = db.query(SubtitleFile).filter(SubtitleFile.jellyfin_item_id == item_id).all()
    out = {"state": job.state if job else "none", "files": [
        {"language": f.language, "name": watch_prefs.name(f.language), "hearing_impaired": bool(f.hearing_impaired),
         "forced": bool(f.forced), "source": f.source} for f in files]}
    if job is not None and job.state == "queued":
        ahead = db.query(SubtitleJob).filter(SubtitleJob.state.in_(("queued", "running")),
                                             (SubtitleJob.priority < job.priority)
                                             | ((SubtitleJob.priority == job.priority)
                                                & (SubtitleJob.queued_at < job.queued_at))).count()
        out["ahead"] = ahead
    p = progress.get(item_id)
    if p and job is not None and job.state == "running":
        took = time.time() - p["started"]
        out["seconds"] = round(took)
        if p["duration"] and p["at"] > 0:
            done = min(1.0, p["at"] / p["duration"])
            out["percent"] = round(done * 100)
            if done > 0.02:
                out["seconds_left"] = round(took / done - took)
    return out


async def get_another(db, item_id: str, lang: str) -> Optional[str]:
    """"Subtitles wrong? Get another": the next `lang` subtitle from the
    site, lined up with the sound. A provider title whose speech pattern
    isn't kept yet is read for it first (background slot)."""
    from services import subtitle_fetch, subtitle_sync
    item_id = item_id.replace("-", "").lower()
    items = await asyncio.to_thread(jellyfin_items, db, [item_id])
    if not items:
        return None
    item = items[0]
    media = local_path(item.get("Path") or "")
    if media is None:
        return None
    speech = subtitle_sync.speech_path(item_id)
    if not speech.exists():
        sources = item.get("MediaSources") or []
        title = relay_title((sources[0].get("Path") if sources else "") or "", db) if media.suffix.lower() == ".strm" else None
        source = f"{RELAY_BASE}/api/vod/{title}" if title else str(media)
        work = Path(os.getenv("DATA_DIR", "/data")) / "cache" / "subtitle-work" / (item_id + "-speech")
        work.mkdir(parents=True, exist_ok=True)
        raw = work / "speech.pcm"
        try:
            await extract(source, title is not None, [], work, raw, item_id)
            await asyncio.to_thread(subtitle_sync.save_speech, raw, speech)
        except Exception as e:
            logger.info(f"[Subtitles] {item.get('Name')}: sound not read for lining up ({e})")
        finally:
            raw.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass
    got = await subtitle_fetch.fetch(db, item, media, lang, another=True)
    if got:
        await asyncio.to_thread(tell_jellyfin, db, item_id)
        logger.info(f"[Subtitles] {item.get('Name')}: another subtitle — {got}")
    return got


ADDED_KEY = "subtitles_added_checked_at"


def queue_added(db) -> int:
    """Downloaded titles (real files) that came into Jellyfin since the last
    look: their subtitles are read now, from disk (cheap), so nobody waits
    later. The first look only notes the time: the library already there
    is read when someone watches it."""
    import requests
    from models.database import set_setting
    url, key = _jellyfin(db)
    if not url or not key:
        return 0
    since = get_setting(db, ADDED_KEY, "") or ""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    if not since:
        set_setting(db, ADDED_KEY, now)
        return 0
    r = requests.get(f"{url}/Items", params={
        "IncludeItemTypes": "Movie,Episode", "Recursive": "true", "SortBy": "DateCreated", "SortOrder": "Descending",
        "Fields": "Path,DateCreated", "Limit": 200, "EnableImages": "false", "EnableUserData": "false"},
        headers={"X-Emby-Token": key}, timeout=30)
    r.raise_for_status()
    n = 0
    for item in r.json().get("Items") or []:
        if (item.get("DateCreated") or "")[:19] <= since:
            break
        if (item.get("Path") or "").lower().endswith(".strm"):
            continue             # a provider title is read when someone starts it
        queue(db, item["Id"], ADDED, _episode_name(item))
        n += 1
    set_setting(db, ADDED_KEY, now)
    if n:
        logger.info(f"[Subtitles] {n} new downloaded title(s) queued")
    return n


def queue_added_job() -> None:
    """The scheduler's entry (its own session)."""
    db = SessionLocal()
    try:
        queue_added(db)
    except Exception as e:
        logger.info(f"[Subtitles] new titles not checked: {type(e).__name__}")
    finally:
        db.close()


def srt_to_vtt(text: str) -> str:
    """WebVTT for a browser's <track>: the same cues, a header and dots for commas."""
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text.replace("\ufeff", "").replace("\r\n", "\n"))
    return "WEBVTT\n\n" + body.strip() + "\n"
