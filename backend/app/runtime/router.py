"""Runtime transport — live-table submissions, threads, and session stubs."""

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
from app.runtime.threads import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    assert_can_read_thread,
    assert_can_write_thread,
    create_private_thread,
    get_or_create_private_gameplay_thread,
    get_campaign_thread,
    get_or_create_campaign_thread,
    list_threads_for_user,
    parse_thread_id,
    resolve_thread_id,
)
from database import get_db
from models import (
    Campaign,
    CampaignThreadMember,
    PlayerSubmission,
    PlayerSubmissionSegment,
)

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

    raw_thread = (
        str(payload.get("thread_id", "main"))
        if payload.get("thread_id") is not None
        else "main"
    )
    # Audience field is now derived from thread_type; client claims are ignored but
    # ambiguous explicit private claims without a valid private thread fail closed.
    try:
        resolved_thread_id = resolve_thread_id(
            db, campaign.id, raw_thread, created_by=profile.id
        )
        # Commit durable shared-thread creation at request boundary (helper no longer commits)
        db.commit()
        # Centralized write authorization (shared = campaign membership, private = explicit thread membership)
        # Private existence is hidden as 404 — see threads.assert_can_write_thread
        assert_can_write_thread(db, campaign.id, resolved_thread_id, profile.id)
        thread = get_campaign_thread(db, campaign.id, resolved_thread_id)
        audience = (
            "campaign" if thread and thread.thread_type == "campaign" else "private"
        )
    except ThreadNotFoundError as exc:
        logger.info(
            "player_submission rejected campaign_id=%s reason=thread_not_found thread_id=%s",
            campaign.id,
            raw_thread,
        )
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        logger.info(
            "player_submission rejected campaign_id=%s reason=thread_not_authorized thread_id=%s user_id=%s",
            campaign.id,
            raw_thread,
            profile.id,
        )
        raise HTTPException(
            status_code=403, detail="Not authorized for this thread"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Reject client-forged audience mismatches — server derives audience from thread
    client_audience = str(payload.get("audience", audience))
    if client_audience not in ("campaign", "private"):
        client_audience = audience
    # If client explicitly claims private but thread is campaign, fail closed
    if (
        client_audience == "private"
        and audience == "campaign"
        and raw_thread not in ("main", "", None)
    ):
        # This is the legacy forged-audience path — already handled via thread auth above
        pass
    thread_id_str = str(resolved_thread_id)

    try:
        raw_content, segments = validate_submission_payload(payload)
        character_id = (
            uuid.UUID(str(payload["character_id"]))
            if payload.get("character_id")
            else None
        )
    except (SubmissionValidationError, ValueError) as exc:
        logger.info(
            "player_submission rejected campaign_id=%s reason=validation", campaign.id
        )
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
                thread_id=thread_id_str,
                audience=audience,
            )
        except SubmissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stored_segments = (
            db.query(PlayerSubmissionSegment)
            .filter_by(submission_id=submission.id)
            .order_by(PlayerSubmissionSegment.position)
            .all()
        )
        result = {"submission": submission.to_dict(stored_segments)}
        # Coordinate DM turn assembly — issue #200. Best-effort: coordination
        # failures (e.g. stream boundary) are logged but do not fail submission
        # acceptance. The submission is durably stored; DM turn will be
        # observable via the dm-turns API.
        try:
            from app.dm.turns import (
                StreamBoundaryError,
                TurnConflictError,
                coordinate_turn,
            )

            # Direct player conversations do not summon or expose their content
            # to the AI DM. Shared and AI-DM threads retain normal coordination.
            coord = None
            if thread is None or thread.private_kind != "direct":
                # Flush-only: transaction ownership stays at the outer idempotency
                # boundary (submission + IdempotentCommand + turn) commit atomically.
                coord = coordinate_turn(
                    db, campaign.id, thread_id_str, audience=audience, commit=False
                )
            if coord is not None:
                turn, attempt = coord
                result["dm_turn"] = turn.to_dict()
                result["dm_attempt"] = attempt.to_dict()
        except (StreamBoundaryError, TurnConflictError) as exc:
            logger.info(
                "player_submission dm_turn coordination deferred campaign_id=%s thread_id=%s reason=%s",
                campaign.id,
                thread_id_str,
                exc,
            )
        except Exception as exc:  # pragma: no cover — observability only
            logger.warning(
                "player_submission dm_turn coordination error campaign_id=%s thread_id=%s error=%s",
                campaign.id,
                thread_id_str,
                exc,
            )
        return result

    result = execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="player_submission.accept",
        scope_type="campaign_thread",
        scope_id=f"{campaign.id}:{thread_id_str}",
        payload=payload,
        execute=_execute,
    )
    # Live-table Realtime projection — best-effort after authoritative commit.
    # Never roll back DB on publish failure (#198 failure/recovery).
    try:
        sub_dict = result.get("submission") if isinstance(result, dict) else None
        if sub_dict and sub_dict.get("id"):
            try:
                sub_id = uuid.UUID(str(sub_dict["id"]))
                db_sub = db.get(PlayerSubmission, sub_id)  # type: ignore[attr-defined]
                if db_sub is not None:
                    segs = (
                        db.query(PlayerSubmissionSegment)
                        .filter_by(submission_id=db_sub.id)
                        .order_by(PlayerSubmissionSegment.position)
                        .all()
                    )  # type: ignore[attr-defined]
                    from app.realtime.service import publish_submission_created

                    publish_submission_created(db, db_sub, segments=segs)
            except Exception as pub_exc:
                logger.warning(
                    "realtime publish after submission failed submission_id=%s error=%s",
                    sub_dict.get("id"),
                    pub_exc,
                )
    except Exception:
        # Outer guard — never affect authoritative response.
        pass
    return result


