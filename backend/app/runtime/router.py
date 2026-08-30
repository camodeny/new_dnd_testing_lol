"""Runtime transport — live-table submissions and session stubs."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from app.runtime.submissions import (
    SubmissionValidationError,
    accept_submission,
    list_submissions,
    validate_submission_payload,
)
from database import get_db
from models import Campaign, PlayerSubmissionSegment

router = APIRouter()
logger = logging.getLogger(__name__)


def _authorized_campaign(db: Session, campaign_id: str, user_id: uuid.UUID) -> Campaign:
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid campaign id") from exc
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != user_id and not is_campaign_member(db, cid, user_id):
        logger.info("player_submission rejected campaign_id=%s reason=not_member", cid)
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return campaign


@router.post("/api/campaigns/{campaign_id}/submissions", status_code=201)
def create_player_submission(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)

    # Only the campaign-wide main thread exists today. Reject client-created
    # visibility claims until a server-owned thread membership model exists.
    thread_id = str(payload.get("thread_id", "main"))
    audience = str(payload.get("audience", "campaign"))
    if thread_id != "main" or audience != "campaign":
        logger.info("player_submission rejected campaign_id=%s reason=unauthorized_thread", campaign.id)
        raise HTTPException(status_code=403, detail="Only the campaign main thread is currently available")

    try:
        raw_content, segments = validate_submission_payload(payload)
        character_id = uuid.UUID(str(payload["character_id"])) if payload.get("character_id") else None
    except (SubmissionValidationError, ValueError) as exc:
        logger.info("player_submission rejected campaign_id=%s reason=validation", campaign.id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    idempotency_key = require_idempotency_key(
        request, str(payload.get("operation_id") or "").strip() or None
    )

    def _execute():
        try:
            submission = accept_submission(
                db,
                campaign_id=campaign.id,
                user_id=profile.id,
                character_id=character_id,
                raw_content=raw_content,
                segments=segments,
                thread_id=thread_id,
                audience=audience,
            )
        except SubmissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stored_segments = db.query(PlayerSubmissionSegment).filter_by(
            submission_id=submission.id
        ).order_by(PlayerSubmissionSegment.position).all()
        return {"submission": submission.to_dict(stored_segments)}

    return execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="player_submission.accept",
        scope_type="campaign_thread",
        scope_id=f"{campaign.id}:{thread_id}",
        payload=payload,
        execute=_execute,
    )


@router.get("/api/campaigns/{campaign_id}/submissions")
def get_player_submissions(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    return {"submissions": list_submissions(db, campaign.id), "thread_id": "main"}


@router.post("/api/campaigns/{campaign_id}/sessions")
def stub_start_session(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Sessions not yet implemented")
