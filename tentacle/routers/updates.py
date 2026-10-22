"""
Tentacle - Live updates for the open dashboard (services/live_updates.py)
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from models.database import TentacleUser
from routers.auth import get_user_from_request
from services import live_updates

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("")
async def updates(request: Request, user: TentacleUser = Depends(get_user_from_request)):
    """Server-sent events for this person's open tab: "change" notes by topic
    (requests, activity, people, health, home, ...). The tab re-reads what it
    shows for a topic; on "hello" (a new connection) it re-reads everything."""
    return StreamingResponse(
        live_updates.stream(user.id, bool(user.is_admin), request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
