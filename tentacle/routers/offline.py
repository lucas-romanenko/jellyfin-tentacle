"""The Offline page's routes (services/offline.py): may this person
download a title, its picture, its video, and what they watched offline.

The video is Jellyfin's own Download of the title, passed on with its byte
ranges: a download that was interrupted asks again from the byte it got
to. For a provider's title that is the real video through Tentacle's relay
on a "download" slot (the plugin's StrmDownloadFilter, routers/vod.py),
which gives way to anyone who wants to watch.
"""
import asyncio
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_db, get_setting
from routers.auth import get_user_from_request
from routers.vod import _VodResponse, _pending_releases
from services import family as F
from services import offline
from services import offline_convert as C
from services import offline_saves as S

logger = logging.getLogger(__name__)
router = APIRouter()

# Passed on from Jellyfin's answer to the browser.
_PASS_HEADERS = ("content-length", "content-range", "accept-ranges", "content-type", "etag", "last-modified")


def _refusal(e: F.FamilyError) -> HTTPException:
    return HTTPException(e.status, e.message)


@router.get("/api/offline/where")
def where(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """For Jellyfin's pages (the plugin's Offline link and Download menu):
    where this person's Offline page is, and whether they may download."""
    tent = F.addresses(db).get("tentacle") or ""
    return {"url": f"{tent}/#offline" if tent else "", "can_download": offline.can_download(db, user)}


@router.get("/api/offline/items/{item_id}")
def item(item_id: str, season: Optional[int] = None, db: Session = Depends(get_db),
         user: TentacleUser = Depends(get_user_from_request)):
    """What the Offline page keeps about a title, if this person may save it.
    A show or a season: {"kind": "show", "name", "episodes": [...]}, every
    episode they may save (`season`: only that season of the show)."""
    try:
        got = offline.check(db, user, item_id, fresh=True, folders=True)
        if got.get("IsFolder"):
            return offline.episodes(db, user, got, season)
        return offline.summary(got)
    except F.FamilyError as e:
        raise _refusal(e)


@router.get("/api/offline/items/{item_id}/poster")
def poster(item_id: str, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    try:
        got = offline.poster(db, user, item_id)
    except F.FamilyError as e:
        raise _refusal(e)
    if got is None:
        raise HTTPException(404, "No picture")
    return Response(content=got[0], media_type=got[1], headers={"cache-control": "private, max-age=86400"})


@router.get("/api/offline/items/{item_id}/subtitles")
def subtitles(item_id: str, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """The subtitles a saved title takes with it: the files beside it in this
    person's languages, as WebVTT (what a browser's player shows), and whether
    they show by themselves; and its intro and end credits with the person's
    skip choices. A title whose subtitles aren't ready yet is put in
    line ("ready": false; the Offline page asks again later)."""
    from services import subtitles as S, watch_prefs
    try:
        offline.check(db, user, item_id)
    except F.FamilyError as e:
        raise _refusal(e)
    prefs = watch_prefs.for_person(db, user)
    try:
        items = S.jellyfin_items(db, [item_id])
    except Exception:
        items = []
    media = S.local_path(items[0].get("Path") or "") if items else None
    tracks = []
    if media is not None:
        found = S.sidecars(media)
        for lang in prefs["subtitle_languages"]:
            for (code, hi, forced), path in sorted(found.items(), key=lambda kv: (kv[0][1], kv[0][2])):
                if code != lang or path.suffix.lower() != ".srt":
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                tracks.append({"language": code, "label": watch_prefs.name(code) + (" SDH" if hi else "")
                               + (" (forced)" if forced else ""), "vtt": S.srt_to_vtt(text)})
    job = S.queue(db, item_id, S.WATCHING) if items else None
    from services import segments
    return {"tracks": tracks, "ready": job is None or job.state == "done", "mode": prefs["subtitle_mode"],
            # Skip intro / Skip credits go with it too, with the person's choice for each.
            "segments": segments.for_item(db, item_id),
            "skip": {"Intro": prefs["skip_intro"], "Outro": prefs["skip_credits"]}}


@router.get("/api/offline/items/{item_id}/video")
async def video(item_id: str, request: Request, can: str = "", device: str = "pc", db: Session = Depends(get_db),
                user: TentacleUser = Depends(get_user_from_request)):
    """The title's video as this device takes it (`can`: the optional codecs
    its browser said it plays; `device`: "phone" for the smaller copy). A
    file on the server that it plays as it is: Jellyfin's Download, byte
    ranges passed through. Anything else: the copy Tentacle makes of it
    (services/offline_convert.py, started by POST /api/offline/saves), a
    normal file once finished (read as it grows before)."""
    try:
        got = await asyncio.to_thread(offline.check, db, user, item_id)
    except F.FamilyError as e:
        raise _refusal(e)
    url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        raise HTTPException(409, "Jellyfin isn't connected yet.")
    iid = offline.norm_item_id(item_id)
    caps = C.caps_of(can)
    phone = device == "phone"
    try:
        if not S.as_is(got, phone, caps):
            job = S.start_job(db, got, phone, caps, user.display_name)
            if not job.done:
                # Its size once it has looked inside (the page shows "of about ...")
                try:
                    await asyncio.wait_for(job.probed.wait(), 60)
                except asyncio.TimeoutError:
                    pass
            return _made(job, request)
    except F.FamilyError as e:
        raise _refusal(e)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, C.BROKE)
    headers = {"X-Emby-Token": key}
    for name in ("range", "if-range"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    # No read timeout: a provider's title waits, sending nothing, while
    # someone watching has its connection (routers/vod.py).
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    try:
        resp = await client.send(client.build_request("GET", f"{url}/Items/{iid}/Download", headers=headers),
                                 stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        logger.info(f"[Offline] {user.display_name}: Jellyfin didn't answer for {iid}: {type(e).__name__}")
        raise HTTPException(502, "Jellyfin can't be reached right now.")
    if resp.status_code not in (200, 206):
        status = resp.status_code
        try:
            text = (await resp.aread()).decode("utf-8", "replace").strip()
        finally:
            await resp.aclose()
            await client.aclose()
        logger.info(f"[Offline] {user.display_name}: Jellyfin answered {status} for the video of {iid}")
        if status == 503:
            raise HTTPException(503, text[:300] if text and not text.startswith(("{", "<")) else
                                "The server is busy right now. The download carries on when it can.")
        if status == 416:
            raise HTTPException(416, "Nothing more to download.")
        raise HTTPException(502 if status >= 500 else status, "Jellyfin didn't send the video.")
    if ".strm" in (resp.headers.get("content-disposition") or "").lower():
        # Jellyfin sent the .strm text: its Tentacle plugin predates the Download hook
        await resp.aclose()
        await client.aclose()
        logger.warning(f"[Offline] {user.display_name}: Jellyfin sent the .strm of {iid}, not the video: "
                       f"the Tentacle plugin in Jellyfin is older than the Download hook (StrmDownloadFilter)")
        raise HTTPException(409, "Downloading streams needs a newer Tentacle plugin in Jellyfin. "
                                 "Ask whoever runs your server to update it.")
    passthrough = {k: v for k, v in resp.headers.items() if k.lower() in _PASS_HEADERS}
    logger.info(f"[Offline] {user.display_name}: downloading {iid} "
                f"({request.headers.get('range') or 'from the start'})")

    def finish():
        task = asyncio.get_running_loop().create_task(_close(resp, client))
        _pending_releases.add(task)
        task.add_done_callback(_pending_releases.discard)

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except httpx.HTTPError as e:
            # Ends short of its Content-Length: the browser sees the break
            # and asks again from where it got to.
            logger.info(f"[Offline] {user.display_name}: {iid} broke off: {type(e).__name__}")

    return _VodResponse(body(), status_code=resp.status_code, headers=passthrough, finish=finish)


def _made(job: C.Job, request: Request):
    """The MP4 Tentacle is making (or made): finished, a normal file with
    its size and byte ranges; still being made, from the byte asked for to
    as far as it gets, with no size (X-Offline-From says where it starts).
    X-Offline-Job: which making this is (a browser holding part of another
    one starts again); X-Offline-Estimate: about how big it will be."""
    start, end = 0, None
    rng = (request.headers.get("range") or "").strip().lower()
    if rng.startswith("bytes="):
        a, _, b = rng[6:].split(",")[0].partition("-")
        try:
            start = int(a) if a else 0
            end = int(b) if b else None
        except ValueError:
            start, end = 0, None
    if job.error:
        raise HTTPException(502, job.error)
    headers = {"x-offline-job": job.gen, "x-offline-estimate": str(job.guess_total()),
               "accept-ranges": "bytes", "cache-control": "no-store"}
    if job.done:
        total = job.size
        if start >= total:
            raise HTTPException(416, "Nothing more to save.")
        last = total - 1 if end is None else min(end, total - 1)
        headers["content-length"] = str(last - start + 1)
        status = 200
        if rng:
            status = 206
            headers["content-range"] = f"bytes {start}-{last}/{total}"
        body = C.read(job, start, last)
    else:
        status = 206 if start else 200
        headers["x-offline-from"] = str(start)
        body = C.read(job, start, end)

    def finish():
        # Closed with the response, even when the browser went away before
        # the body began (its reader count must come down).
        task = asyncio.get_running_loop().create_task(body.aclose())
        _pending_releases.add(task)
        task.add_done_callback(_pending_releases.discard)

    return _VodResponse(body, status_code=status, media_type="video/mp4", headers=headers, finish=finish)


async def _close(resp, client):
    try:
        await resp.aclose()
    finally:
        await client.aclose()


class ProgressEntry(BaseModel):
    id: str
    position_ticks: Optional[int] = 0
    played: Optional[bool] = False
    at: Optional[float] = None


class ProgressBody(BaseModel):
    items: List[ProgressEntry] = []


@router.post("/api/offline/progress")
def progress(body: ProgressBody, db: Session = Depends(get_db),
             user: TentacleUser = Depends(get_user_from_request)):
    """What was watched offline, saved in Jellyfin now the device is online."""
    if len(body.items) > 200:
        raise HTTPException(400, "Too many at once")
    try:
        return offline.sync_progress(db, user, [e.model_dump() for e in body.items])
    except F.FamilyError as e:
        raise _refusal(e)


# ── Save offline: the server makes the copy first ─────────────────────────

class SaveBody(BaseModel):
    items: List[str] = []
    device: str = "pc"          # phone | pc (a tablet is a phone)
    can: str = ""
    push_endpoint: Optional[str] = None


@router.post("/api/offline/saves")
def save(body: SaveBody, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Save offline tapped on a device (services/offline_saves.py): for each
    title, ready now, or the server makes its copy first and says when it is
    ready. A title this person may not save: {"state": "refused", "error"}."""
    if len(body.items) > 300:
        raise HTTPException(400, "Too many at once")
    phone = body.device == "phone"
    caps = C.caps_of(body.can)
    out = []
    for item_id in body.items:
        try:
            out.append(S.request(db, user, item_id, phone, caps, body.push_endpoint or ""))
        except F.FamilyError as e:
            if e.status in (401, 403) and not out:
                raise _refusal(e)
            out.append({"id": offline.norm_item_id(item_id), "state": "refused", "error": e.message})
    return {"items": out}


@router.get("/api/offline/saves")
def saves(ids: str = "", device: str = "pc", can: str = "", db: Session = Depends(get_db),
          user: TentacleUser = Depends(get_user_from_request)):
    """How far the server has got with this person's titles (`ids`, comma-separated)."""
    wanted = [i for i in ids.split(",") if i.strip()]
    return {"items": S.status(db, user, wanted, device == "phone", C.caps_of(can))}


@router.delete("/api/offline/saves/{item_id}")
def saved(item_id: str, device: str = "pc", can: str = "", db: Session = Depends(get_db),
          user: TentacleUser = Depends(get_user_from_request)):
    """The device has the title now, or it was removed there: the server's copy may go."""
    return {"forgotten": S.forget(db, user, item_id, device == "phone", C.caps_of(can))}


class PushBody(BaseModel):
    endpoint: str
    keys: dict = {}


@router.get("/api/offline/push")
def push_key(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """The server's public push key, for the browser's subscription."""
    from services import web_push
    return {"key": web_push.public_key(db)}


@router.post("/api/offline/push")
def push_subscribe(body: PushBody, db: Session = Depends(get_db),
                   user: TentacleUser = Depends(get_user_from_request)):
    """This browser may show this person's messages with no page open."""
    from services import web_push
    try:
        web_push.subscribe(db, user.id, body.endpoint, str(body.keys.get("p256dh") or ""),
                           str(body.keys.get("auth") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/api/offline/push")
def push_unsubscribe(body: PushBody, db: Session = Depends(get_db),
                     user: TentacleUser = Depends(get_user_from_request)):
    from services import web_push
    web_push.unsubscribe(db, user.id, body.endpoint)
    return {"ok": True}
