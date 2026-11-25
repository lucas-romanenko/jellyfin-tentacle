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
def item(item_id: str, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """What the Offline page keeps about a title, if this person may download it."""
    try:
        return offline.summary(offline.check(db, user, item_id, fresh=True))
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


@router.get("/api/offline/items/{item_id}/video")
async def video(item_id: str, request: Request, db: Session = Depends(get_db),
                user: TentacleUser = Depends(get_user_from_request)):
    """The title's video, byte ranges passed through (Jellyfin's Download)."""
    try:
        await asyncio.to_thread(offline.check, db, user, item_id)
    except F.FamilyError as e:
        raise _refusal(e)
    url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        raise HTTPException(409, "Jellyfin isn't connected yet.")
    headers = {"X-Emby-Token": key}
    for name in ("range", "if-range"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    # No read timeout: a provider's title waits, sending nothing, while
    # someone watching has its connection (routers/vod.py).
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    iid = offline.norm_item_id(item_id)
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
