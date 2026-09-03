"""HTTP transport for durable player-owned roll requests — issue #204."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from app.dm.router import _authorized_campaign, _require_owner
from app.rolls.service import (
    RollAuthorizationError, RollLifecycleError, cancel_or_replace, fulfill_roll,
    get_fulfillment, list_roll_requests, request_rolls,
)
from app.runtime.threads import ThreadAuthorizationError, ThreadNotFoundError, assert_can_read_thread, parse_thread_id
from database import get_db
from models.dm import DmTurn
from models.dm import PlayerRollRequest

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


def _request_or_404(db: Session, campaign_id: uuid.UUID, request_id: uuid.UUID) -> PlayerRollRequest:
    row = db.get(PlayerRollRequest, request_id)
    if row is None or row.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Roll request not found")
    return row


@router.post("/api/campaigns/{campaign_id}/dm-turns/{turn_id}/roll-requests", status_code=201)
def create_roll_requests(campaign_id: str, turn_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    _require_owner(campaign, profile.id)
    tid = _id(turn_id, "turn id")
    turn = _visible_turn(db, campaign.id, tid, profile.id)
    attempt_raw = payload.get("attempt_id")
    if not attempt_raw:
        raise HTTPException(status_code=422, detail="attempt_id is required")
    try:
        attempt_id = uuid.UUID(str(attempt_raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid attempt_id") from exc
    raw_requests = payload.get("requests")
    if raw_requests is None and isinstance(payload.get("request"), dict):
        raw_requests = [payload["request"]]
    if not isinstance(raw_requests, list) or any(not isinstance(item, dict) for item in raw_requests):
        raise HTTPException(status_code=422, detail="requests must be a list of objects")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            rows = request_rolls(db, campaign_id=campaign.id, turn_id=turn.id, attempt_id=attempt_id, requests=raw_requests)
            return {"roll_requests": [row.to_dict(include_private=True) for row in rows], "turn_id": str(turn.id)}
        except RollLifecycleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="player_roll.request", scope_type="dm_turn", scope_id=turn.id,
        payload=payload, execute=execute,
    )


@router.get("/api/campaigns/{campaign_id}/roll-requests")
def get_roll_requests(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    thread_filter = request.query_params.get("thread_id")
    rows = list_roll_requests(db, campaign_id=campaign.id, thread_id=thread_filter)
    visible = []
    for row in rows:
        try:
            assert_can_read_thread(db, campaign.id, parse_thread_id(row.thread_id), profile.id)
        except (ThreadNotFoundError, ThreadAuthorizationError):
            continue
        fulfillment = get_fulfillment(db, row.id)
        include_private = campaign.owner_id == profile.id
        item = row.to_dict(include_private=include_private)
        item["fulfillment"] = fulfillment.to_dict(include_private=include_private or row.requested_user_id == profile.id) if fulfillment else None
        visible.append(item)
    return {"roll_requests": visible}


@router.post("/api/campaigns/{campaign_id}/roll-requests/{roll_request_id}/fulfill")
def fulfill_roll_request(campaign_id: str, roll_request_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    rid = _id(roll_request_id, "roll request id")
    row = _request_or_404(db, campaign.id, rid)
    _visible_turn(db, campaign.id, row.turn_id, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            req, fulfillment, resumed = fulfill_roll(db, request_id=rid, actor_id=profile.id, payload=payload)
            return {
                "roll_request": req.to_dict(),
                "fulfillment": fulfillment.to_dict(include_private=True),
                "resumed_attempt": resumed.to_dict() if resumed else None,
            }
        except RollAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RollLifecycleError as exc:
            raise HTTPException(status_code=409 if "status" in str(exc) else 422, detail=str(exc)) from exc

    return execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="player_roll.fulfill", scope_type="roll_request", scope_id=rid,
        payload=payload, execute=execute,
    )


@router.post("/api/campaigns/{campaign_id}/roll-requests/{roll_request_id}/cancel")
def cancel_roll_request(campaign_id: str, roll_request_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    _require_owner(campaign, profile.id)
    rid = _id(roll_request_id, "roll request id")
    row = _request_or_404(db, campaign.id, rid)
    _visible_turn(db, campaign.id, row.turn_id, profile.id)
    replacement = payload.get("replacement")
    if replacement is not None and not isinstance(replacement, dict):
        raise HTTPException(status_code=422, detail="replacement must be an object")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            old, created, resumed = cancel_or_replace(db, request_id=rid, replacement=replacement)
            return {
                "roll_request": old.to_dict(include_private=True),
                "replacement": created[0].to_dict(include_private=True) if created else None,
                "resumed_attempt": resumed.to_dict() if resumed else None,
            }
        except RollLifecycleError as exc:
            raise HTTPException(status_code=409 if "status" in str(exc) else 422, detail=str(exc)) from exc

    return execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="player_roll.cancel_or_replace", scope_type="roll_request", scope_id=rid,
        payload=payload, execute=execute,
    )
