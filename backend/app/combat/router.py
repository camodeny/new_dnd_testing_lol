"""HTTP transport for the authoritative encounter lifecycle — issue #230,
turn progression — issue #231, map geometry/movement — issue #232."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns.auth import authorized_campaign, require_owner
from app.campaigns.service import CampaignArchivedError
from app.combat.maps import (
    MapAuthorizationError,
    MapError,
    ensure_map,
    get_map,
    map_projection,
    move_participant,
    reachable_for,
    update_terrain,
)
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
from app.combat.turns import (
    StaleTurnError,
    TurnAuthorizationError,
    TurnError,
    cast_skip_vote,
    consume_resource,
    end_turn,
    turn_projection,
)
from app.combat.ending import (
    EndEncounterAuthorizationError,
    EndEncounterError,
    end_encounter,
    list_end_followups,
    process_end_followup,
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


def _publish_post_commit(db: Session, result: dict, *, replayed: bool = False) -> None:
    """Best-effort realtime delivery after the outer idempotency commit.

    The durable outbox row (enqueued atomically with the mutation) is the
    guaranteed realtime hook; this direct publish is latency-only and never
    rolls back authoritative state. Stable event ids make replays idempotent.

    On ``X-Idempotent-Replay: true`` the stored result is returned without
    advancing state, so rebuilding turn events from the encounter's current
    row could publish a false transition for a later turn (e.g. replaying an
    old end-turn after a later skip). The outbox already owns delivery there,
    so replays never trigger the direct publish.
    """
    if replayed:
        return
    try:
        from app.realtime.service import (
            publish_encounter_ended,
            publish_encounter_ready,
            publish_encounter_started,
            publish_encounter_turn,
        )

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
            # Readiness opens turn 1 atomically (#231): project it too.
            publish_encounter_turn(db, encounter, "started")
        if result.get("ended_event") is not None:
            publish_encounter_turn(db, encounter, "ended")
        if result.get("skipped_event") is not None:
            publish_encounter_turn(db, encounter, "skipped")
        if result.get("started_event") is not None:
            publish_encounter_turn(db, encounter, "started")
        if result.get("encounter_ended_event") is not None:
            publish_encounter_ended(db, encounter)
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
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
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
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
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
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
    return result


# ── Turn progression — issue #231 ────────────────────────────────────────────


def _turn_http_error(exc: Exception) -> HTTPException:
    """Map deterministic turn failures to transport status (never 500)."""
    if isinstance(exc, StaleTurnError):
        return HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Turn-Sequence": str(exc.actual_sequence)},
        )
    if isinstance(exc, (TurnAuthorizationError, EncounterAuthorizationError, MapAuthorizationError, EndEncounterAuthorizationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, CampaignArchivedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (TurnError, EncounterError, MapError)):
        message = str(exc)
        status = 409 if any(
            token in message for token in ("status", "already", "advanced", "pending", "blocking")
        ) else 422
        return HTTPException(status_code=status, detail=message)
    if isinstance(exc, RevisionConflictError):
        return HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        )
    return HTTPException(status_code=422, detail=str(exc))


def _require_revision(payload: dict) -> int:
    expected_revision = payload.get("expected_revision")
    if expected_revision is None:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        return int(expected_revision)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-state")
def read_turn_state(campaign_id: str, encounter_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    projection = turn_projection(
        db, encounter, viewer_id=profile.id,
        is_owner=campaign.owner_id == profile.id,
    )
    if projection is None:
        raise HTTPException(status_code=409, detail="turn state becomes available when required initiative is complete")
    return {"encounter_id": str(encounter.id), "turn": projection}


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-turn")
def post_end_turn(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)
    if payload.get("expected_turn_sequence") is None:
        raise HTTPException(status_code=400, detail="expected_turn_sequence is required")
    expected_sequence = payload["expected_turn_sequence"]

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit so the
            # record, state change, and both turn events commit atomically.
            updated, ended_event, started_event = end_turn(
                db, encounter.id, actor_id=profile.id,
                expected_turn_sequence=expected_sequence,
                expected_revision=expected_revision, commit=False,
            )
            return {
                "encounter": _viewer_view(db, updated, profile.id, is_owner=campaign.owner_id == profile.id),
                "ended_event": ended_event.to_dict() if hasattr(ended_event, "to_dict") else None,
                "started_event": started_event.to_dict() if hasattr(started_event, "to_dict") else None,
                "turn_sequence": int(updated.turn_sequence or 0),
            }
        except Exception as exc:
            raise _turn_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.end_turn", scope_type="encounter", scope_id=encounter.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
    return result


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/skip-votes")
def post_skip_vote(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    target_raw = payload.get("target_participant_id")
    if not target_raw:
        raise HTTPException(status_code=422, detail="target_participant_id is required")
    target_id = _id(str(target_raw), "participant id")
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)
    if payload.get("expected_turn_sequence") is None:
        raise HTTPException(status_code=400, detail="expected_turn_sequence is required")
    expected_sequence = payload["expected_turn_sequence"]

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit.
            tally, executed, updated, skipped_event, started_event = cast_skip_vote(
                db, encounter.id, target_id, voter_id=profile.id,
                expected_revision=expected_revision,
                expected_turn_sequence=expected_sequence, commit=False,
            )
            return {
                "encounter": _viewer_view(db, updated, profile.id, is_owner=campaign.owner_id == profile.id),
                "tally": tally,
                "executed": executed,
                "skipped_event": skipped_event.to_dict() if skipped_event is not None and hasattr(skipped_event, "to_dict") else None,
                "started_event": started_event.to_dict() if started_event is not None and hasattr(started_event, "to_dict") else None,
            }
        except Exception as exc:
            raise _turn_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.skip_vote", scope_type="encounter", scope_id=encounter.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
    return result


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-resources/consume")
def post_consume_resource(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    participant_raw = payload.get("participant_id")
    if not participant_raw:
        raise HTTPException(status_code=422, detail="participant_id is required")
    participant_id = _id(str(participant_raw), "participant id")
    resource = payload.get("resource")
    if not resource:
        raise HTTPException(status_code=422, detail="resource is required")
    if payload.get("expected_turn_sequence") is None:
        raise HTTPException(status_code=400, detail="expected_turn_sequence is required")
    expected_sequence = payload["expected_turn_sequence"]
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit.
            state = consume_resource(
                db, encounter.id, participant_id, actor_id=profile.id,
                resource=str(resource), amount=payload.get("amount", 1),
                expected_turn_sequence=expected_sequence, commit=False,
            )
            return {
                "encounter": _viewer_view(db, encounter, profile.id, is_owner=campaign.owner_id == profile.id),
                "participant_id": str(participant_id),
                "resource": str(resource),
                "turn_state": state.to_dict(),
            }
        except Exception as exc:
            raise _turn_http_error(exc) from exc

    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="encounter.consume", scope_type="encounter_participant", scope_id=participant_id,
        payload=payload, execute=execute,
    )
    return result


# ── Map geometry / terrain / movement — issue #232 ───────────────────────────


def _map_http_error(exc: Exception) -> HTTPException:
    """Map deterministic movement failures to transport status (never 500)."""
    if isinstance(exc, StaleTurnError):
        return HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Turn-Sequence": str(exc.actual_sequence)},
        )
    if isinstance(exc, (MapAuthorizationError, TurnAuthorizationError, EncounterAuthorizationError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, CampaignArchivedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (MapError, TurnError, EncounterError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, RevisionConflictError):
        return HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        )
    return HTTPException(status_code=422, detail=str(exc))


def _publish_map_post_commit(db: Session, result: dict, encounter_id: str, *, replayed: bool = False) -> None:
    """Best-effort map/movement realtime delivery after the outer commit."""
    if replayed:
        return
    try:
        from app.realtime.service import publish_encounter_map, publish_encounter_moved

        encounter = get_encounter(db, uuid.UUID(str(encounter_id)))
        if encounter is None:
            return
        if result.get("map_event") is not None:
            publish_encounter_map(db, encounter)
        if result.get("move") is not None:
            publish_encounter_moved(
                db, encounter, uuid.UUID(str(result["move"]["participant_id"])),
                move_id=result["move"].get("id"),
            )
    except Exception:
        logger.warning("map post-commit publish skipped", exc_info=True)


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/map", status_code=201)
def init_encounter_map(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)
    if payload.get("width") is None or payload.get("height") is None:
        raise HTTPException(status_code=422, detail="width and height are required")

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit.
            encounter_map, event = ensure_map(
                db, encounter.id, actor_id=profile.id,
                width=payload["width"], height=payload["height"],
                diagonal_policy=payload.get("diagonal_policy") or "no_corner_cut",
                background_art_ref=payload.get("background_art_ref"),
                terrain=payload.get("terrain") or [],
                placements=payload.get("placements"),
                expected_revision=expected_revision,
                operation_id=key, commit=False,
            )
            return {
                "map": map_projection(db, encounter, viewer_id=profile.id, is_owner=True),
                "map_event": event.to_dict() if hasattr(event, "to_dict") else None,
            }
        except Exception as exc:
            raise _map_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.map_init", scope_type="encounter", scope_id=encounter.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_map_post_commit(db, result, str(encounter.id),
                             replayed=response.headers.get("X-Idempotent-Replay") == "true")
    return result


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/terrain")
def change_encounter_terrain(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit.
            encounter_map, event = update_terrain(
                db, encounter.id, actor_id=profile.id,
                zones=payload.get("zones") or [],
                clear_zone_ids=payload.get("clear_zone_ids") or [],
                expected_revision=expected_revision,
                operation_id=key, commit=False,
            )
            return {
                "map": map_projection(db, encounter, viewer_id=profile.id, is_owner=True),
                "map_event": event.to_dict() if hasattr(event, "to_dict") else None,
            }
        except Exception as exc:
            raise _map_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.terrain_change", scope_type="encounter", scope_id=encounter.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_map_post_commit(db, result, str(encounter.id),
                             replayed=response.headers.get("X-Idempotent-Replay") == "true")
    return result


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}/map")
def read_encounter_map(campaign_id: str, encounter_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    projection = map_projection(
        db, encounter, viewer_id=profile.id,
        is_owner=campaign.owner_id == profile.id,
    )
    if projection is None:
        raise HTTPException(status_code=404, detail="Encounter has no map yet")
    return {"encounter_id": str(encounter.id), "map": projection}


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}/reachable")
def read_reachable(campaign_id: str, encounter_id: str, participant_id: str, request: Request, movement_mode: str = "walk", db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    try:
        return reachable_for(
            db, encounter.id, _id(participant_id, "participant id"),
            movement_mode=movement_mode,
            viewer_id=profile.id,
            is_owner=campaign.owner_id == profile.id,
        )
    except Exception as exc:
        raise _map_http_error(exc) from exc


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/move")
def post_move(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    participant_raw = payload.get("participant_id")
    if not participant_raw:
        raise HTTPException(status_code=422, detail="participant_id is required")
    participant_id = _id(str(participant_raw), "participant id")
    destination = payload.get("to") or {}
    if destination.get("col") is None or destination.get("row") is None:
        raise HTTPException(status_code=422, detail="to.col and to.row are required")
    if payload.get("expected_turn_sequence") is None:
        raise HTTPException(status_code=400, detail="expected_turn_sequence is required")
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit so the
            # placement write, budget debit, ledger insert, and moved event
            # commit atomically.
            move, updated, event = move_participant(
                db, encounter.id, participant_id, actor_id=profile.id,
                to_col=destination["col"], to_row=destination["row"],
                movement_mode=payload.get("movement_mode") or "walk",
                expected_turn_sequence=payload["expected_turn_sequence"],
                expected_revision=expected_revision,
                operation_id=key, commit=False,
            )
            return {
                "move": move.to_dict(),
                "encounter": _viewer_view(db, updated, profile.id, is_owner=campaign.owner_id == profile.id),
                "moved_event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            }
        except Exception as exc:
            raise _map_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.move", scope_type="encounter_participant", scope_id=participant_id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_map_post_commit(db, result, str(encounter.id),
                             replayed=response.headers.get("X-Idempotent-Replay") == "true")
    return result


# ── DM-controlled encounter end + post-combat hooks — issue #239 ─────────────


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/end")
def post_end_encounter(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    # The AI is the only DM: ending runs on the campaign-owner path, never a
    # separate human DM role.
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    key = require_idempotency_key(request, payload.get("operation_id"))
    expected_revision = _require_revision(payload)
    for field in ("outcome", "reason"):
        if not payload.get(field):
            raise HTTPException(status_code=422, detail=f"{field} is required")

    def execute():
        try:
            # Flush-only: the outer idempotent command owns the commit so the
            # transition, death writes, hook rows, and ended event commit
            # atomically — failures leave the encounter active, never
            # half-closed.
            updated, event, hooks = end_encounter(
                db, encounter.id, actor_id=profile.id,
                outcome=payload["outcome"], reason=payload["reason"],
                participant_outcomes=payload.get("participant_outcomes"),
                expected_revision=expected_revision, operation_id=key,
                commit=False,
            )
            return {
                "encounter": _viewer_view(db, updated, profile.id, is_owner=True),
                "encounter_ended_event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
                "followups": [h.to_dict() for h in hooks],
            }
        except Exception as exc:
            raise _turn_http_error(exc) from exc

    try:
        result = execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=key,
            command_type="encounter.end", scope_type="encounter", scope_id=encounter.id,
            payload=payload, execute=execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    _publish_post_commit(
        db, result,
        replayed=response.headers.get("X-Idempotent-Replay") == "true",
    )
    return result


@router.get("/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-followups")
def read_end_followups(campaign_id: str, encounter_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    # Issue #239 privacy: hook results carry post-combat custody/death/loot
    # detail (participant IDs) that can name hidden NPC fates — owner-only
    # bookkeeping, matching the process endpoint below. Members converge via
    # the redacted encounter view (public outcome) instead.
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    return {
        "encounter_id": str(encounter.id),
        "status": encounter.status,
        "followups": [h.to_dict() for h in list_end_followups(db, encounter.id)],
    }


@router.post("/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-followups/process")
def post_process_end_followup(campaign_id: str, encounter_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    require_owner(campaign, profile.id)
    encounter = _encounter_or_404(db, campaign.id, _id(encounter_id, "encounter id"))
    _assert_encounter_visible(db, encounter, profile.id)
    hook_type = payload.get("hook_type")
    if not hook_type:
        raise HTTPException(status_code=422, detail="hook_type is required")
    if payload.get("result") is None and payload.get("fail_reason") is None:
        raise HTTPException(status_code=422, detail="result or fail_reason is required")
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            # Flush-only under the idempotency guard. Hook completion/failure
            # is a non-fictional ledger write (no revision bump); it never
            # reopens or invalidates the ended encounter.
            row = process_end_followup(
                db, encounter.id, str(hook_type),
                result=payload.get("result"),
                fail_reason=payload.get("fail_reason"),
                commit=False,
            )
            return {
                "encounter": _viewer_view(db, encounter, profile.id, is_owner=True),
                "followup": row.to_dict(),
            }
        except Exception as exc:
            raise _turn_http_error(exc) from exc

    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="encounter.end_followup", scope_type="encounter", scope_id=encounter.id,
        payload=payload, execute=execute,
    )
    return result
