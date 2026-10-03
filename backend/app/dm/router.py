"""DM turn transport — issue #200.

Read and lifecycle endpoints for the durable DM turn/attempt state machine.
Write-side turn assembly is triggered automatically via submission coordination;
these endpoints expose read-only inspection, player-initiated retry, and the
execution cron trigger.
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.deps.campaign import campaign_for, require_owner, run_campaign_command
from app.deps.auth import current_profile
from app.deps.cron import require_cron_secret
from app.deps.idempotency import require_idempotency_key
from app.post_turn.backpressure import describe_client_state, evaluate_backpressure
from app.realtime.service import publish_dm_status
from app.threads.service import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    assert_can_read_thread,
    list_threads_for_user,
    parse_thread_id,
    resolve_thread_id,
)
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/retry")
def retry_adjudication(
    turn_id: str,
    payload: dict,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    from app.dm.recovery import retry_failed_adjudication, execute_committed_attempt
    from models.dm import DmTurn
    require_owner(campaign, profile.id)
    try:
        tid, aid = uuid.UUID(turn_id), uuid.UUID(str(payload.get("attempt_id")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Valid turn and attempt IDs are required") from exc
    turn = db.get(DmTurn, tid)
    if turn is None or turn.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        assert_can_read_thread(db, campaign.id, parse_thread_id(turn.thread_id), profile.id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise HTTPException(status_code=404, detail="Turn not found") from exc
    key = require_idempotency_key(request, payload.get("operation_id"))
    def execute():
        try:
            updated, attempt = retry_failed_adjudication(db, campaign.id, tid, aid)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn_id": str(updated.id), "attempt_id": str(attempt.id)}
    result = run_campaign_command(db, response, actor_id=profile.id, idempotency_key=key,
        command_type="dm_turn.retry", scope_type="dm_turn", scope_id=tid, payload=payload, execute=execute)
    background_tasks.add_task(execute_committed_attempt, result["attempt_id"])
    return result


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/retry-narration")
def retry_narration(
    turn_id: str,
    payload: dict,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """Narration-independent retry reusing the preserved structured result."""
    from app.dm.recovery import retry_narration_only, execute_committed_attempt
    from models.dm import DmTurn
    require_owner(campaign, profile.id)
    try:
        tid, aid = uuid.UUID(turn_id), uuid.UUID(str(payload.get("attempt_id")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Valid turn and attempt IDs are required") from exc
    turn = db.get(DmTurn, tid)
    if turn is None or turn.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        assert_can_read_thread(db, campaign.id, parse_thread_id(turn.thread_id), profile.id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise HTTPException(status_code=404, detail="Turn not found") from exc
    key = require_idempotency_key(request, payload.get("operation_id"))
    def execute():
        try:
            updated, attempt = retry_narration_only(db, campaign.id, tid, aid)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn_id": str(updated.id), "attempt_id": str(attempt.id)}
    result = run_campaign_command(db, response, actor_id=profile.id, idempotency_key=key,
        command_type="dm_turn.retry_narration", scope_type="dm_turn", scope_id=tid, payload=payload, execute=execute)
    background_tasks.add_task(execute_committed_attempt, result["attempt_id"])
    return result


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/streams/{stream_id}/continue")
def continue_stream(
    turn_id: str,
    stream_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """Recover a failed partial stream through the full turn state machine.

    Body: {"continued_text": "..."}. The stream is scoped to the authorized
    campaign/turn/current-attempt before any write; the supplied text must
    preserve the persisted visible prefix and pass the contract fidelity
    gate; on success the attempt/turn finalize and staged effects promote
    via the normal commit path. Failures return generic messages without
    infrastructure details.

    Atomicity: recovery runs flush-only inside the idempotent command, so
    the command row, stream progress, and turn commit persist in ONE commit.
    A crash can never leave half-committed recovery state beside a durable
    ``in_progress`` record — retrying the same key cleanly re-executes.
    Realtime delivery happens after that commit returns (never before
    durability), via a best-effort post-commit status publish.
    """
    from app.dm.recovery import recover_partial_stream
    from models.dm import DmTurn
    require_owner(campaign, profile.id)
    try:
        tid, sid = uuid.UUID(turn_id), uuid.UUID(stream_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Valid turn and stream IDs are required") from exc
    turn = db.get(DmTurn, tid)
    if turn is None or turn.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        assert_can_read_thread(db, campaign.id, parse_thread_id(turn.thread_id), profile.id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise HTTPException(status_code=404, detail="Turn not found") from exc
    continued = str(payload.get("continued_text") or "")
    if not continued:
        raise HTTPException(status_code=422, detail="continued_text is required")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def _shape(final_turn, final_attempt, event) -> dict:
        event_id = getattr(event, "id", None)
        if event_id is None:
            result = dict(final_attempt.result or {})
            event_id = result.get("event_id") or result.get("id") or ""
        return {"turn_id": str(final_turn.id), "attempt_id": str(final_attempt.id),
                "stream_id": str(sid), "event_id": str(event_id)}

    def execute():
        try:
            final_turn, final_attempt, event = recover_partial_stream(
                db, campaign.id, tid, sid, continued, actor_id=profile.id,
                commit=False,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Turn not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="The storyteller faltered. You can retry this turn.") from exc
        return _shape(final_turn, final_attempt, event)

    # Full semantic identity: stream + complete text. Only the digest is
    # persisted, so nothing is truncated — requests differing anywhere
    # (even past character 4,000) are distinct commands.
    result = run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="dm_turn.recover_partial_stream", scope_type="dm_turn",
        scope_id=tid, payload={"stream_id": str(sid), "continued_text": continued},
        execute=execute,
    )
    if response.headers.get("X-Idempotent-Replay") != "true":
        _publish_recovery_status(db, sid)
    return result


def _publish_recovery_status(db: Session, stream_id) -> None:
    """Best-effort post-commit realtime status for a recovered stream."""
    import logging as _logging

    _logger = _logging.getLogger(__name__)
    try:
        from app.dm.streams import get_stream
        stream = get_stream(db, stream_id)
        if stream is not None and stream.status == "completed":
            publish_dm_status(db, stream, visible_text=stream.final_text)
    except Exception as exc:
        _logger.warning("recovery realtime publish failed stream_id=%s error=%s",
                        stream_id, exc)


@router.get("/api/campaigns/{campaign_id}/dm-turns")
def list_dm_turns(
    request: Request,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    thread_raw = request.query_params.get("thread_id")
    from app.dm.turns import list_turns

    if thread_raw:
        try:
            tid = resolve_thread_id(db, campaign.id, thread_raw, created_by=profile.id)
            db.commit()
            assert_can_read_thread(db, campaign.id, tid, profile.id)
            turns = list_turns(db, campaign.id, thread_id=str(tid), limit=200)
            return {"turns": [t.to_dict() for t in turns]}
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except ThreadAuthorizationError as exc:
            raise HTTPException(status_code=403, detail="Not authorized for this thread") from exc
    # No thread filter: return only turns for threads the user is authorized to see
    # (prevents private-turn metadata leakage)
    visible_threads = list_threads_for_user(db, campaign.id, profile.id)
    visible_ids = {str(t.id) for t in visible_threads}
    all_turns = list_turns(db, campaign.id, thread_id=None, limit=200)
    filtered = [t for t in all_turns if str(t.thread_id) in visible_ids]
    return {"turns": [t.to_dict() for t in filtered]}


@router.get("/api/campaigns/{campaign_id}/dm-turns/{turn_id}")
def get_dm_turn(
    turn_id: str,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    try:
        tid = uuid.UUID(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid turn id") from exc
    from app.dm.turns import get_turn

    turn = get_turn(db, tid)
    if turn is None or str(turn.campaign_id) != str(campaign.id):
        raise HTTPException(status_code=404, detail="Turn not found")
    # Strict per-turn thread authorization — private turn metadata must not leak
    try:
        t_uuid = parse_thread_id(turn.thread_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    try:
        assert_can_read_thread(db, campaign.id, t_uuid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        # Hide private existence as 404
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    from sqlalchemy import select
    from models.dm import DmTurnAttempt

    attempts = db.execute(
        select(DmTurnAttempt).where(DmTurnAttempt.turn_id == tid).order_by(DmTurnAttempt.attempt_number)
    ).scalars().all()
    # Issue #222 — low-key client state: ready vs temporary DM processing
    # delay. Never exposes queues, providers, models, or internals.
    try:
        readiness = describe_client_state(evaluate_backpressure(db, campaign.id))
    except Exception:
        readiness = {"dm_state": "processing",
                     "message": "The DM is finishing processing recent events before continuing."}
    return {
        "turn": turn.to_dict(),
        "attempts": [a.to_dict(include_private_roll_evidence=campaign.owner_id == profile.id, include_private_staged_effects=campaign.owner_id == profile.id) for a in attempts],
        "readiness": readiness,
    }


@router.get("/api/cron/dm-execute")
def dm_execute_cron_get(request: Request, db: Session = Depends(get_db)):
    """Autonomous DM execution sweep — issue #354.

    Cron trigger (Supabase Cron / scheduler) that claims and executes eligible
    prepared DM attempts through the production pipeline without manual
    API/database intervention. Shared cron auth guard (``app.deps.cron``):
    ``CRON_SECRET`` bearer, or ``ALLOW_INSECURE_CRON=1`` local/test bypass.
    """
    require_cron_secret(request.headers.get("authorization"))
    from app.dm.execution import run_dm_execute_sweep

    result = run_dm_execute_sweep(db, limit=1)
    logger.info(
        "dm execute cron executed=%s failed=%s skipped=%s recovered=%s",
        len(result.get("executed", [])),
        len(result.get("failed", [])),
        len(result.get("skipped", [])),
        result.get("recovered", 0),
    )
    return {"ok": True, "sweep": result}


@router.post("/api/cron/dm-execute")
def dm_execute_cron_post(request: Request, db: Session = Depends(get_db)):
    return dm_execute_cron_get(request=request, db=db)