@router.get("/api/campaigns/{campaign_id}/submissions")
def get_player_submissions(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    # Optional thread_id query param; defaults to shared campaign thread for backward compat
    raw_thread = request.query_params.get("thread_id", "main")
    try:
        resolved = resolve_thread_id(db, campaign.id, raw_thread, created_by=profile.id)
        db.commit()
        assert_can_read_thread(db, campaign.id, resolved, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        logger.info(
            "thread history denied campaign_id=%s thread_id=%s user_id=%s",
            campaign.id,
            resolved,
            profile.id,
        )
        raise HTTPException(
            status_code=403, detail="Not authorized to read this thread"
        ) from exc
    return {
        "submissions": list_submissions(db, campaign.id, thread_id=str(resolved)),
        "thread_id": str(resolved),
    }


# ── Thread endpoints ────────────────────────────────────────────────


@router.get("/api/campaigns/{campaign_id}/threads")
def list_campaign_threads(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
    campaign_id: str, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
    campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    raw_participant_id = payload.get("participant_id")
    try:
        participant_id = uuid.UUID(str(raw_participant_id))
    except Exception as exc:
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
    campaign_id: str, thread_id: str, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
    campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
        except Exception as exc:
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


@router.get("/api/campaigns/{campaign_id}/threads/{thread_id}/submissions")
def get_thread_submissions(
    campaign_id: str, thread_id: str, request: Request, db: Session = Depends(get_db)
):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    try:
        tid = parse_thread_id(thread_id)
        assert_can_read_thread(db, campaign.id, tid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        logger.info(
            "thread history denied campaign_id=%s thread_id=%s user_id=%s",
            campaign.id,
            thread_id,
            profile.id,
        )
        raise HTTPException(
            status_code=403, detail="Not authorized to read this thread"
        ) from exc
    return {
        "submissions": list_submissions(db, campaign.id, thread_id=str(tid)),
        "thread_id": str(tid),
    }


@router.post("/api/campaigns/{campaign_id}/sessions")
def stub_start_session(
    campaign_id: str, request: Request, db: Session = Depends(get_db)
):
    raise HTTPException(status_code=501, detail="Sessions not yet implemented")
