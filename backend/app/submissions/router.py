"""Submission transport — live-table player submissions and capacity state."""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.billing.resolution_guarantee import (
    CapacityPausedError as _CapPausedPre,
    CapacityPausedError as _CapPaused,
    capacity_state_payload,
    require_new_ai_work,
)
from app.deps.campaign import campaign_for
from app.campaigns.service import CampaignArchivedError
from app.deps.auth import current_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from app.dm.recovery import execute_committed_attempt
from app.dm.turns import StreamBoundaryError, TurnConflictError, coordinate_turn, get_active_turn
from app.realtime.service import publish_submission_created
from app.submissions.service import (
    SubmissionValidationError,
    accept_submission,
    list_submissions,
    validate_submission_payload,
)
from app.threads.service import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    assert_can_read_thread,
    assert_can_write_thread,
    get_campaign_thread,
    is_lobby_thread,
    parse_thread_id,
    resolve_thread_id,
)
from database import get_db
from models.campaigns import Campaign
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/campaigns/{campaign_id}/submissions", status_code=201)
def create_player_submission(
    payload: dict,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    if str(campaign.status or "").lower() == "archived":
        logger.info(
            "player_submission rejected campaign_id=%s reason=archived", campaign.id
        )
        raise HTTPException(
            status_code=409,
            detail="Campaign is archived; restore it before continuing play",
        )

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
        # Issue #243 — the lobby OOC thread is non-fictional table talk. Gameplay
        # (IC-capable) submissions can never target it: lobby chat has its own
        # OOC-forced endpoint, and this refusal keeps lobby history out of DM
        # turn assembly by construction.
        if is_lobby_thread(thread):
            logger.warning(
                "player_submission rejected campaign_id=%s reason=lobby_thread thread_id=%s user_id=%s",
                campaign.id,
                resolved_thread_id,
                profile.id,
            )
            raise HTTPException(
                status_code=409,
                detail="The lobby thread is out-of-character only; post lobby chat via the lobby chat endpoint",
            )
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
        # Issue #254 — capacity boundary before a NEW AI obligation. This runs
        # only on first execution: idempotent retries replay the committed
        # result via the idempotency record without re-gating, so already
        # accepted work is never refused as though it were new. When this
        # submission would start fresh AI work (no active owed turn to merge
        # into pre-stream), an AI-paused campaign refuses with a
        # machine-readable state hook; the client keeps the unsent text as an
        # editable local draft. Merging into an existing owed turn, and all
        # owed continuations (rolls, streaming, commit, post-turn), proceed.
        try:
            _active = get_active_turn(db, campaign.id, thread_id_str)
            # Direct player conversations never invoke the AI DM (coordination
            # is skipped below), so the capacity gate does not apply to them
            # — this non-AI surface stays usable while AI work is paused.
            _is_direct = thread is not None and thread.private_kind == "direct"
            if not _is_direct and (
                _active is None or str(_active.status) not in ("pending", "awaiting_roll")
            ):
                require_new_ai_work(db, campaign.id, thread_id_str)
        except _CapPausedPre as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ai_paused_capacity",
                    "message": str(exc),
                    "retryable": True,
                    "draft_safe": True,
                    "capacity_state": exc.decision,
                },
            ) from exc
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
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        except _CapPaused as exc:
            # Lost a capacity race between the first-execution pre-check above
            # and serialized coordination: nothing is accepted (outer
            # transaction rolls back) so the client keeps its local draft and
            # retries after capacity returns.
            logger.info(
                "player_submission rejected campaign_id=%s thread_id=%s reason=ai_paused_capacity",
                campaign.id,
                thread_id_str,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ai_paused_capacity",
                    "message": str(exc),
                    "retryable": True,
                    "draft_safe": True,
                    "capacity_state": exc.decision,
                },
            ) from exc
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
                    publish_submission_created(db, db_sub, segments=segs)
            except Exception as pub_exc:
                logger.warning(
                    "realtime publish after submission failed submission_id=%s error=%s",
                    sub_dict.get("id"),
                    pub_exc,
                )
    except Exception as exc:
        # Outer guard — never affect the authoritative response, but never
        # swallow silently: log a stable degradation reason.
        logger.warning(
            "realtime publish guard failed campaign_id=%s thread_id=%s error=%s",
            campaign.id,
            thread_id_str,
            exc,
        )
    # Immediate execution is scoped to this submission's coordinated attempt.
    # Direct player conversations have no DM attempt and never trigger DM work.
    # Dispatch post-response instead of waiting for the next cron sweep (~60s).
    # BackgroundTasks returns 201 in milliseconds and runs the pipeline after
    # the response; a blocking inline run would hold the request for the full
    # multi-model pipeline (60s+), which proxies kill (~30s) with a 500 the
    # backend never sees — while the turn still succeeds server-side. Best
    # effort: if the runtime does not finish post-response work, the attempt
    # stays ``prepared`` and ``/api/cron/dm-execute`` reconciles it.
    attempt_data = result.get("dm_attempt") if isinstance(result, dict) else None
    if attempt_data and attempt_data.get("id"):
        background_tasks.add_task(
            execute_committed_attempt, str(attempt_data["id"])
        )
    return result


@router.get("/api/campaigns/{campaign_id}/capacity-state")
def get_capacity_state(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """Participant-safe capacity state hook — issue #254.

    Aggregates + AI-pause/grace/owed flags only (no secrets, keys, or
    narrative). Non-AI surface: stays usable while AI play is paused.
    """
    return capacity_state_payload(db, campaign.id)


@router.get("/api/campaigns/{campaign_id}/submissions")
def get_player_submissions(
    request: Request,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
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


@router.get("/api/campaigns/{campaign_id}/threads/{thread_id}/submissions")
def get_thread_submissions(
    thread_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
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
