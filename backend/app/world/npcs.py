"""Progressive, durable NPC state — issue #215.

Deterministic domain code owns state, visibility, and revisions. This module
never advances state from wall-clock time and never asks a model to apply
side effects.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.world.service import get_entity_strict
from models.campaigns import Campaign
from models.world import NPCState

UNSET: Any = object()

IMPORTANCE = ("incidental", "supporting", "major")
STATE_FIELDS = frozenset({
    "role", "goals", "disposition", "resources", "current_activity",
    "location_entity_id", "location_name", "importance", "depth",
})
VISIBILITIES = frozenset({"public", "campaign", "dm_only"})


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _provenance(value: Any, *, source_turn_id: Any, source_attempt_id: Any, source_event_id: Any) -> dict:
    if not isinstance(value, dict) or not str(value.get("source") or "").strip():
        raise ValueError("provenance.source is required for NPC state changes")
    if not any((source_turn_id, source_attempt_id, source_event_id)):
        raise ValueError("NPC state changes require a committed turn, attempt, or event source")
    return dict(value)


def _resolve_npc_source_refs(
    db: Session, campaign_id: uuid.UUID, *, turn_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None, event_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    """Resolve source refs by existence/campaign/link — no status gate.

    The caller owns the surrounding transaction (turn-commit mutate,
    post-turn range), so cited rows may still be mid-flight — e.g. a
    ``streaming`` turn whose commit owns this write.
    """
    from models.campaigns import CampaignDomainEvent
    from models.dm import DmTurn, DmTurnAttempt

    resolved_event: uuid.UUID | None = None
    if event_id is not None:
        event = db.get(CampaignDomainEvent, event_id)
        if event is None or event.campaign_id != campaign_id:
            raise ValueError(f"source_event {event_id} not found in campaign {campaign_id}")
        resolved_event = event.id
    resolved_turn: uuid.UUID | None = None
    if turn_id is not None:
        turn = db.get(DmTurn, turn_id)
        if turn is None or turn.campaign_id != campaign_id:
            raise ValueError(f"source_turn {turn_id} not found in campaign {campaign_id}")
        resolved_turn = turn.id
    resolved_attempt: uuid.UUID | None = None
    if attempt_id is not None:
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is None or attempt.campaign_id != campaign_id:
            raise ValueError(f"source_attempt {attempt_id} not found in campaign {campaign_id}")
        resolved_attempt = attempt.id
        if resolved_turn is not None and str(attempt.turn_id) != str(resolved_turn):
            raise ValueError(f"source_attempt {attempt_id} does not belong to source_turn {turn_id}")
    return resolved_turn, resolved_attempt, resolved_event


def _visibility_map(value: Any, current: dict | None = None) -> dict:
    result = dict(current or {})
    if value is UNSET:
        return result
    if not isinstance(value, dict):
        raise ValueError("field_visibility must be an object")
    for field, visibility in value.items():
        if field not in STATE_FIELDS:
            raise ValueError(f"unknown NPC field visibility: {field}")
        normalized = str(visibility).strip().lower()
        if normalized not in VISIBILITIES:
            raise ValueError(f"field visibility must be one of {sorted(VISIBILITIES)}")
        result[field] = normalized
    return result


def get_npc_state(db: Session, campaign_id: Any, entity_id: Any) -> NPCState | None:
    row = db.get(NPCState, _uuid(entity_id, "entity_id"))
    return row if row is not None and row.campaign_id == _uuid(campaign_id, "campaign_id") else None


def apply_npc_state(
    db: Session, campaign: Campaign, entity_id: Any, *, new_revision: int,
    role: Any = UNSET, goals: Any = UNSET, disposition: Any = UNSET,
    resources: Any = UNSET, current_activity: Any = UNSET,
    location_entity_id: Any = UNSET, location_name: Any = UNSET,
    importance: Any = UNSET, depth: Any = UNSET, field_visibility: Any = UNSET,
    provenance: dict | None = None, source_turn_id: Any = None,
    source_attempt_id: Any = None, source_event_id: Any = None,
    operation_id: str | None = None,
) -> NPCState:
    """Create or progressively enrich NPC state inside the caller's transaction.

    Flushes; never commits. Records the current turn/attempt provenance even
    when those rows are still mid-flight (``streaming``), so staged-effect
    and post-turn callers can run inside the outer revision transaction and
    roll back atomically with it.
    """
    cid, eid = _uuid(campaign.id, "campaign_id"), _uuid(entity_id, "entity_id")
    turn_id = _uuid(source_turn_id, "source_turn_id") if source_turn_id else None
    attempt_id = _uuid(source_attempt_id, "source_attempt_id") if source_attempt_id else None
    event_id = _uuid(source_event_id, "source_event_id") if source_event_id else None
    prov = _provenance(provenance, source_turn_id=turn_id, source_attempt_id=attempt_id, source_event_id=event_id)
    turn_id, attempt_id, event_id = _resolve_npc_source_refs(
        db, cid, turn_id=turn_id, attempt_id=attempt_id, event_id=event_id,
    )
    entity = get_entity_strict(db, cid, eid)
    if entity.entity_type != "npc":
        raise ValueError("NPC state may only be attached to an npc world entity")
    if location_entity_id is not UNSET and location_entity_id is not None:
        location_entity_id = _uuid(location_entity_id, "location_entity_id")
        location = get_entity_strict(db, cid, location_entity_id)
        if location.entity_type not in {"location", "landmark"}:
            raise ValueError("NPC location must reference a location or landmark entity")
    if importance is not UNSET:
        importance = str(importance).strip().lower()
        if importance not in IMPORTANCE:
            raise ValueError(f"importance must be one of {list(IMPORTANCE)}")
    if depth is not UNSET:
        depth = int(depth)
        if depth < 0:
            raise ValueError("depth must be non-negative")
    for value, label, kind in ((goals, "goals", list), (resources, "resources", list), (disposition, "disposition", dict)):
        if value is not UNSET and not isinstance(value, kind):
            raise ValueError(f"{label} must be a {kind.__name__}")

    row = db.get(NPCState, eid)
    if row is not None and row.campaign_id != cid:
        raise ValueError("NPC state belongs to another campaign")
    if row is None:
        row = NPCState(
            entity_id=eid, campaign_id=cid, campaign_revision=new_revision,
            provenance=prov, source_turn_id=turn_id, source_attempt_id=attempt_id,
            source_event_id=event_id, operation_id=(str(operation_id)[:128] if operation_id else None),
        )
        db.add(row)
    else:
        row.state_revision += 1
        row.campaign_revision = new_revision
        row.provenance = prov
        row.source_turn_id, row.source_attempt_id, row.source_event_id = turn_id, attempt_id, event_id
        row.operation_id = str(operation_id)[:128] if operation_id else None
    if importance is not UNSET:
        if IMPORTANCE.index(importance) < IMPORTANCE.index(row.importance or "incidental"):
            raise ValueError("importance cannot decrease during progressive enrichment")
        row.importance = importance
    if depth is not UNSET:
        if depth < int(row.depth or 0):
            raise ValueError("depth cannot decrease during progressive enrichment")
        row.depth = depth
    for field, value in {
        "role": role, "goals": goals, "disposition": disposition,
        "resources": resources, "current_activity": current_activity,
        "location_entity_id": location_entity_id, "location_name": location_name,
    }.items():
        if value is not UNSET:
            setattr(row, field, value)
    row.field_visibility = _visibility_map(field_visibility, row.field_visibility)
    db.flush()
    return row


