"""Private character setup lore (#244) and the lore-DM setup chat."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.campaigns import lore_dm_chat, party_lore
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, parse_uuid_or_404, require_expected_revision, run_campaign_command
from app.deps.idempotency import command_keys
from database import get_db
from models.campaigns import Campaign

router = APIRouter()

_INVALID_IDS = "Invalid campaign or character id"
_LORE_NOT_FOUND = "Character lore not found"
lore_campaign = campaign_for("participant", invalid=_INVALID_IDS)


@router.get("/api/campaigns/{campaign_id}/characters/{character_id}/lore")
def get_character_lore(
    character_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(lore_campaign),
    db: Session = Depends(get_db),
):
    """Read own private setup lore — issue #244.

    Fail-closed: another player's row (even for the campaign owner) returns
    404 with no existence leak. Reads survive the start transition.
    """
    char_id = parse_uuid_or_404(character_id, _INVALID_IDS)
    party_lore.require_own_character(db, char_id, profile.id, status_code=404, detail=_LORE_NOT_FOUND)
    row = party_lore.get_own_lore(db, campaign_id=campaign.id, character_id=char_id, user_id=profile.id)
    return {"lore": row.to_dict(include_content=True)}


@router.put("/api/campaigns/{campaign_id}/characters/{character_id}/lore")
def put_character_lore(
    character_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(lore_campaign),
    db: Session = Depends(get_db),
):
    """Create/update own private setup lore — issue #244.

    Lobby-only, idempotent, versioned (retries bump nothing when content is
    identical). Logs/observability record only lengths/versions, never raw
    secret content.
    """
    char_id = parse_uuid_or_404(character_id, _INVALID_IDS)
    content = party_lore.validate_lore_content(payload)
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    party_lore.require_own_character(
        db, char_id, profile.id, status_code=403, detail="Only your own character's lore can be edited",
    )
    party_lore.require_lore_writable(campaign)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.character.lore.put",
        scope_type="campaign_character_lore", scope_id=f"{campaign.id}:{char_id}:{profile.id}",
        # The idempotency helper persists only the SHA-256 digest of this
        # payload, so the actual content belongs here: same key + different
        # same-length lore must 409 rather than silently replay the old write.
        payload={
            "expected_revision": expected_revision,
            "operation_id": operation_id,
            "character_id": str(char_id),
            "content": content,
        },
        execute=lambda: party_lore.put_lore(
            db, campaign.id, character_id=char_id, user_id=profile.id, content=content,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.delete("/api/campaigns/{campaign_id}/characters/{character_id}/lore")
def delete_character_lore(
    character_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(lore_campaign),
    db: Session = Depends(get_db),
):
    """Remove own private setup lore before start — issue #244 (idempotent)."""
    char_id = parse_uuid_or_404(character_id, _INVALID_IDS)
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    party_lore.require_own_character(db, char_id, profile.id, status_code=404, detail=_LORE_NOT_FOUND)
    party_lore.require_lore_writable(campaign)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.character.lore.delete",
        scope_type="campaign_character_lore", scope_id=f"{campaign.id}:{char_id}:{profile.id}",
        payload=payload,
        execute=lambda: party_lore.delete_lore(
            db, campaign.id, character_id=char_id, user_id=profile.id,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.get("/api/campaigns/{campaign_id}/characters/{character_id}/lore-chat")
def get_lore_dm_chat(
    character_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(lore_campaign),
    db: Session = Depends(get_db),
):
    """Read own lore-DM setup thread (same no-oracle 404 rule as lore).

    Reads survive the start transition; only writes lock with the lobby.
    """
    char_id = parse_uuid_or_404(character_id, _INVALID_IDS)
    party_lore.require_own_character(db, char_id, profile.id, status_code=404, detail=_LORE_NOT_FOUND)
    messages = lore_dm_chat.list_lore_chat_messages(
        db, campaign_id=campaign.id, character_id=char_id, user_id=profile.id,
    )
    return {"messages": messages}


@router.post("/api/campaigns/{campaign_id}/characters/{character_id}/lore-chat")
def post_lore_dm_chat(
    character_id: str,
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(lore_campaign),
    db: Session = Depends(get_db),
):
    """One guided lore-DM turn — lobby-only.

    Saves the player's message, streams the DM reply (tokens + at most one
    lore proposal). The DM side is advisory only: proposals become canon
    solely through the standard lore PUT. Locks with lore writes (409) once
    the campaign leaves the lobby.
    """
    char_id = parse_uuid_or_404(character_id, _INVALID_IDS)
    content = lore_dm_chat.validate_lore_chat_content(payload)
    char = party_lore.require_own_character(db, char_id, profile.id, status_code=404, detail=_LORE_NOT_FOUND)
    stream = lore_dm_chat.start_lore_chat_turn(db, campaign=campaign, character=char, user_id=profile.id, content=content)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
