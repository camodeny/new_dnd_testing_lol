"""HTTP transport for durable player-owned roll requests — issue #204."""
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.combat.service import get_encounter
from app.deps.campaign import campaign_for, run_campaign_command
from app.deps.auth import current_profile
from app.deps.idempotency import require_idempotency_key
from app.combat.npc_turns import coordinate_encounter_npc_turn
from app.dm.recovery import execute_committed_attempt
from app.realtime.service import publish_encounter_ready, publish_encounter_turn
from app.rolls.service import (
    RollAuthorizationError, RollLifecycleError, fulfill_roll,
    get_fulfillment, list_roll_requests,
)
from app.threads.service import ThreadAuthorizationError, ThreadNotFoundError, assert_can_read_thread, parse_thread_id
from database import get_db
from models.campaigns import Campaign
from models.dm import DmTurn
from models.dm import PlayerRollRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _id(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Invalid {label}") from exc


def _visible_turn(db: Session, campaign_id: uuid.UUID, turn_id: uuid.UUID, user_id: uuid.UUID) -> DmTurn:
    turn = db.get(DmTurn, turn_id)
    if turn is None or turn.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Turn not found")
    try:
        assert_can_read_thread(db, campaign_id, parse_thread_id(turn.thread_id), user_id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise HTTPException(status_code=404, detail="Turn not found") from exc
    return turn


def _publish_encounter_ready_post_commit(db: Session, result: dict) -> None:
    """Best-effort ready publish for initiative fulfilled via generic rolls.

    Mirrors the encounter router's post-commit pattern: the committed domain
    event is authoritative; this direct publish is latency-only. Runs after the outer idempotency commit so
    replays (stable event ids) stay idempotent.
    """
    ready = result.get("encounter_ready")
    if not isinstance(ready, dict) or not ready.get("ready") or not ready.get("encounter_id"):
        return
    try:
        encounter = get_encounter(db, uuid.UUID(str(ready["encounter_id"])))
        if encounter is None:
            return
        publish_encounter_ready(db, encounter)
        # Readiness opens turn 1 atomically (#231): project it too.
        publish_encounter_turn(db, encounter, "started")
    except Exception:
        logger.warning("generic roll fulfill post-commit ready publish skipped", exc_info=True)


def _request_or_404(db: Session, campaign_id: uuid.UUID, request_id: uuid.UUID) -> PlayerRollRequest:
    row = db.get(PlayerRollRequest, request_id)
    if row is None or row.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Roll request not found")
    return row


@router.get("/api/campaigns/{campaign_id}/roll-requests")
def get_roll_requests(
    request: Request,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    thread_filter = request.query_params.get("thread_id")
    rows = list_roll_requests(db, campaign_id=campaign.id, thread_id=thread_filter)
    visible = []
    for row in rows:
        try:
            assert_can_read_thread(db, campaign.id, parse_thread_id(row.thread_id), profile.id)
        except (ThreadNotFoundError, ThreadAuthorizationError):
            continue
        fulfillment = get_fulfillment(db, row.id)
        item = row.to_dict()
        item["fulfillment"] = fulfillment.to_dict(include_private=row.requested_user_id == profile.id) if fulfillment else None
        visible.append(item)
    return {"roll_requests": visible}


@router.post("/api/campaigns/{campaign_id}/roll-requests/{roll_request_id}/fulfill")
def fulfill_roll_request(
    roll_request_id: str,
    payload: dict,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    rid = _id(roll_request_id, "roll request id")
    row = _request_or_404(db, campaign.id, rid)
    _visible_turn(db, campaign.id, row.turn_id, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            req, fulfillment, resumed, encounter_ready = fulfill_roll(db, request_id=rid, actor_id=profile.id, payload=payload)
            return {
                "roll_request": req.to_dict(),
                "fulfillment": fulfillment.to_dict(include_private=True),
                "resumed_attempt": resumed.to_dict() if resumed else None,
                "encounter_ready": encounter_ready,
            }
        except RollAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RollLifecycleError as exc:
            raise HTTPException(status_code=409 if "status" in str(exc) else 422, detail=str(exc)) from exc

    result = run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="player_roll.fulfill", scope_type="roll_request", scope_id=rid,
        payload=payload, execute=execute,
    )
    _publish_encounter_ready_post_commit(db, result)
    resumed = result.get("resumed_attempt")
    if resumed:
        background_tasks.add_task(execute_committed_attempt, resumed["id"])
    ready = result.get("encounter_ready") or {}
    if ready.get("ready"):
        # Issue #236: an NPC that wins initiative opens with the AI DM's turn.
        npc_attempt = coordinate_encounter_npc_turn(db, ready.get("encounter_id"))
        if npc_attempt:
            background_tasks.add_task(execute_committed_attempt, npc_attempt)
    return result
