"""Progressive, durable NPC state and bounded projections — issue #215.

Deterministic domain code owns state, visibility, revisions, and legal action
candidates.  This module never advances state from wall-clock time and never
asks a model to invent candidates or apply side effects.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.events import commit_campaign_mutation
from app.observability.tracing import structured_log
from app.world.epistemics import may_user_receive, what_does_subject_know
from app.world.service import get_entity_strict
from models.campaigns import Campaign
from models.world import NPCState, WorldRelation

logger = logging.getLogger(__name__)
UNSET: Any = object()

IMPORTANCE = ("incidental", "supporting", "major")
STATE_FIELDS = frozenset({
    "role", "goals", "disposition", "resources", "current_activity",
    "location_entity_id", "location_name", "importance", "depth",
})
DEFAULT_FIELD_VISIBILITY = {
    "role": "campaign", "location_entity_id": "campaign", "location_name": "campaign",
    "importance": "dm_only", "depth": "dm_only", "goals": "dm_only",
    "disposition": "dm_only", "resources": "dm_only", "current_activity": "dm_only",
}
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


def _require_committed_source(
    db: Session, campaign_id: uuid.UUID, *, turn_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None, event_id: uuid.UUID | None,
) -> None:
    """Fail closed unless at least one cited source is durably committed.

    The DM state machine's terminal success state for both turns and
    attempts is ``"succeeded"`` (see ``app.dm.turns``); anything earlier
    (streaming/prepared/failed) cannot back a standalone write.
    """
    from models.campaigns import CampaignDomainEvent
    from models.dm import DmTurn, DmTurnAttempt

    if event_id:
        event = db.get(CampaignDomainEvent, event_id)
        if event is not None and event.campaign_id == campaign_id:
            return
    if turn_id:
        turn = db.get(DmTurn, turn_id)
        if turn is not None and turn.campaign_id == campaign_id and turn.status == "succeeded":
            if attempt_id is not None:
                attempt = db.get(DmTurnAttempt, attempt_id)
                if attempt is None or str(attempt.turn_id) != str(turn_id):
                    raise ValueError("NPC state source_attempt does not belong to source_turn")
            return
    if attempt_id:
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is not None and attempt.campaign_id == campaign_id and attempt.status == "succeeded":
            turn = db.get(DmTurn, attempt.turn_id)
            if turn is not None and turn.campaign_id == campaign_id and turn.status == "succeeded":
                return
    raise ValueError("NPC state source must reference committed gameplay/post-turn history")


def _resolve_npc_source_refs(
    db: Session, campaign_id: uuid.UUID, *, turn_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None, event_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    """Resolve source refs by existence/campaign/link — no status gate.

    The caller owns the surrounding transaction (turn-commit mutate,
    post-turn range), so cited rows may still be mid-flight — e.g. a
    ``streaming`` turn whose commit owns this write. Status gating lives
    in :func:`_require_committed_source` for standalone writes only.
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


