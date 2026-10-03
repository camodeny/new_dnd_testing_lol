"""Lobby projection (#241) and shared OOC lobby chat (#243)."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.campaigns import lobby_chat
from app.campaigns.members import lobby_projection
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for
from app.deps.idempotency import command_keys, execute_http_idempotent
from database import get_db
from models.campaigns import Campaign

router = APIRouter()


@router.get("/api/campaigns/{campaign_id}/lobby")
def get_campaign_lobby(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    """Authoritative lobby projection — issue #241."""
    return lobby_projection(db, campaign, profile.id)


@router.get("/api/campaigns/{campaign_id}/lobby/chat")
def get_lobby_chat(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("participant")),
    db: Session = Depends(get_db),
):
    """Lobby OOC snapshot — issue #243.

    The lobby thread plus its ordered OOC history and the private Realtime
    channel to subscribe to. Members only; removed/non-member users get 403.
    """
    return lobby_chat.lobby_chat_snapshot(db, campaign, profile.id)


@router.post("/api/campaigns/{campaign_id}/lobby/chat", status_code=201)
def post_lobby_chat(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("participant")),
    db: Session = Depends(get_db),
):
    """Post one OOC lobby message — issue #243.

    Member-only, idempotent (``Idempotency-Key`` header or ``operation_id``),
    writable only while pre-start (``lobby``/``starting``). The message is
    forced OOC and stored via the shared submission infrastructure, then
    projected best-effort to Realtime. This endpoint deliberately never
    coordinates a forward DM turn, bumps the campaign revision, appends
    domain events, or touches clocks/world state.
    """
    thread, content = lobby_chat.writable_lobby_thread(db, campaign, profile.id, payload)
    _, idempotency_key = command_keys(request, payload)
    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="lobby_chat.post", scope_type="campaign_lobby", scope_id=f"{campaign.id}:{thread.id}",
        payload=payload,
        execute=lambda: lobby_chat.post_lobby_chat(db, campaign.id, thread.id, user_id=profile.id, content=content),
    )
    lobby_chat.publish_lobby_message(db, result, campaign_id=campaign.id, thread_id=thread.id)
    return result
