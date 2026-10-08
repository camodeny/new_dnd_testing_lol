"""Private per-player notes — the player's own journal scratch space.

Unlike the auto DM journal (WorldFact projections), these rows are
player-authored and visible only to their author. Fail-closed: a note id
belonging to another user reads as 404 with no existence leak.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, parse_uuid_or_404
from database import get_db
from models.campaigns import Campaign, CampaignPlayerNote

router = APIRouter()

_INVALID_IDS = "Invalid campaign or note id"
_NOTE_NOT_FOUND = "Note not found"
notes_campaign = campaign_for("participant", invalid=_INVALID_IDS)

NOTE_MAX_LENGTH = 2000


def _validate_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be an object")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="content must be a non-empty string")
    stripped = content.strip()
    if len(stripped) > NOTE_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"content must be {NOTE_MAX_LENGTH} characters or fewer")
    return stripped


def _own_note_or_404(db: Session, note_id: uuid.UUID, user_id: uuid.UUID) -> CampaignPlayerNote:
    note = db.get(CampaignPlayerNote, note_id)
    if note is None or note.user_id != user_id:
        raise HTTPException(status_code=404, detail=_NOTE_NOT_FOUND)
    return note


@router.get("/api/campaigns/{campaign_id}/notes")
def list_notes(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(notes_campaign),
    db: Session = Depends(get_db),
):
    """List the caller's own notes in this campaign, oldest first."""
    rows = db.execute(
        select(CampaignPlayerNote)
        .where(
            CampaignPlayerNote.campaign_id == campaign.id,
            CampaignPlayerNote.user_id == profile.id,
        )
        .order_by(CampaignPlayerNote.created_at.asc())
        .limit(200)
    ).scalars().all()
    return {"notes": [row.to_dict() for row in rows]}


@router.post("/api/campaigns/{campaign_id}/notes", status_code=201)
def create_note(
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(notes_campaign),
    db: Session = Depends(get_db),
):
    """Create one private note for the caller."""
    content = _validate_content(payload)
    note = CampaignPlayerNote(campaign_id=campaign.id, user_id=profile.id, content=content)
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"note": note.to_dict()}


@router.patch("/api/campaigns/{campaign_id}/notes/{note_id}")
def update_note(
    note_id: str,
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(notes_campaign),
    db: Session = Depends(get_db),
):
    """Edit the caller's own note. Another user's id reads as 404."""
    nid = parse_uuid_or_404(note_id, _INVALID_IDS)
    note = _own_note_or_404(db, nid, profile.id)
    if note.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail=_NOTE_NOT_FOUND)
    note.content = _validate_content(payload)
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"note": note.to_dict()}


@router.delete("/api/campaigns/{campaign_id}/notes/{note_id}")
def delete_note(
    note_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(notes_campaign),
    db: Session = Depends(get_db),
):
    """Delete the caller's own note (idempotent within own scope)."""
    nid = parse_uuid_or_404(note_id, _INVALID_IDS)
    note = _own_note_or_404(db, nid, profile.id)
    if note.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail=_NOTE_NOT_FOUND)
    db.delete(note)
    db.commit()
    return {"deleted": str(note.id)}