def apply_npc_state_inline(
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
    roll back atomically with it. All domain validation lives here — the
    authoritative wrapper only adds the committed-source gate plus the
    revision bump and domain event.
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


def update_npc_state_authoritative(
    db: Session, campaign_id: Any, entity_id: Any, expected_revision: int, *,
    role: Any = UNSET, goals: Any = UNSET, disposition: Any = UNSET,
    resources: Any = UNSET, current_activity: Any = UNSET,
    location_entity_id: Any = UNSET, location_name: Any = UNSET,
    importance: Any = UNSET, depth: Any = UNSET, field_visibility: Any = UNSET,
    provenance: dict | None = None, source_turn_id: Any = None,
    source_attempt_id: Any = None, source_event_id: Any = None,
    operation_id: str | None = None,
) -> tuple[NPCState, Any]:
    """Create or progressively enrich state under campaign revision ordering.

    Omitted fields are preserved; explicit ``None`` clears nullable fields.
    Importance and depth are monotonic so an enrichment cannot accidentally
    erase a dossier. Corrections use a later repair update with new provenance.
    """
    cid = _uuid(campaign_id, "campaign_id")
    turn_id = _uuid(source_turn_id, "source_turn_id") if source_turn_id else None
    attempt_id = _uuid(source_attempt_id, "source_attempt_id") if source_attempt_id else None
    event_id = _uuid(source_event_id, "source_event_id") if source_event_id else None
    _provenance(provenance, source_turn_id=turn_id, source_attempt_id=attempt_id, source_event_id=event_id)
    _require_committed_source(db, cid, turn_id=turn_id, attempt_id=attempt_id, event_id=event_id)
    eid = _uuid(entity_id, "entity_id")
    prov = dict(provenance)

    holder: dict[str, Any] = {}
    started = time.monotonic()

    def mutate(campaign: Campaign) -> None:
        # new_revision is prior+1 — mirrors commit_campaign_mutation's bump.
        prior = int(campaign.revision) if campaign.revision is not None else 0
        holder["row"] = apply_npc_state_inline(
            db, campaign, entity_id, new_revision=prior + 1,
            role=role, goals=goals, disposition=disposition,
            resources=resources, current_activity=current_activity,
            location_entity_id=location_entity_id, location_name=location_name,
            importance=importance, depth=depth, field_visibility=field_visibility,
            provenance=provenance, source_turn_id=turn_id,
            source_attempt_id=attempt_id, source_event_id=event_id,
            operation_id=operation_id,
        )

    _campaign, event = commit_campaign_mutation(
        db, cid, expected_revision, event_type="world.npc_state_updated",
        operation_id=operation_id, mutate=mutate,
        payload_builder=lambda: {
            "entity_id": str(eid), "state_revision": holder["row"].state_revision,
            "importance": holder["row"].importance, "depth": holder["row"].depth,
        },
        targets_builder=lambda: {"entity_id": str(eid)},
        # The event carries dossier depth/importance metadata; keep it in the
        # authority lane even when the NPC's baseline identity is campaign-visible.
        visibility="dm_only", provenance=prov,
    )
    row = holder["row"]
    structured_log(
        logger, logging.INFO, "npc_state_updated", campaign_id=str(cid), entity_id=str(eid),
        state_revision=row.state_revision, depth=row.depth, importance=row.importance,
        update_source=prov.get("source"), update_ms=round((time.monotonic() - started) * 1000, 3),
    )
    return row, event


def _field_allowed(row: NPCState, field: str, authority: bool) -> bool:
    if authority:
        return True
    visibility = (row.field_visibility or {}).get(field, DEFAULT_FIELD_VISIBILITY[field])
    return visibility in {"public", "campaign"}


def project_npc_state(
    db: Session, campaign: Campaign, entity_id: Any, viewer_user_id: Any, *,
    fields: Iterable[str] | None = None, include_relationships: bool = False,
    include_knowledge: bool = False,
) -> dict[str, Any]:
    """Return only explicitly requested, viewer-authorized NPC state fields."""
    eid, viewer = _uuid(entity_id, "entity_id"), _uuid(viewer_user_id, "viewer_user_id")
    entity = get_entity_strict(db, campaign.id, eid)
    if entity.entity_type != "npc":
        raise ValueError("entity is not an NPC")
    if not may_user_receive(db, campaign, "entity", eid, viewer)["allowed"]:
        return {"entity_id": str(eid), "visible": False, "fields": {}}
    requested = set(fields or {"role", "location_entity_id", "location_name", "importance", "depth"})
    unknown = requested - STATE_FIELDS
    if unknown:
        raise ValueError(f"unknown NPC projection fields: {sorted(unknown)}")
    row = get_npc_state(db, campaign.id, eid)
    authority = campaign.owner_id == viewer
    projected: dict[str, Any] = {}
    if row is not None:
        raw = row.to_dict()
        projected = {field: raw[field] for field in requested if _field_allowed(row, field, authority)}
    result: dict[str, Any] = {
        "entity_id": str(eid), "name": entity.name, "visible": True,
        "fields": projected,
        # Revision count itself reveals that hidden enrichment occurred.
        "state_revision": row.state_revision if row and authority else None,
    }
    if include_relationships:
        relations = db.execute(select(WorldRelation).where(
            WorldRelation.campaign_id == campaign.id,
            WorldRelation.subject_entity_id == eid,
            WorldRelation.status == "active",
        )).scalars().all()
        result["relationships"] = [
            rel.to_dict() for rel in relations
            if may_user_receive(db, campaign, "relation", rel.id, viewer)["allowed"]
        ]
    if include_knowledge:
        result["knowledge"] = what_does_subject_know(db, campaign, eid, viewer)
    structured_log(
        logger, logging.INFO, "npc_state_projected", campaign_id=str(campaign.id), entity_id=str(eid),
        requested_field_count=len(requested), returned_field_count=len(projected),
        context_payload_size=len(str(result)), depth=(row.depth if row else 0),
    )
    return result


def build_npc_decision_context(
    db: Session, campaign: Campaign, entity_id: Any, viewer_user_id: Any, *,
    candidates: Iterable[dict], fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a bounded context from domain-enumerated legal candidates.

    Candidate generation remains in the owning mechanics/domain service. This
    function only validates and packages those candidates for semantic choice.
    """
    legal: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each NPC candidate must be an object")
        cid = str(candidate.get("id") or "").strip()
        if not cid or cid in seen:
            raise ValueError("NPC candidates require unique non-empty ids")
        if not str(candidate.get("label") or "").strip():
            raise ValueError("NPC candidates require labels")
        seen.add(cid)
        legal.append(dict(candidate))
    context = project_npc_state(db, campaign, entity_id, viewer_user_id, fields=fields)
    context["legal_candidates"] = legal
    context["candidate_count"] = len(legal)
    return context


def require_legal_npc_choice(context: dict[str, Any], candidate_id: Any) -> dict:
    """Resolve a model's selection only within the supplied legal set."""
    wanted = str(candidate_id or "").strip()
    for candidate in context.get("legal_candidates") or []:
        if str(candidate.get("id")) == wanted:
            return candidate
    raise ValueError("NPC decision did not select a supplied legal candidate")
