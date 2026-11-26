"""Skip intro and Skip credits: where they are, for streams and downloads.

Why: nothing found them on this server (no intro plugin, Jellyfin's
MediaSegments table empty), so no app ever offered Skip intro, and the
prompt late in the credits was Jellyfin's next-episode timer.

When someone starts an episode, it, the one before it and the next
episodes of its season (SEASON_AHEAD) are queued. For each, one title at a
time (`_worker`):
- ffprobe reads its chapters (the file's head): chapters named like an
  intro or the credits are used as they are.
- Its first minutes (HEAD_S) and last minutes (TAIL_S) are made into audio
  fingerprints, and the last minutes' keyframes into how dark each picture
  is. A provider title is read through the relay on the "background" slot
  (routers/vod.py BACKGROUND_HEADER): only those minutes (ffmpeg stops
  after the head, and seeks to the tail with a Range request), the lowest
  rank, a viewer takes the slot first. A downloaded file is read from disk.
  Fingerprints are kept in /data/cache/fingerprints, so each episode is read
  once and compared with every other.
- After each read, the whole season is worked out again
  (services/segment_detect.py): an episode's intro is what its opening
  shares with another episode's; its credits what its end shares, else
  where the end turns dark. Times are per copy (each Jellyfin item).
- The plugin publishes them as Jellyfin media segments
  (`/Tentacle/Segments/Refresh` → its IMediaSegmentProvider reads
  `/api/segments/{item}`), so Jellyfin Web, the phone apps and the TV app
  show Skip intro / Skip credits, and next-episode comes at the credits.
"""
import asyncio
import json
import logging
import os
import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.database import MediaSegment, SegmentJob, SessionLocal, get_setting
from services import segment_detect as D

logger = logging.getLogger(__name__)

HEAD_FRACTION, HEAD_MAX_S, HEAD_MIN_S = 0.25, 360, 90
TAIL_FRACTION, TAIL_MAX_S = 0.25, 360
SEASON_AHEAD = 6
WATCHING, SEASON = 1, 2
MAX_ATTEMPTS = 3
READ_TIMEOUT_SECONDS = 3600

_wake: Optional[asyncio.Event] = None
_worker_task: Optional[asyncio.Task] = None


def _dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "cache" / "fingerprints"


def _save_points(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw[:len(raw) - len(raw) % 4])
    os.replace(tmp, path)


def _points(path: Path) -> list:
    try:
        b = path.read_bytes()
    except OSError:
        return []
    return list(struct.unpack(f"<{len(b) // 4}I", b[:len(b) - len(b) % 4]))


def queue(db, item_id: str, priority: int, season_id: Optional[str] = None, name: str = "") -> SegmentJob:
    item_id = item_id.replace("-", "").lower()
    job = db.query(SegmentJob).filter(SegmentJob.jellyfin_item_id == item_id).first()
    if job is None:
        job = SegmentJob(jellyfin_item_id=item_id, season_id=(season_id or "").replace("-", "").lower() or None,
                         name=name or None, priority=priority, state="queued")
        db.add(job)
    elif job.state == "failed" and (job.attempts or 0) < MAX_ATTEMPTS:
        job.state = "queued"
    else:
        job.priority = min(job.priority, priority)
    db.commit()
    if _wake is not None:
        _wake.set()
    return job


def on_play(db, item: dict, season_items: list) -> int:
    """Someone started `item` (an episode: its season's other episodes in
    `season_items`, in order): it, the one before (a partner to compare
    with) and the next SEASON_AHEAD."""
    from services.subtitles import _episode_name
    if item.get("Type") != "Episode" or not item.get("SeasonId"):
        return 0
    queue(db, item["Id"], WATCHING, item["SeasonId"], _episode_name(item))
    ids = [e["Id"] for e in season_items]
    at = ids.index(item["Id"]) if item["Id"] in ids else 0
    n = 1
    for e in season_items[max(0, at - 1):at] + season_items[at + 1:at + 1 + SEASON_AHEAD]:
        queue(db, e["Id"], SEASON, item["SeasonId"], _episode_name(e))
        n += 1
    return n


_BLACK = re.compile(r"pblack:(\d+).*?\bt:([\d.]+)")


