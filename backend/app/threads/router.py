"""Thread transport — campaign and private thread listing, creation, and detail."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps.auth import current_profile
from app.deps.campaign import campaign_for
from app.threads.service import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    assert_can_read_thread,
    create_private_thread,
    get_campaign_thread,
    get_or_create_campaign_thread,
    get_or_create_private_gameplay_thread,
    list_threads_for_user,
    parse_thread_id,
)
from database import get_db
from models.campaigns import Campaign
from models.threads import CampaignThreadMember

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/campaigns/{campaign_id}/threads")
def list_campaign_threads(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    # Ensure shared thread exists so its id survives reconnects
    get_or_create_campaign_thread(db, campaign.id, created_by=profile.id)
    db.commit()
    threads = list_threads_for_user(db, campaign.id, profile.id)
    result = []
    for thread in threads:
        members = db.query(CampaignThreadMember).filter_by(thread_id=thread.id).all()
        result.append(
            thread.to_dict(
                include_members=thread.thread_type == "private", members=members
            )
        )
    logger.info(
        "thread list accessed campaign_id=%s user_id=%s visible_count=%s",
        campaign.id,
        profile.id,
        len(result),
    )
    return {"threads": result}


def _private_thread_response(db: Session, thread, created: bool):
    members = db.query(CampaignThreadMember).filter_by(thread_id=thread.id).all()
    return {
        "thread": thread.to_dict(include_members=True, members=members),
        "created": created,
    }


@router.post("/api/campaigns/{campaign_id}/threads/dm")
def get_or_create_ai_dm_thread(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    try:
        thread, created = get_or_create_private_gameplay_thread(
            db,
            campaign_id=campaign.id,
            created_by=profile.id,
            private_kind="dm",
            participant_ids=[],
            title="Private with AI DM",
        )
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _private_thread_response(db, thread, created)


@router.post("/api/campaigns/{campaign_id}/threads/direct")
def get_or_create_direct_thread(
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    raw_participant_id = payload.get("participant_id")
    try:
        participant_id = uuid.UUID(str(raw_participant_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=422, detail="participant_id must be a valid user id"
        ) from exc
    if participant_id == profile.id:
        raise HTTPException(status_code=422, detail="Choose another player")
    try:
        thread, created = get_or_create_private_gameplay_thread(
            db,
            campaign_id=campaign.id,
            created_by=profile.id,
            private_kind="direct",
            participant_ids=[participant_id],
            title="Private player conversation",
        )
    except ThreadAuthorizationError as exc:
        raise HTTPException(
            status_code=403, detail="That player is not a campaign member"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _private_thread_response(db, thread, created)


@router.get("/api/campaigns/{campaign_id}/threads/{thread_id}")
def get_campaign_thread_detail(
    thread_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    try:
        tid = parse_thread_id(thread_id)
        assert_can_read_thread(db, campaign.id, tid, profile.id)
        thread = get_campaign_thread(db, campaign.id, tid)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        logger.info(
            "thread detail denied campaign_id=%s thread_id=%s user_id=%s",
            campaign.id,
            thread_id,
            profile.id,
        )
        raise HTTPException(
            status_code=403, detail="Not authorized to read this thread"
        ) from exc
    members = db.query(CampaignThreadMember).filter_by(thread_id=tid).all()
    return {"thread": thread.to_dict(include_members=True, members=members)}


@router.post("/api/campaigns/{campaign_id}/threads", status_code=201)
def create_campaign_thread(
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    thread_type = str(payload.get("thread_type", "private"))
    if thread_type not in ("private",):
        raise HTTPException(
            status_code=422,
            detail="Only private threads can be created via this endpoint",
        )
    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=422, detail="title must be a string")
    raw_members = payload.get("member_ids", payload.get("members", []))
    if not isinstance(raw_members, list):
        raise HTTPException(status_code=422, detail="member_ids must be an array")
    member_ids: list[uuid.UUID] = []
    for mid in raw_members:
        try:
            member_ids.append(uuid.UUID(str(mid)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid member id: {mid}"
            ) from exc
    try:
        thread = create_private_thread(
            db,
            campaign_id=campaign.id,
            created_by=profile.id,
            member_ids=member_ids,
            title=title,
        )
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Return with members for convenience
    members = db.query(CampaignThreadMember).filter_by(thread_id=thread.id).all()
    db.commit()
    return {"thread": thread.to_dict(include_members=True, members=members)}
