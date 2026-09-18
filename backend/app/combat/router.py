"""HTTP transport for the authoritative encounter lifecycle — issue #230."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns.auth import authorized_campaign, require_owner
from app.campaigns.service import CampaignArchivedError
from app.combat.service import (
    EncounterAlreadyActiveError,
    EncounterAuthorizationError,
    EncounterError,
    EncounterNotReadyError,
    can_view_encounter,
    encounter_view,
    fulfill_human_initiative,
    get_active_encounter,
    get_encounter,
    get_turn_order,
    list_participants,
    roll_npc_initiative,
    start_encounter,
)
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from app.campaigns.events import RevisionConflictError
from app.runtime.threads import ThreadNotFoundError
from database import get_db
from models.combat import Encounter

logger = logging.getLogger(__name__)

router = APIRouter()


def _id(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Invalid {label}") from exc


def _encounter_or_404(db: Session, campaign_id: uuid.UUID, encounter_id: uuid.UUID) -> Encounter:
    encounter = get_encounter(db, encounter_id)
    if encounter is None or encounter.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return encounter


def _viewer_view(db: Session, encounter: Encounter, viewer_id: uuid.UUID, is_owner: bool) -> dict:
    return encounter_view(db, encounter, viewer_id, is_owner=is_owner)


def _assert_encounter_visible(db: Session, encounter: Encounter, viewer_id: uuid.UUID) -> None:
    """Thread-scoped encounter read gate (#230 privacy).

    Encounters inherit the source turn's thread; private-thread encounters
    stay invisible to non-readers. Fail closed as 404, matching the other
    thread-scoped reads.
    """
    if not can_view_encounter(db, encounter, viewer_id):
        raise HTTPException(status_code=404, detail="Encounter not found")


def _publish_post_commit(db: Session, result: dict) -> None:
    """Best-effort realtime delivery after the outer idempotency commit.

    The durable outbox row (enqueued atomically with the mutation) is the
    guaranteed realtime hook; this direct publish is latency-only and never
    rolls back authoritative state. Stable event ids make replays idempotent.
    """
    try:
        from app.realtime.service import publish_encounter_ready, publish_encounter_started

        encounter_id = (result.get("encounter") or {}).get("id")
        if not encounter_id:
            return
        encounter = get_encounter(db, uuid.UUID(str(encounter_id)))
        if encounter is None:
            return
        if result.get("event") is not None:
            publish_encounter_started(db, encounter)
        if result.get("ready_event") is not None:
            publish_encounter_ready(db, encounter)
    except Exception:
        logger.warning("encounter post-commit publish skipped", exc_info=True)


@router.post("/api/campaigns/{campaign_id}/encounters", status_code=201)
def create_encounter(campaign_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    require_owner(campaign, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = payload.get("expected_revision")
    if expected_revision is None:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    for field in ("source_turn_id", "source_attempt_id"):
        if not payload.get(field):
            raise HTTPException(status_code=422, detail=f"{field} is required")

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit so the
            # record, mutation, and result commit atomically (no crash window
            # with a stuck in_progress row).
            encounter, event = start_encounter(
                db,
                campaign.id,
                operation_id=key,
                expected_revision=expected_revision,
                actor_id=profile.id,
                source_turn_id=payload["source_turn_id"],
                source_attempt_id=payload["source_attempt_id"],
                scene=payload.get("scene"),
                participants=payload.get("participants") or [],
                start_source="api",
                commit=False,
            )
            return {
                "encounter": _viewer_view(db, encounter, profile.id, is_owner=True),
                "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            }
        except EncounterAlreadyActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RevisionConflictError as exc:
            raise HTTPException(
                status_code=409, detail=str(exc),
                headers={"X-Current-Revision": str(exc.actual_revision)},
            ) from exc
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Source turn not found") from exc
        except EncounterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.start", scope_type="campaign", scope_id=campaign.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_post_commit(db, result)
    return result


@router.get("/api/campaigns/{campaign_id}/encounters/active")
def read_active_encounter(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = get_active_encounter(db, campaign.id)
    if encounter is None or not can_view_encounter(db, encounter, profile.id):
        return {"encounter": None}
    return {"encounter": _viewer_view(db, encounter, profile.id, is_owner=campaign.owner_id == profile.id)}


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}")
def read_encounter(campaign_id: str, encounter_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    return {"encounter": _viewer_view(db, encounter, profile.id, is_owner=campaign.owner_id == profile.id)}


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-order")
def read_turn_order(campaign_id: str, encounter_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    is_owner = campaign.owner_id == profile.id
    try:
        ordered = get_turn_order(db, encounter.id)
    except EncounterNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    order = []
    for p in ordered:
        # Same participant redaction as encounter_view(): another PC's
        # roll_request_id stays with its controller (or the owner).
        privileged = is_owner or str(p.controller_user_id or "") == str(profile.id)
        item = p.to_dict(include_private=privileged)
        if not privileged:
            item.pop("roll_request_id", None)
        order.append(item)
    return {
        "encounter_id": str(encounter.id),
        "status": encounter.status,
        "round": encounter.round,
        "active_participant_id": str(encounter.active_participant_id) if encounter.active_participant_id else None,
        "order": order,
    }


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/initiative/fulfill")
def fulfill_initiative(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    participant_raw = payload.get("participant_id")
    if not participant_raw:
        raise HTTPException(status_code=422, detail="participant_id is required")
    participant_id = _id(str(participant_raw), "participant id")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            req, fulfillment, participant, updated, event = fulfill_human_initiative(
                db, encounter.id, participant_id, actor_id=profile.id, payload=payload,
                # Flush-only: the outer idempotent command owns the commit.
                commit=False,
            )
            return {
                "roll_request": req.to_dict(),
                "fulfillment": fulfillment.to_dict(include_private=True),
                "participant": participant.to_dict(include_private=True),
                "encounter": _viewer_view(
                    db, updated, profile.id, is_owner=campaign.owner_id == profile.id
                ),
                "ready_event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            }
        except EncounterAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EncounterError as exc:
            message = str(exc)
            raise HTTPException(
                status_code=409 if "status" in message or "already" in message else 422,
                detail=message,
            ) from exc

    participant = next(
        (p for p in list_participants(db, encounter.id) if p.id == participant_id), None
    )
    scope_id = participant.roll_request_id if participant and participant.roll_request_id else participant_id
    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="encounter.initiative_fulfill", scope_type="encounter_participant", scope_id=scope_id,
        payload=payload, execute=execute,
    )
    _publish_post_commit(db, result)
    return result


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/initiative/roll-npc")
def roll_npc(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    participant_raw = payload.get("participant_id")
    if not participant_raw:
        raise HTTPException(status_code=422, detail="participant_id is required")
    participant_id = _id(str(participant_raw), "participant id")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            participant, updated, event = roll_npc_initiative(
                db, encounter.id, participant_id, raw_d20=payload.get("raw_d20"),
                # Flush-only: the outer idempotent command owns the commit.
                commit=False,
            )
            return {
                "participant": participant.to_dict(include_private=True),
                "encounter": _viewer_view(db, updated, profile.id, is_owner=True),
                "ready_event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            }
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EncounterError as exc:
            message = str(exc)
            raise HTTPException(
                status_code=409 if "status" in message or "already" in message else 422,
                detail=message,
            ) from exc

    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="encounter.npc_roll", scope_type="encounter_participant", scope_id=participant_id,
        payload=payload, execute=execute,
    )
    _publish_post_commit(db, result)
    return result
