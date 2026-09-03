"""DM turn transport — issue #200.

Read and lifecycle endpoints for the durable DM turn/attempt state machine.
Write-side turn assembly is triggered automatically via submission coordination;
these endpoints allow inspection and explicit lifecycle transitions (streaming,
commit) for integration tests and the worker runtime.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.runtime.threads import assert_can_read_thread, parse_thread_id, resolve_thread_id, ThreadNotFoundError, ThreadAuthorizationError
from database import get_db
from models import Campaign

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
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return campaign


def _require_owner(campaign: Campaign, user_id: uuid.UUID):
    if campaign.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the campaign owner can perform this DM lifecycle action")


@router.get("/api/campaigns/{campaign_id}/dm-turns")
def list_dm_turns(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
    from app.runtime.threads import list_threads_for_user

    visible_threads = list_threads_for_user(db, campaign.id, profile.id)
    visible_ids = {str(t.id) for t in visible_threads}
    all_turns = list_turns(db, campaign.id, thread_id=None, limit=200)
    filtered = [t for t in all_turns if str(t.thread_id) in visible_ids]
    return {"turns": [t.to_dict() for t in filtered]}


@router.get("/api/campaigns/{campaign_id}/dm-turns/{turn_id}")
def get_dm_turn(campaign_id: str, turn_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
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
    from models import DmTurnAttempt

    attempts = db.execute(
        select(DmTurnAttempt).where(DmTurnAttempt.turn_id == tid).order_by(DmTurnAttempt.attempt_number)
    ).scalars().all()
    return {
        "turn": turn.to_dict(),
        "attempts": [a.to_dict(include_private_roll_evidence=campaign.owner_id == profile.id) for a in attempts],
    }


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/streaming")
def start_streaming(campaign_id: str, turn_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    _require_owner(campaign, profile.id)
    try:
        tid = uuid.UUID(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid turn id") from exc
    attempt_id_raw = payload.get("attempt_id")
    if not attempt_id_raw:
        raise HTTPException(status_code=422, detail="attempt_id is required")
    try:
        aid = uuid.UUID(str(attempt_id_raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid attempt_id") from exc
    stream_id_raw = payload.get("stream_id")
    if not stream_id_raw:
        raise HTTPException(status_code=422, detail="stream_id is required — transition to streaming requires durable first chunk")
    from app.dm.turns import AttemptSupersededError, get_turn, mark_streaming_started

    # Verify turn belongs to path campaign (prevents cross-campaign mutation via known UUID)
    turn_check = get_turn(db, tid)
    if turn_check is None or str(turn_check.campaign_id) != str(campaign.id):
        raise HTTPException(status_code=404, detail="Turn not found")
    # Preserve private-thread authorization semantics
    try:
        t_uuid = parse_thread_id(turn_check.thread_id)
        assert_can_read_thread(db, campaign.id, t_uuid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc

    try:
        turn, attempt = mark_streaming_started(db, tid, aid, stream_id=stream_id_raw)
        # Re-verify after mutation that turn still belongs to path campaign
        if str(turn.campaign_id) != str(campaign.id):
            raise HTTPException(status_code=404, detail="Turn not found")
        return {"turn": turn.to_dict(), "attempt": attempt.to_dict()}
    except AttemptSupersededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/commit")
def commit_dm_turn_endpoint(campaign_id: str, turn_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    _require_owner(campaign, profile.id)
    try:
        tid = uuid.UUID(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid turn id") from exc
    attempt_id_raw = payload.get("attempt_id")
    if not attempt_id_raw:
        raise HTTPException(status_code=422, detail="attempt_id is required")
    try:
        aid = uuid.UUID(str(attempt_id_raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid attempt_id") from exc
    from app.dm.turns import get_turn

    turn_check = get_turn(db, tid)
    if turn_check is None or str(turn_check.campaign_id) != str(campaign.id):
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        t_uuid = parse_thread_id(turn_check.thread_id)
        assert_can_read_thread(db, campaign.id, t_uuid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    from app.dm.turns import AttemptSupersededError, StaleRevisionError, TurnConflictError
    from app.campaigns.events import RevisionConflictError

    expected = payload.get("expected_revision")
    if expected is not None:
        try:
            expected = int(expected)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="expected_revision must be an integer") from exc

    mutate_fields = payload.get("mutate") if isinstance(payload.get("mutate"), dict) else None

    def _mutate(camp):
        if not mutate_fields:
            return
        if "name" in mutate_fields and mutate_fields["name"] is not None:
            from app.campaigns.service import validate_campaign_name

            camp.name = validate_campaign_name(str(mutate_fields["name"]))
        if "description" in mutate_fields:
            camp.description = mutate_fields["description"]

    event_type = str(payload.get("event_type") or "dm.turn_resolved")
    operation_id = str(payload.get("operation_id") or "").strip() or None

    from app.dm.turns import commit_turn

    try:
        turn, attempt, event = commit_turn(
            db,
            tid,
            aid,
            expected_revision=expected,
            mutate=_mutate if mutate_fields else None,
            event_type=event_type,
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else None,
            operation_id=operation_id,
            actor_id=profile.id,
        )
        if str(turn.campaign_id) != str(campaign.id):
            raise HTTPException(status_code=404, detail="Turn not found")
        return {"turn": turn.to_dict(), "attempt": attempt.to_dict(), "event": event.to_dict() if hasattr(event, "to_dict") else None}
    except AttemptSupersededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StaleRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"X-Current-Revision": str(exc.actual_revision)}) from exc
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"X-Current-Revision": str(exc.actual_revision)}) from exc
    except TurnConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/abandon")
def abandon_dm_turn_endpoint(campaign_id: str, turn_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    _require_owner(campaign, profile.id)
    try:
        tid = uuid.UUID(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid turn id") from exc
    attempt_id_raw = payload.get("attempt_id")
    if not attempt_id_raw:
        raise HTTPException(status_code=422, detail="attempt_id is required")
    try:
        aid = uuid.UUID(str(attempt_id_raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid attempt_id") from exc
    from app.dm.turns import abandon_visible_attempt, get_turn

    turn_check = get_turn(db, tid)
    if turn_check is None or str(turn_check.campaign_id) != str(campaign.id):
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        t_uuid = parse_thread_id(turn_check.thread_id)
        assert_can_read_thread(db, campaign.id, t_uuid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc

    reason = str(payload.get("reason") or payload.get("abandonment_reason") or "explicit_retry")
    try:
        turn, attempt = abandon_visible_attempt(db, tid, aid, reason=reason, actor_id=profile.id)
        if str(turn.campaign_id) != str(campaign.id):
            raise HTTPException(status_code=404, detail="Turn not found")
        return {"turn": turn.to_dict(), "attempt": attempt.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/dm-turns/recover")
def recover_stuck(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    # Only owner can recover
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can recover stuck turns")
    lease = payload.get("lease_seconds", 300)
    try:
        lease = int(lease)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lease_seconds must be an integer")
    from app.dm.turns import recover_stuck_attempts

    recovered = recover_stuck_attempts(db, campaign_id=campaign.id, lease_seconds=lease)
    return {"recovered": recovered}
