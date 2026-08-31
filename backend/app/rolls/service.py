"""Player-owned roll request lifecycle — issue #204.

The DM runtime may request a roll, but only the owner of the requested
character can create its fulfillment. Fulfilled evidence resumes the same
logical DmTurn as a new attempt; it never creates a player submission or turn.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Campaign, CampaignMember, Character, DmTurn, DmTurnAttempt,
    PlayerRollFulfillment, PlayerRollRequest,
)

logger = logging.getLogger(__name__)

ROLL_KINDS = {"check", "save", "attack", "ability", "initiative", "other"}
ADVANTAGE_STATES = {"normal", "advantage", "disadvantage"}


class RollLifecycleError(ValueError):
    pass


class RollAuthorizationError(PermissionError):
    pass


class PendingRollsError(RollLifecycleError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(payload: dict, name: str, maximum: int) -> str:
    value = str(payload.get(name) or "").strip()
    if not value or len(value) > maximum:
        raise RollLifecycleError(f"{name} must be between 1 and {maximum} characters")
    return value


def _uuid(payload: dict, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload.get(name) or ""))
    except ValueError as exc:
        raise RollLifecycleError(f"{name} must be a UUID") from exc


def _lock_turn_attempt(db: Session, turn_id: uuid.UUID, attempt_id: uuid.UUID) -> tuple[DmTurn, DmTurnAttempt]:
    turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
    attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
    if turn is None or attempt is None or attempt.turn_id != turn.id:
        raise RollLifecycleError("Turn or attempt not found")
    if turn.current_attempt_id != attempt.id:
        raise RollLifecycleError("Roll requests must belong to the current turn attempt")
    return turn, attempt


def _validate_request(db: Session, campaign_id: uuid.UUID, payload: dict) -> dict:
    key = _text(payload, "request_key", 48)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise RollLifecycleError("request_key must match [A-Za-z0-9_-]+")
    requested_user_id = _uuid(payload, "requested_user_id")
    character_id = _uuid(payload, "character_id")
    character = db.get(Character, character_id)
    if character is None or character.owner_id != requested_user_id:
        raise RollLifecycleError("character_id must be controlled by requested_user_id")
    campaign = db.get(Campaign, campaign_id)
    member = db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": requested_user_id})
    if campaign is None or (campaign.owner_id != requested_user_id and member is None):
        raise RollLifecycleError("requested_user_id must be a campaign member")
    kind = str(payload.get("roll_kind") or "")
    advantage = str(payload.get("advantage_state") or "normal")
    if kind not in ROLL_KINDS:
        raise RollLifecycleError("roll_kind is invalid")
    if advantage not in ADVANTAGE_STATES:
        raise RollLifecycleError("advantage_state is invalid")
    dc = payload.get("dc_private")
    if dc is not None:
        try:
            dc = int(dc)
        except (TypeError, ValueError) as exc:
            raise RollLifecycleError("dc_private must be an integer") from exc
        if not 1 <= dc <= 1000:
            raise RollLifecycleError("dc_private must be between 1 and 1000")
    return {
        "request_key": key, "requested_user_id": requested_user_id, "character_id": character_id,
        "roll_kind": kind, "ability_or_skill": _text(payload, "ability_or_skill", 64),
        "label": _text(payload, "label", 120), "advantage_state": advantage,
        "reason_public": _text(payload, "reason_public", 600), "dc_private": dc,
    }


def request_rolls(
    db: Session, *, campaign_id: uuid.UUID, turn_id: uuid.UUID, attempt_id: uuid.UUID,
    requests: list[dict], replacement_of_id: uuid.UUID | None = None,
) -> list[PlayerRollRequest]:
    if not requests or len(requests) > 20:
        raise RollLifecycleError("requests must contain between 1 and 20 roll requests")
    turn, attempt = _lock_turn_attempt(db, turn_id, attempt_id)
    if turn.campaign_id != campaign_id:
        raise RollLifecycleError("Turn not found")
    if turn.status not in {"pending", "awaiting_roll"} or attempt.status not in {"prepared", "running", "awaiting_roll"}:
        raise RollLifecycleError("Current attempt cannot request rolls from its present state")
    values = [_validate_request(db, campaign_id, item) for item in requests]
    if len({v["request_key"] for v in values}) != len(values):
        raise RollLifecycleError("request_key values must be unique within the batch")
    rows = [PlayerRollRequest(
        campaign_id=campaign_id, thread_id=turn.thread_id, turn_id=turn.id, attempt_id=attempt.id,
        replacement_of_id=replacement_of_id, **value,
    ) for value in values]
    db.add_all(rows)
    turn.status = "awaiting_roll"
    attempt.status = "awaiting_roll"
    attempt.result = {"mode": "await_roll", "roll_request_ids": [str(row.id) for row in rows]}
    db.flush()
    logger.info("player_roll requested campaign_id=%s turn_id=%s attempt_id=%s count=%s request_ids=%s",
                campaign_id, turn.id, attempt.id, len(rows), [str(row.id) for row in rows])
    return rows


def list_roll_requests(db: Session, *, campaign_id: uuid.UUID, thread_id: str | None = None) -> list[PlayerRollRequest]:
    query = select(PlayerRollRequest).where(PlayerRollRequest.campaign_id == campaign_id)
    if thread_id is not None:
        query = query.where(PlayerRollRequest.thread_id == str(thread_id))
    return list(db.execute(query.order_by(PlayerRollRequest.requested_at, PlayerRollRequest.id)).scalars().all())


def get_fulfillment(db: Session, request_id: uuid.UUID) -> PlayerRollFulfillment | None:
    return db.execute(select(PlayerRollFulfillment).where(PlayerRollFulfillment.roll_request_id == request_id)).scalars().first()


def has_pending_rolls(db: Session, turn_id: uuid.UUID) -> bool:
    return db.execute(select(PlayerRollRequest.id).where(
        PlayerRollRequest.turn_id == turn_id, PlayerRollRequest.status == "pending"
    ).limit(1)).first() is not None


def _resume_if_unblocked(db: Session, turn: DmTurn, parent_attempt: DmTurnAttempt) -> DmTurnAttempt | None:
    if has_pending_rolls(db, turn.id):
        return None
    requests = list(db.execute(select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id).order_by(
        PlayerRollRequest.requested_at, PlayerRollRequest.id
    )).scalars().all())
    evidence = []
    for req in requests:
        item = req.to_dict(include_private=True)
        fulfillment = get_fulfillment(db, req.id)
        item["fulfillment"] = fulfillment.to_dict(include_private=True) if fulfillment else None
        evidence.append(item)
    parent_attempt.status = "superseded"
    parent_attempt.invalidation_reason = "player_roll_input_available"
    parent_attempt.invalidated_at = _now()
    campaign = db.get(Campaign, turn.campaign_id)
    next_attempt = DmTurnAttempt(
        turn_id=turn.id, attempt_number=parent_attempt.attempt_number + 1, status="prepared",
        campaign_id=turn.campaign_id, thread_id=turn.thread_id, audience=turn.audience,
        source_revision=int(campaign.revision if campaign else parent_attempt.source_revision),
        input_set_revision=turn.input_set_revision, submission_ids=list(turn.submission_ids or []),
        parent_attempt_id=parent_attempt.id, roll_evidence=evidence,
        assembly_window_start=turn.assembly_window_start, assembly_window_end=turn.assembly_window_end,
    )
    db.add(next_attempt)
    db.flush()
    turn.status = "pending"
    turn.current_attempt_id = next_attempt.id
    turn.streaming_attempt_id = None
    logger.info("player_roll turn_resumed campaign_id=%s turn_id=%s old_attempt_id=%s new_attempt_id=%s evidence_count=%s",
                turn.campaign_id, turn.id, parent_attempt.id, next_attempt.id, len(evidence))
    return next_attempt


def fulfill_roll(db: Session, *, request_id: uuid.UUID, actor_id: uuid.UUID, payload: dict) -> tuple[PlayerRollRequest, PlayerRollFulfillment, DmTurnAttempt | None]:
    started = time.monotonic()
    req = db.execute(select(PlayerRollRequest).where(PlayerRollRequest.id == request_id).with_for_update()).scalars().first()
    if req is None:
        raise RollLifecycleError("Roll request not found")
    if req.requested_user_id != actor_id:
        logger.warning("player_roll invalid_attempt request_id=%s actor_id=%s reason=unauthorized", request_id, actor_id)
        raise RollAuthorizationError("Only the requested character's controller may fulfill this roll")
    character = db.get(Character, req.character_id)
    if character is None or character.owner_id != actor_id:
        raise RollAuthorizationError("Character control changed; roll cannot be fulfilled")
    if req.status != "pending":
        raise RollLifecycleError(f"Roll request cannot be fulfilled from status {req.status}")
    source = str(payload.get("source") or "")
    visibility = str(payload.get("visibility") or "public")
    if source not in {"app", "physical"}:
        raise RollLifecycleError("source must be app or physical")
    if visibility not in {"public", "private"}:
        raise RollLifecycleError("visibility must be public or private")
    raw_rolls = payload.get("raw_rolls", [])
    if not isinstance(raw_rolls, list) or len(raw_rolls) > 20 or any(type(v) is not int or not 1 <= v <= 1000 for v in raw_rolls):
        raise RollLifecycleError("raw_rolls must be a list of up to 20 integers between 1 and 1000")
    if source == "app" and not raw_rolls:
        raise RollLifecycleError("app rolls require raw_rolls supplied by the player client")
    try:
        modifier = int(payload.get("modifier", 0))
        total = int(payload["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RollLifecycleError("modifier and total must be integers; total is required") from exc
    if not -10000 <= modifier <= 10000 or not -10000 <= total <= 10000:
        raise RollLifecycleError("modifier and total must be between -10000 and 10000")
    metadata = payload.get("raw_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise RollLifecycleError("raw_metadata must be an object")
    fulfillment = PlayerRollFulfillment(
        roll_request_id=req.id, submitted_by=actor_id, source=source, visibility=visibility,
        raw_rolls=raw_rolls, modifier=modifier, total=total, raw_metadata=metadata,
    )
    db.add(fulfillment)
    req.status = "fulfilled"
    req.fulfilled_at = _now()
    db.flush()
    turn = db.get(DmTurn, req.turn_id)
    attempt = db.get(DmTurnAttempt, req.attempt_id)
    if turn is None or attempt is None:
        raise RollLifecycleError("Owning turn or attempt no longer exists")
    resumed = _resume_if_unblocked(db, turn, attempt)
    logger.info("player_roll fulfilled request_id=%s turn_id=%s source=%s visibility=%s latency_ms=%.2f resumed=%s",
                req.id, req.turn_id, source, visibility, (time.monotonic() - started) * 1000, bool(resumed))
    return req, fulfillment, resumed


def cancel_or_replace(db: Session, *, request_id: uuid.UUID, replacement: dict | None) -> tuple[PlayerRollRequest, list[PlayerRollRequest], DmTurnAttempt | None]:
    req = db.execute(select(PlayerRollRequest).where(PlayerRollRequest.id == request_id).with_for_update()).scalars().first()
    if req is None:
        raise RollLifecycleError("Roll request not found")
    if req.status != "pending":
        raise RollLifecycleError(f"Roll request cannot be changed from status {req.status}")
    turn = db.get(DmTurn, req.turn_id)
    attempt = db.get(DmTurnAttempt, req.attempt_id)
    if turn is None or attempt is None:
        raise RollLifecycleError("Owning turn or attempt no longer exists")
    req.status = "replaced" if replacement else "cancelled"
    req.cancelled_at = _now()
    created: list[PlayerRollRequest] = []
    if replacement:
        created = request_rolls(db, campaign_id=req.campaign_id, turn_id=turn.id, attempt_id=attempt.id,
                                requests=[replacement], replacement_of_id=req.id)
    resumed = None if created else _resume_if_unblocked(db, turn, attempt)
    logger.info("player_roll %s request_id=%s turn_id=%s replacement_ids=%s",
                req.status, req.id, turn.id, [str(item.id) for item in created])
    return req, created, resumed