async def read_ends(source: str, background: bool, duration: float, item_id: str) -> tuple:
    """Fingerprint the first and last minutes (and how dark the last
    minutes' keyframes are). (head points, tail points, tail start s, [(t, dark %)])."""
    from services.subtitles import _input_args
    head_s = max(HEAD_MIN_S, min(HEAD_MAX_S, duration * HEAD_FRACTION))
    tail_s = min(TAIL_MAX_S, duration * TAIL_FRACTION)
    tail_start = max(0.0, duration - tail_s)
    work = _dir() / f"{item_id}.work"
    work.mkdir(parents=True, exist_ok=True)
    head, tail = work / "head.raw", work / "tail.raw"
    try:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-t", f"{head_s:.1f}"]
        cmd += _input_args(source, background)
        cmd += ["-map", "0:a:0", "-ac", "2", "-f", "chromaprint", "-fp_format", "raw", str(head)]
        await _ffmpeg(cmd)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-y", "-ss", f"{tail_start:.1f}",
               "-skip_frame", "nokey"]
        cmd += _input_args(source, background)
        cmd += ["-map", "0:a:0", "-ac", "2", "-f", "chromaprint", "-fp_format", "raw", str(tail),
                "-map", "0:v:0?", "-vf", "blackframe=amount=0:threshold=32", "-f", "null", "-"]
        log = await _ffmpeg(cmd)
        frames = [(tail_start + float(m.group(2)), int(m.group(1))) for m in _BLACK.finditer(log)]
        # ffmpeg's t: counts from where it started reading only when the seek is exact; keep it inside the window
        frames = [(min(t, duration), share) for t, share in frames]
        return head.read_bytes(), tail.read_bytes(), tail_start, frames
    finally:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass


async def _ffmpeg(cmd: list) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), READ_TIMEOUT_SECONDS)
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    text = (err or b"").decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {text.strip()[-200:]}")
    return text


async def probe(source: str, background: bool) -> tuple:
    """(duration s, chapters [(start, end, title)])."""
    from services.subtitles import _input_args, _run
    code, out, err = await _run(["ffprobe", "-v", "error", "-show_format", "-show_chapters", "-of", "json"]
                                + _input_args(source, background), 600)
    if code != 0:
        raise RuntimeError(f"ffprobe failed: {(err or b'').decode(errors='replace').strip()[-200:]}")
    data = json.loads(out or b"{}")
    duration = float((data.get("format") or {}).get("duration") or 0)
    chapters = [(float(c.get("start_time") or 0), float(c.get("end_time") or 0), (c.get("tags") or {}).get("title") or "")
                for c in data.get("chapters") or []]
    return duration, chapters


def _save(db, item_id: str, kind: str, span: Optional[tuple], source: str) -> bool:
    """Keep one segment; True when it changed."""
    row = db.query(MediaSegment).filter(MediaSegment.jellyfin_item_id == item_id, MediaSegment.kind == kind).first()
    if span is None:
        return False
    start_ms, end_ms = int(span[0] * 1000), int(span[1] * 1000)
    if row is not None and row.source == "chapters" and source != "chapters":
        return False                       # the file's own marks win
    if row is not None and abs(row.start_ms - start_ms) < 500 and abs(row.end_ms - end_ms) < 500 and row.source == source:
        return False
    if row is None:
        row = MediaSegment(jellyfin_item_id=item_id, kind=kind)
        db.add(row)
    row.start_ms, row.end_ms, row.source, row.created_at = start_ms, end_ms, source, datetime.utcnow()
    db.commit()
    return True


def work_out_season(db, season_id: str) -> list:
    """Every read episode of a season compared with the others; the items whose times changed."""
    jobs = [j for j in db.query(SegmentJob).filter(SegmentJob.season_id == season_id, SegmentJob.state == "done").all()
            if (_dir() / f"{j.jellyfin_item_id}.head").exists()]
    heads = {j.jellyfin_item_id: _points(_dir() / f"{j.jellyfin_item_id}.head") for j in jobs}
    tails = {j.jellyfin_item_id: _points(_dir() / f"{j.jellyfin_item_id}.tail") for j in jobs}
    changed = []
    for j in jobs:
        me = j.jellyfin_item_id
        # Nearest episodes first (queued in order: ids by job id)
        others = sorted((o for o in jobs if o.jellyfin_item_id != me), key=lambda o: abs(o.id - j.id))
        intro = credits = None
        for o in others:
            got = D.intro_between(heads[me], heads[o.jellyfin_item_id])
            if got and (intro is None or got[1] - got[0] > intro[1] - intro[0] + 2):
                intro = got
            got = D.credits_between(tails[me], float(j.tail_start_s or 0), tails[o.jellyfin_item_id])
            if got and (credits is None or got[1] - got[0] > credits[1] - credits[0] + 2):
                credits = got
            if intro and credits and others.index(o) >= 1:
                break
        ch = False
        if intro:
            ch |= _save(db, me, "intro", intro, "audio")
        if credits:
            ch |= _save(db, me, "credits", D.credits_end(credits, float(j.duration_s or credits[1])), "audio")
        if ch:
            changed.append(me)
    return changed


async def run_job(job_id: int) -> list:
    """Read one title's ends; returns the items whose segments changed."""
    from services import subtitles as S
    db = SessionLocal()
    changed = []
    try:
        job = db.get(SegmentJob, job_id)
        job.state, job.attempts = "running", (job.attempts or 0) + 1
        db.commit()
        me = job.jellyfin_item_id
        try:
            items = await asyncio.to_thread(S.jellyfin_items, db, [me])
            if not items:
                raise RuntimeError("Jellyfin doesn't know this title any more")
            item = items[0]
            media = S.local_path(item.get("Path") or "")
            if media is None:
                raise RuntimeError(f"its file isn't where Tentacle can see it ({item.get('Path')})")
            src = ((item.get("MediaSources") or [{}])[0].get("Path") or "")
            title = S.relay_title(src, db) if media.suffix.lower() == ".strm" else None
            if media.suffix.lower() == ".strm" and title is None:
                raise RuntimeError("a .strm that doesn't play through Tentacle")
            source = f"{S.RELAY_BASE}/api/vod/{title}" if title else str(media)
            duration, chapters = await probe(source, title is not None)
            if duration <= 0:
                raise RuntimeError("its length is unknown")
            job.duration_s = int(duration)
            marks = D.from_chapters(chapters, duration)
            for kind, span in marks.items():
                if _save(db, me, kind, D.credits_end(span, duration) if kind == "credits" else span, "chapters"):
                    changed.append(me)
            if not ({"intro", "credits"} <= set(marks)):
                head, tail, tail_start, frames = await read_ends(source, title is not None, duration, me)
                _save_points(_dir() / f"{me}.head", head)
                _save_points(_dir() / f"{me}.tail", tail)
                job.tail_start_s = int(tail_start)
                black = D.credits_from_black(frames, duration)
                if black and "credits" not in marks:
                    existing = db.query(MediaSegment).filter(MediaSegment.jellyfin_item_id == me,
                                                             MediaSegment.kind == "credits").first()
                    if existing is None and _save(db, me, "credits", black, "black"):
                        changed.append(me)
            job.state, job.error, job.finished_at = "done", None, datetime.utcnow()
            db.commit()
            if job.season_id:
                changed += work_out_season(db, job.season_id)
            found = {r.kind: (r.start_ms / 1000, r.end_ms / 1000, r.source)
                     for r in db.query(MediaSegment).filter(MediaSegment.jellyfin_item_id == me).all()}
            logger.info(f"[Skip] {job.name or me}: " + (", ".join(f"{k} {a:.0f}-{b:.0f} s ({s})" for k, (a, b, s) in found.items())
                                                       or "nothing found yet (needs another episode to compare)"))
        except Exception as e:
            job.error = (str(e) or type(e).__name__)[:300]
            job.state = "failed" if (job.attempts or 0) >= MAX_ATTEMPTS else "queued"
            job.finished_at = datetime.utcnow()
            db.commit()
            logger.warning(f"[Skip] {job.name or me}: not read ({job.error})")
        return sorted(set(changed))
    finally:
        db.close()


def tell_jellyfin(db, item_ids: list) -> None:
    """The plugin publishes these items' segments to Jellyfin (its provider reads /api/segments)."""
    import requests
    url, key = ((get_setting(db, "jellyfin_url", "") or "").rstrip("/"), get_setting(db, "jellyfin_api_key", "") or "")
    if not url or not key or not item_ids:
        return
    try:
        requests.post(f"{url}/Tentacle/Segments/Refresh", params={"itemIds": ",".join(item_ids)},
                      headers={"X-Emby-Token": key}, timeout=30)
    except Exception as e:
        logger.info(f"[Skip] Jellyfin not told about new segments: {type(e).__name__}")


async def _worker() -> None:
    global _wake
    _wake = asyncio.Event()
    db = SessionLocal()
    try:
        for job in db.query(SegmentJob).filter(SegmentJob.state == "running").all():
            job.state = "queued"
        db.commit()
    finally:
        db.close()
    while True:
        db = SessionLocal()
        try:
            job = (db.query(SegmentJob).filter(SegmentJob.state == "queued")
                   .order_by(SegmentJob.priority, SegmentJob.queued_at, SegmentJob.id).first())
            job_id = job.id if job else None
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
            changed = await run_job(job_id)
            if changed:
                db = SessionLocal()
                try:
                    await asyncio.to_thread(tell_jellyfin, db, changed)
                finally:
                    db.close()
        except Exception as e:
            logger.error(f"[Skip] job {job_id} failed: {e}")
            await asyncio.sleep(5)


def start() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.get_running_loop().create_task(_worker())


def for_item(db, item_id: str) -> list:
    """The item's segments, for the plugin's provider."""
    item_id = item_id.replace("-", "").lower()
    return [{"type": "Intro" if r.kind == "intro" else "Outro", "start_ms": r.start_ms, "end_ms": r.end_ms,
             "source": r.source}
            for r in db.query(MediaSegment).filter(MediaSegment.jellyfin_item_id == item_id).order_by(MediaSegment.start_ms)]
