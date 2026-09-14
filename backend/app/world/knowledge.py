"""Durable relations + epistemic facts — issue #210.

Two versioned record kinds with explicit truth/epistemic state, visibility,
provenance, and lifecycle:

- WorldRelation: subject —relation_type→ object (entity or literal label).
- WorldFact: free-form proposition with referenced canonical entities.

Contract shared by both:

- Only ``status == "active"`` rows are current truth (indexed; no replay).
- Changes create a new version row; the prior active row flips to
  ``superseded`` in the same transaction — history is preserved, never
  destructively overwritten.
- A bare claim (player/NPC utterance) stores as ``claimed``/``suspected``/
  etc. and never becomes ``confirmed`` truth unless superseded explicitly.
- Duplicate retries keyed by idempotency_key return the existing row with
  no new version and (authoritative path) no revision bump.
- Failed updates raise before mutating the prior row's lifecycle, so the
  prior active truth survives intact (the outer revision transaction rolls
  back on any error).
- Unknown/conflicting entity or source-event references fail closed
  (ValueError) for later adjudication/repair.
- Visibility is mandatory and fail-closed (default ``dm_only``); restricted
  records are filtered for ordinary viewers, mirroring #209.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.world.service import (
    RESTRICTED_VISIBILITIES,
    UNSET,
    is_world_authority,
    normalize_visibility,
    world_event_visibility,
)
from models.campaigns import Campaign, CampaignDomainEvent
from models.world import (
    EPISTEMIC_STATES,
    RECORD_STATUSES,
    WorldEntity,
    WorldFact,
    WorldFactEntityRef,
    WorldRelation,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EPISTEMIC_STATES",
    "RECORD_STATUSES",
    "validate_relation_type",
    "validate_epistemic_state",
    "validate_record_status",
    "validate_fact_content",
    "relation_visible_to_viewer",
    "fact_visible_to_viewer",
    "filter_relations_for_viewer",
    "filter_facts_for_viewer",
    "get_relation_strict",
    "get_fact_strict",
    "list_relations",
    "list_facts",
    "list_relations_for_entity",
    "list_records_for_source_turn",
    "list_records_for_source_event",
    "create_relation_inline",
    "supersede_relation_inline",
    "create_fact_inline",
    "supersede_fact_inline",
    "create_relation_authoritative",
    "supersede_relation_authoritative",
    "create_fact_authoritative",
    "supersede_fact_authoritative",
    "count_active_relations",
    "count_active_facts",
]

_RELATION_TYPE_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_MAX_FACT_ENTITY_REFS = 24

# Re-exported for router convenience (single import surface).
_IsAuthority = is_world_authority


# ── Validation ──────────────────────────────────────────────────────────────

def validate_relation_type(value: Any) -> str:
    t = str(value or "").strip().lower()
    if not _RELATION_TYPE_RE.fullmatch(t or ""):
        raise ValueError("relation_type must be 2-64 chars of [a-z0-9_]")
    return t


def validate_epistemic_state(value: Any) -> str:
    s = str(value or "claimed").strip().lower() or "claimed"
    if s not in EPISTEMIC_STATES:
        raise ValueError(f"epistemic_state must be one of {sorted(EPISTEMIC_STATES)}")
    return s


def validate_record_status(value: Any) -> str:
    s = str(value or "active").strip().lower() or "active"
    if s not in RECORD_STATUSES:
        raise ValueError(f"status must be one of {sorted(RECORD_STATUSES)}")
    return s


def validate_fact_content(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("fact content is required")
    if len(text) > 2000:
        raise ValueError("fact content must be 2000 characters or fewer")
    return text


def _coerce_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field} {value!r}") from exc


def _coerce_optional_uuid(value: Any, *, field: str) -> uuid.UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _coerce_uuid(value, field=field)


def _normalize_idempotency_key(value: Any) -> str | None:
    key = str(value or "").strip() or None
    if key and len(key) > 128:
        raise ValueError("idempotency_key must be 128 characters or fewer")
    return key


def _normalize_grants(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("grants must be an object")
    return dict(value)


def _normalize_provenance(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("provenance must be an object")
    return dict(value)


# ── Reference resolution (fail closed) ──────────────────────────────────────

def _resolve_world_entity(db: Session, campaign_id: uuid.UUID, entity_id: Any, *, role: str) -> WorldEntity:
    eid = _coerce_uuid(entity_id, field=f"{role}_entity_id")
    entity = db.get(WorldEntity, eid)
    if entity is None or entity.campaign_id != campaign_id:
        structured_log(
            logger, logging.WARNING, "world_provenance_resolution_failed",
            campaign_id=str(campaign_id), role=role, entity_id=str(eid),
            reason="entity_not_in_campaign",
        )
        raise ValueError(f"{role} entity {eid} not found in campaign {campaign_id}")
    return entity


def _resolve_fact_entity_refs(
    db: Session, campaign_id: uuid.UUID, entity_refs: Any
) -> list[uuid.UUID]:
    if entity_refs is None:
        return []
    if not isinstance(entity_refs, (list, tuple)):
        raise ValueError("entity_refs must be a list")
    if len(entity_refs) > _MAX_FACT_ENTITY_REFS:
        raise ValueError(f"entity_refs must have at most {_MAX_FACT_ENTITY_REFS} entries")
    resolved: list[uuid.UUID] = []
    for raw in entity_refs:
        entity = _resolve_world_entity(db, campaign_id, raw, role="fact_reference")
        if entity.id not in resolved:
            resolved.append(entity.id)
    return resolved


def _resolve_source_event(
    db: Session, campaign_id: uuid.UUID, source_event_id: Any
) -> uuid.UUID | None:
    eid = _coerce_optional_uuid(source_event_id, field="source_event_id")
    if eid is None:
        return None
    event = db.get(CampaignDomainEvent, eid)
    if event is None or event.campaign_id != campaign_id:
        structured_log(
            logger, logging.WARNING, "world_provenance_resolution_failed",
            campaign_id=str(campaign_id), source_event_id=str(eid),
            reason="event_not_in_campaign",
        )
        raise ValueError(f"source_event {eid} not found in campaign {campaign_id}")
    return eid


def _resolve_source_turn_refs(
    source_turn_id: Any, source_attempt_id: Any
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    # Turn/attempt rows live in the DM domain; the knowledge layer records the
    # linkage as UUIDs without cross-importing that domain. Malformed UUIDs
    # still fail closed here.
    return (
        _coerce_optional_uuid(source_turn_id, field="source_turn_id"),
        _coerce_optional_uuid(source_attempt_id, field="source_attempt_id"),
    )


# ── Viewer-aware reads ──────────────────────────────────────────────────────

def relation_visible_to_viewer(relation: WorldRelation, is_authority: bool) -> bool:
    return bool(is_authority) or relation.visibility not in RESTRICTED_VISIBILITIES


def fact_visible_to_viewer(fact: WorldFact, is_authority: bool) -> bool:
    return bool(is_authority) or fact.visibility not in RESTRICTED_VISIBILITIES


def filter_relations_for_viewer(
    relations: list[WorldRelation], is_authority: bool
) -> list[WorldRelation]:
    if is_authority:
        return list(relations)
    return [r for r in relations if r.visibility not in RESTRICTED_VISIBILITIES]


def filter_facts_for_viewer(
    facts: list[WorldFact], is_authority: bool
) -> list[WorldFact]:
    if is_authority:
        return list(facts)
    return [f for f in facts if f.visibility not in RESTRICTED_VISIBILITIES]


# ── Typed reads (active truth by default; no history replay) ────────────────

def get_relation_strict(db: Session, campaign_id: uuid.UUID, relation_id: uuid.UUID) -> WorldRelation:
    relation = db.get(WorldRelation, _coerce_uuid(relation_id, field="relation_id"))
    if relation is None or relation.campaign_id != campaign_id:
        raise ValueError(f"World relation {relation_id} not found in campaign {campaign_id}")
    return relation


def get_fact_strict(db: Session, campaign_id: uuid.UUID, fact_id: uuid.UUID) -> WorldFact:
    fact = db.get(WorldFact, _coerce_uuid(fact_id, field="fact_id"))
    if fact is None or fact.campaign_id != campaign_id:
        raise ValueError(f"World fact {fact_id} not found in campaign {campaign_id}")
    return fact


def list_relations(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    subject_entity_id: Any | None = None,
    object_entity_id: Any | None = None,
    entity_id: Any | None = None,
    relation_type: str | None = None,
    epistemic_state: str | None = None,
    status: str | None = None,
    include_history: bool = False,
    limit: int = 100,
) -> list[WorldRelation]:
    q = select(WorldRelation).where(WorldRelation.campaign_id == campaign_id)
    if status is not None:
        q = q.where(WorldRelation.status == validate_record_status(status))
    elif not include_history:
        q = q.where(WorldRelation.status == "active")
    if subject_entity_id is not None:
        q = q.where(WorldRelation.subject_entity_id == _coerce_uuid(subject_entity_id, field="subject_entity_id"))
    if object_entity_id is not None:
        q = q.where(WorldRelation.object_entity_id == _coerce_uuid(object_entity_id, field="object_entity_id"))
    if entity_id is not None:
        eid = _coerce_uuid(entity_id, field="entity_id")
        q = q.where(
            (WorldRelation.subject_entity_id == eid) | (WorldRelation.object_entity_id == eid)
        )
    if relation_type:
        q = q.where(WorldRelation.relation_type == validate_relation_type(relation_type))
    if epistemic_state:
        q = q.where(WorldRelation.epistemic_state == validate_epistemic_state(epistemic_state))
    q = q.order_by(WorldRelation.created_at.asc()).limit(max(1, min(int(limit or 100), 200)))
    return list(db.execute(q).scalars().all())


def list_relations_for_entity(
    db: Session, campaign_id: uuid.UUID, entity_id: Any, *, include_history: bool = False, limit: int = 100
) -> list[WorldRelation]:
    return list_relations(db, campaign_id, entity_id=entity_id, include_history=include_history, limit=limit)


def list_facts(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    entity_id: Any | None = None,
    epistemic_state: str | None = None,
    status: str | None = None,
    include_history: bool = False,
    limit: int = 100,
) -> list[WorldFact]:
    q = select(WorldFact).where(WorldFact.campaign_id == campaign_id)
    if status is not None:
        q = q.where(WorldFact.status == validate_record_status(status))
    elif not include_history:
        q = q.where(WorldFact.status == "active")
    if epistemic_state:
        q = q.where(WorldFact.epistemic_state == validate_epistemic_state(epistemic_state))
    if entity_id is not None:
        eid = _coerce_uuid(entity_id, field="entity_id")
        q = q.join(
            WorldFactEntityRef,
            (WorldFactEntityRef.fact_id == WorldFact.id)
            & (WorldFactEntityRef.entity_id == eid),
        )
    q = q.order_by(WorldFact.created_at.asc()).limit(max(1, min(int(limit or 100), 200)))
    return list(db.execute(q).scalars().all())


def list_records_for_source_turn(
    db: Session, campaign_id: uuid.UUID, source_turn_id: Any
) -> dict[str, list]:
    tid = _coerce_uuid(source_turn_id, field="source_turn_id")
    relations = list(
        db.execute(
            select(WorldRelation).where(
                WorldRelation.campaign_id == campaign_id, WorldRelation.source_turn_id == tid
            ).order_by(WorldRelation.created_at.asc())
        ).scalars().all()
    )
    facts = list(
        db.execute(
            select(WorldFact).where(
                WorldFact.campaign_id == campaign_id, WorldFact.source_turn_id == tid
            ).order_by(WorldFact.created_at.asc())
        ).scalars().all()
    )
    return {"relations": relations, "facts": facts}


def list_records_for_source_event(
    db: Session, campaign_id: uuid.UUID, source_event_id: Any
) -> dict[str, list]:
    eid = _coerce_uuid(source_event_id, field="source_event_id")
    relations = list(
        db.execute(
            select(WorldRelation).where(
                WorldRelation.campaign_id == campaign_id, WorldRelation.source_event_id == eid
            ).order_by(WorldRelation.created_at.asc())
        ).scalars().all()
    )
    facts = list(
        db.execute(
            select(WorldFact).where(
                WorldFact.campaign_id == campaign_id, WorldFact.source_event_id == eid
            ).order_by(WorldFact.created_at.asc())
        ).scalars().all()
    )
    return {"relations": relations, "facts": facts}


def count_active_relations(db: Session, campaign_id: uuid.UUID) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WorldRelation).where(
            WorldRelation.campaign_id == campaign_id, WorldRelation.status == "active"
        )
    ) or 0)


def count_active_facts(db: Session, campaign_id: uuid.UUID) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WorldFact).where(
            WorldFact.campaign_id == campaign_id, WorldFact.status == "active"
        )
    ) or 0)


# ── Internal writers (no revision bump; caller owns the transaction) ────────

def _find_relation_by_idempotency(
    db: Session, campaign_id: uuid.UUID, idempotency_key: str | None
) -> WorldRelation | None:
    if not idempotency_key or not str(idempotency_key).strip():
        return None
    return db.execute(
        select(WorldRelation).where(
            WorldRelation.campaign_id == campaign_id,
            WorldRelation.idempotency_key == str(idempotency_key).strip(),
        )
    ).scalars().first()


def _find_fact_by_idempotency(
    db: Session, campaign_id: uuid.UUID, idempotency_key: str | None
) -> WorldFact | None:
    if not idempotency_key or not str(idempotency_key).strip():
        return None
    return db.execute(
        select(WorldFact).where(
            WorldFact.campaign_id == campaign_id,
            WorldFact.idempotency_key == str(idempotency_key).strip(),
        )
    ).scalars().first()


def _dialect_upsert_insert(db: Session):
    try:
        dialect_name = db.get_bind().dialect.name
    except Exception:
        return None
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        return sqlite_insert
    return None


def _insert_relation_row(
    db: Session,
    campaign: Campaign,
    *,
    subject_id: uuid.UUID,
    relation_type: str,
    object_id: uuid.UUID | None,
    object_label: str | None,
    epistemic_state: str,
    status: str,
    version: int,
    supersedes_id: uuid.UUID | None,
    visibility: str,
    grants: dict,
    provenance: dict,
    details: dict,
    source_turn_id: uuid.UUID | None,
    source_attempt_id: uuid.UUID | None,
    source_event_id: uuid.UUID | None,
    operation_id: str | None,
    idempotency_key: str | None,
) -> tuple[WorldRelation, bool]:
    """Idempotent row insert shared by create + supersede.

    Returns (row, created). A concurrent duplicate-key winner is returned
    with created=False instead of raising, so the outer revision transaction
    stays recoverable on every backend.
    """
    if idempotency_key:
        existing = _find_relation_by_idempotency(db, campaign.id, idempotency_key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign.id), relation_id=str(existing.id),
                idempotency_key=idempotency_key,
            )
            return existing, False

        upsert_insert = _dialect_upsert_insert(db)
        if upsert_insert is not None:
            row_id = uuid.uuid4()
            db.execute(
                upsert_insert(WorldRelation)
                .values(
                    id=row_id,
                    campaign_id=campaign.id,
                    subject_entity_id=subject_id,
                    relation_type=relation_type,
                    object_entity_id=object_id,
                    object_label=object_label,
                    epistemic_state=epistemic_state,
                    status=status,
                    version=version,
                    supersedes_id=supersedes_id,
                    visibility=visibility,
                    grants=grants,
                    provenance=provenance,
                    details=details,
                    source_turn_id=source_turn_id,
                    source_attempt_id=source_attempt_id,
                    source_event_id=source_event_id,
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=["campaign_id", "idempotency_key"])
            )
            stored = _find_relation_by_idempotency(db, campaign.id, idempotency_key)
            if stored is None:  # pragma: no cover — defensive
                raise RuntimeError(f"idempotent relation insert for key {idempotency_key!r} left no row")
            if str(stored.id) == str(row_id):
                return stored, True
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign.id), relation_id=str(stored.id),
                idempotency_key=idempotency_key, path="race_winner",
            )
            return stored, False

    row = WorldRelation(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        subject_entity_id=subject_id,
        relation_type=relation_type,
        object_entity_id=object_id,
        object_label=object_label,
        epistemic_state=epistemic_state,
        status=status,
        version=version,
        supersedes_id=supersedes_id,
        visibility=visibility,
        grants=grants,
        provenance=provenance,
        details=details,
        source_turn_id=source_turn_id,
        source_attempt_id=source_attempt_id,
        source_event_id=source_event_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    if idempotency_key is None:
        db.add(row)
        db.flush()
        return row, True
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        try:
            db.expunge(row)
        except Exception:
            pass
        winner = _find_relation_by_idempotency(db, campaign.id, idempotency_key)
        if winner is not None:
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign.id), relation_id=str(winner.id),
                idempotency_key=idempotency_key, path="race_winner",
            )
            return winner, False
        raise
    return row, True


def _sync_fact_refs(
    db: Session, campaign: Campaign, fact: WorldFact, entity_ids: list[uuid.UUID]
) -> None:
    db.execute(delete(WorldFactEntityRef).where(WorldFactEntityRef.fact_id == fact.id))
    for eid in entity_ids:
        db.add(WorldFactEntityRef(fact_id=fact.id, campaign_id=campaign.id, entity_id=eid))
    fact.entity_refs = [str(e) for e in entity_ids]
    db.flush()


def create_relation_inline(
    db: Session,
    campaign: Campaign,
    *,
    subject_entity_id: Any,
    relation_type: str,
    object_entity_id: Any | None = None,
    object_label: str | None = None,
    epistemic_state: str = "claimed",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldRelation, bool]:
    """Insert one relation version inside the caller's transaction.

    A bare claim stores as non-``confirmed`` epistemic state by default, so a
    player/NPC utterance never becomes objective truth implicitly. Flushes;
    never commits.
    """
    rtype = validate_relation_type(relation_type)
    epistemic = validate_epistemic_state(epistemic_state)
    # Fail-closed visibility: unmarked assertions stay restricted.
    vis = normalize_visibility(visibility or "dm_only")
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    # operation_id-scoped keys keep staged-effect retries aligned with the
    # entity path, but an explicit idempotency_key always wins.
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)

    subject = _resolve_world_entity(db, campaign.id, subject_entity_id, role="subject")
    obj: WorldEntity | None = None
    if object_entity_id is not None:
        obj = _resolve_world_entity(db, campaign.id, object_entity_id, role="object")
    label = str(object_label).strip()[:256] if object_label and str(object_label).strip() else None
    if obj is None and not label:
        raise ValueError("relation requires object_entity_id or object_label")
    source_event = _resolve_source_event(db, campaign.id, source_event_id)
    turn_id, attempt_id = _resolve_source_turn_refs(source_turn_id, source_attempt_id)

    row, created = _insert_relation_row(
        db, campaign,
        subject_id=subject.id, relation_type=rtype,
        object_id=obj.id if obj else None, object_label=label,
        epistemic_state=epistemic, status="active", version=1, supersedes_id=None,
        visibility=vis, grants=_normalize_grants(grants),
        provenance=_normalize_provenance(provenance),
        details=dict(details or {}),
        source_turn_id=turn_id, source_attempt_id=attempt_id, source_event_id=source_event,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    if created:
        structured_log(
            logger, logging.INFO, "world_relation_created",
            campaign_id=str(campaign.id), relation_id=str(row.id),
            relation_type=rtype, epistemic_state=epistemic, visibility=vis,
            source_turn_id=str(turn_id) if turn_id else None,
            source_attempt_id=str(attempt_id) if attempt_id else None,
            source_event_id=str(source_event) if source_event else None,
            operation_id=str(operation_id) if operation_id else None,
        )
    return row, created


def supersede_relation_inline(
    db: Session,
    campaign: Campaign,
    prior_relation_id: Any,
    *,
    subject_entity_id: Any | None = None,
    relation_type: str | None = None,
    object_entity_id: Any | None = UNSET,
    object_label: str | None = UNSET,
    epistemic_state: str | None = None,
    new_status: str = "active",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    clear_object: bool = False,
) -> tuple[WorldRelation, bool]:
    """Supersede one active relation with a new version, preserving history.

    All reference validation happens BEFORE the prior row is touched, so a
    failed update never partially supersedes the prior active truth. The
    insert + prior flip share the caller's transaction (atomic with the
    source turn / revision commit).
    """
    prior = get_relation_strict(db, campaign.id, _coerce_uuid(prior_relation_id, field="prior_relation_id"))
    if prior.status != "active":
        raise ValueError(f"relation {prior.id} is {prior.status}, only active relations can be superseded")
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)
    if key:
        dup = _find_relation_by_idempotency(db, campaign.id, key)
        if dup is not None:
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign.id), relation_id=str(dup.id),
                idempotency_key=key, path="supersede_precheck",
            )
            return dup, False

    # ── Validate everything before touching prior lifecycle ──────────────
    rtype = validate_relation_type(relation_type) if relation_type is not None else prior.relation_type
    epistemic = validate_epistemic_state(epistemic_state) if epistemic_state is not None else prior.epistemic_state
    status = validate_record_status(new_status)
    vis = normalize_visibility(visibility) if visibility is not None else prior.visibility
    subject = (
        _resolve_world_entity(db, campaign.id, subject_entity_id, role="subject")
        if subject_entity_id is not None else db.get(WorldEntity, prior.subject_entity_id)
    )
    if subject is None or subject.campaign_id != campaign.id:  # pragma: no cover — defensive
        raise ValueError(f"subject entity {prior.subject_entity_id} not found in campaign {campaign.id}")
    # Key-presence semantics: UNSET (omitted) inherits the prior reference,
    # explicit None clears it, a value re-points it.
    obj: WorldEntity | None = None
    if clear_object:
        obj = None
    elif object_entity_id is UNSET:
        if prior.object_entity_id is not None:
            obj = db.get(WorldEntity, prior.object_entity_id)
            if obj is not None and obj.campaign_id != campaign.id:
                raise ValueError(f"object entity {prior.object_entity_id} not found in campaign {campaign.id}")
    elif object_entity_id is not None:
        obj = _resolve_world_entity(db, campaign.id, object_entity_id, role="object")
    # else explicit None → cleared (obj stays None)
    if object_label is UNSET:
        label: str | None = None if clear_object else prior.object_label
    elif object_label is not None:
        label = str(object_label).strip()[:256] or None
    else:
        label = None
    if obj is None and not label:
        raise ValueError("relation requires object_entity_id or object_label")
    source_event = _resolve_source_event(db, campaign.id, source_event_id)
    turn_id, attempt_id = _resolve_source_turn_refs(source_turn_id, source_attempt_id)
    merged_provenance = {**(prior.provenance or {}), **_normalize_provenance(provenance)}
    merged_details = dict(details) if details is not None else dict(prior.details or {})
    merged_grants = _normalize_grants(grants) if grants is not None else dict(prior.grants or {})

    row, created = _insert_relation_row(
        db, campaign,
        subject_id=subject.id, relation_type=rtype,
        object_id=obj.id if obj else None, object_label=label,
        epistemic_state=epistemic, status=status, version=int(prior.version or 1) + 1,
        supersedes_id=prior.id,
        visibility=vis, grants=merged_grants,
        provenance=merged_provenance, details=merged_details,
        source_turn_id=turn_id or prior.source_turn_id,
        source_attempt_id=attempt_id or prior.source_attempt_id,
        source_event_id=source_event or prior.source_event_id,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    if not created:
        return row, False
    # Race absorbed by upsert (created=False) leaves prior untouched; only the
    # genuine new version flips prior lifecycle.
    prior.status = "superseded"
    prior.superseded_by_id = row.id
    db.flush()
    structured_log(
        logger, logging.INFO, "world_relation_superseded",
        campaign_id=str(campaign.id), prior_relation_id=str(prior.id),
        relation_id=str(row.id), prior_epistemic=prior.epistemic_state,
        epistemic_state=epistemic, version=int(row.version),
        operation_id=str(operation_id) if operation_id else None,
    )
    if prior.epistemic_state != epistemic:
        structured_log(
            logger, logging.INFO, "world_relation_epistemic_transition",
            campaign_id=str(campaign.id), relation_id=str(row.id),
            prior=prior.epistemic_state, current=epistemic,
        )
    return row, True


def create_fact_inline(
    db: Session,
    campaign: Campaign,
    *,
    content: str,
    entity_refs: list | None = None,
    epistemic_state: str = "claimed",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldFact, bool]:
    """Assert one fact version inside the caller's transaction."""
    text = validate_fact_content(content)
    epistemic = validate_epistemic_state(epistemic_state)
    vis = normalize_visibility(visibility or "dm_only")
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)

    resolved_refs = _resolve_fact_entity_refs(db, campaign.id, entity_refs)
    source_event = _resolve_source_event(db, campaign.id, source_event_id)
    turn_id, attempt_id = _resolve_source_turn_refs(source_turn_id, source_attempt_id)

    if key:
        dup = _find_fact_by_idempotency(db, campaign.id, key)
        if dup is not None:
            structured_log(
                logger, logging.INFO, "world_fact_duplicate_conflict",
                campaign_id=str(campaign.id), fact_id=str(dup.id), idempotency_key=key,
            )
            return dup, False
        upsert_insert = _dialect_upsert_insert(db)
        if upsert_insert is not None:
            row_id = uuid.uuid4()
            db.execute(
                upsert_insert(WorldFact)
                .values(
                    id=row_id,
                    campaign_id=campaign.id,
                    content=text,
                    entity_refs=[str(e) for e in resolved_refs],
                    epistemic_state=epistemic,
                    status="active",
                    version=1,
                    visibility=vis,
                    grants=_normalize_grants(grants),
                    provenance=_normalize_provenance(provenance),
                    details=dict(details or {}),
                    source_turn_id=turn_id,
                    source_attempt_id=attempt_id,
                    source_event_id=source_event,
                    operation_id=(str(operation_id)[:128] if operation_id else None),
                    idempotency_key=key,
                )
                .on_conflict_do_nothing(index_elements=["campaign_id", "idempotency_key"])
            )
            stored = _find_fact_by_idempotency(db, campaign.id, key)
            if stored is None:  # pragma: no cover — defensive
                raise RuntimeError(f"idempotent fact insert for key {key!r} left no row")
            _sync_fact_refs(db, campaign, stored, resolved_refs)
            if str(stored.id) != str(row_id):
                structured_log(
                    logger, logging.INFO, "world_fact_duplicate_conflict",
                    campaign_id=str(campaign.id), fact_id=str(stored.id),
                    idempotency_key=key, path="race_winner",
                )
                return stored, False
            structured_log(
                logger, logging.INFO, "world_fact_asserted",
                campaign_id=str(campaign.id), fact_id=str(stored.id),
                epistemic_state=epistemic, visibility=vis,
                operation_id=str(operation_id) if operation_id else None,
            )
            return stored, True

    row = WorldFact(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        content=text,
        entity_refs=[str(e) for e in resolved_refs],
        epistemic_state=epistemic,
        status="active",
        version=1,
        visibility=vis,
        grants=_normalize_grants(grants),
        provenance=_normalize_provenance(provenance),
        details=dict(details or {}),
        source_turn_id=turn_id,
        source_attempt_id=attempt_id,
        source_event_id=source_event,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    if key is None:
        db.add(row)
        db.flush()
        _sync_fact_refs(db, campaign, row, resolved_refs)
    else:
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
                _sync_fact_refs(db, campaign, row, resolved_refs)
        except IntegrityError:
            try:
                db.expunge(row)
            except Exception:
                pass
            winner = _find_fact_by_idempotency(db, campaign.id, key)
            if winner is not None:
                structured_log(
                    logger, logging.INFO, "world_fact_duplicate_conflict",
                    campaign_id=str(campaign.id), fact_id=str(winner.id),
                    idempotency_key=key, path="race_winner",
                )
                return winner, False
            raise
    structured_log(
        logger, logging.INFO, "world_fact_asserted",
        campaign_id=str(campaign.id), fact_id=str(row.id),
        epistemic_state=epistemic, visibility=vis,
        operation_id=str(operation_id) if operation_id else None,
    )
    return row, True


def supersede_fact_inline(
    db: Session,
    campaign: Campaign,
    prior_fact_id: Any,
    *,
    content: str | None = None,
    entity_refs: list | None = None,
    epistemic_state: str | None = None,
    new_status: str = "active",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldFact, bool]:
    """Supersede one active fact with a new version, preserving history.

    Validation precedes any lifecycle mutation so failures leave the prior
    active truth untouched.
    """
    prior = get_fact_strict(db, campaign.id, _coerce_uuid(prior_fact_id, field="prior_fact_id"))
    if prior.status != "active":
        raise ValueError(f"fact {prior.id} is {prior.status}, only active facts can be superseded")
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)
    if key:
        dup = _find_fact_by_idempotency(db, campaign.id, key)
        if dup is not None:
            structured_log(
                logger, logging.INFO, "world_fact_duplicate_conflict",
                campaign_id=str(campaign.id), fact_id=str(dup.id),
                idempotency_key=key, path="supersede_precheck",
            )
            return dup, False

    text = validate_fact_content(content) if content is not None else prior.content
    epistemic = validate_epistemic_state(epistemic_state) if epistemic_state is not None else prior.epistemic_state
    status = validate_record_status(new_status)
    vis = normalize_visibility(visibility) if visibility is not None else prior.visibility
    if entity_refs is not None:
        resolved_refs = _resolve_fact_entity_refs(db, campaign.id, entity_refs)
    else:
        resolved_refs = _resolve_fact_entity_refs(db, campaign.id, list(prior.entity_refs or []))
    source_event = _resolve_source_event(db, campaign.id, source_event_id)
    turn_id, attempt_id = _resolve_source_turn_refs(source_turn_id, source_attempt_id)
    merged_provenance = {**(prior.provenance or {}), **_normalize_provenance(provenance)}
    merged_details = dict(details) if details is not None else dict(prior.details or {})
    merged_grants = _normalize_grants(grants) if grants is not None else dict(prior.grants or {})

    row = WorldFact(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        content=text,
        entity_refs=[str(e) for e in resolved_refs],
        epistemic_state=epistemic,
        status=status,
        version=int(prior.version or 1) + 1,
        supersedes_id=prior.id,
        visibility=vis,
        grants=merged_grants,
        provenance=merged_provenance,
        details=merged_details,
        source_turn_id=turn_id or prior.source_turn_id,
        source_attempt_id=attempt_id or prior.source_attempt_id,
        source_event_id=source_event or prior.source_event_id,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    if key is None:
        db.add(row)
        db.flush()
        _sync_fact_refs(db, campaign, row, resolved_refs)
    else:
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
                _sync_fact_refs(db, campaign, row, resolved_refs)
        except IntegrityError:
            try:
                db.expunge(row)
            except Exception:
                pass
            winner = _find_fact_by_idempotency(db, campaign.id, key)
            if winner is not None:
                structured_log(
                    logger, logging.INFO, "world_fact_duplicate_conflict",
                    campaign_id=str(campaign.id), fact_id=str(winner.id),
                    idempotency_key=key, path="race_winner",
                )
                return winner, False
            raise
    prior.status = "superseded"
    prior.superseded_by_id = row.id
    db.flush()
    structured_log(
        logger, logging.INFO, "world_fact_superseded",
        campaign_id=str(campaign.id), prior_fact_id=str(prior.id),
        fact_id=str(row.id), prior_epistemic=prior.epistemic_state,
        epistemic_state=epistemic, version=int(row.version),
        operation_id=str(operation_id) if operation_id else None,
    )
    if prior.epistemic_state != epistemic:
        structured_log(
            logger, logging.INFO, "world_fact_epistemic_transition",
            campaign_id=str(campaign.id), fact_id=str(row.id),
            prior=prior.epistemic_state, current=epistemic,
        )
    return row, True


# ── Authoritative writers (bump campaign revision + emit domain event) ───────

def _relation_event_payload(row_id: uuid.UUID, relation: WorldRelation | None) -> dict:
    return {
        "relation_id": str(row_id),
        "subject_entity_id": str(relation.subject_entity_id) if relation else None,
        "relation_type": relation.relation_type if relation else None,
        "epistemic_state": relation.epistemic_state if relation else None,
    }


def create_relation_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    *,
    subject_entity_id: Any,
    relation_type: str,
    object_entity_id: Any | None = None,
    object_label: str | None = None,
    epistemic_state: str = "claimed",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldRelation, Any]:
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_relation_by_idempotency(db, campaign_id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign_id), relation_id=str(existing.id),
                idempotency_key=key, path="authoritative_precheck",
            )
            return existing, None

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        row, _ = create_relation_inline(
            db, campaign, subject_entity_id=subject_entity_id, relation_type=relation_type,
            object_entity_id=object_entity_id, object_label=object_label,
            epistemic_state=epistemic_state, visibility=visibility, grants=grants,
            provenance=provenance, details=details,
            source_turn_id=source_turn_id, source_attempt_id=source_attempt_id,
            source_event_id=source_event_id, operation_id=operation_id,
            idempotency_key=key,
        )
        holder["relation_id"] = row.id

    validated_type = validate_relation_type(relation_type)
    validated_epistemic = validate_epistemic_state(epistemic_state)

    def _payload() -> dict[str, Any]:
        return {
            "relation_id": str(holder["relation_id"]),
            "relation_type": validated_type,
            "epistemic_state": validated_epistemic,
            "idempotency_key": key,
        }

    def _targets() -> dict[str, Any]:
        return {"relation_id": str(holder["relation_id"])}

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.relation_created",
        payload_builder=_payload,
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=_targets,
        visibility=world_event_visibility(visibility or "dm_only"),
        provenance={"source": "world_api", "idempotency_key": key, **_normalize_provenance(provenance)},
        mutate=_mutate,
    )
    return db.get(WorldRelation, holder["relation_id"]), event


def supersede_relation_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    prior_relation_id: Any,
    *,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    **kwargs: Any,
) -> tuple[WorldRelation, Any]:
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_relation_by_idempotency(db, campaign_id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_relation_duplicate_conflict",
                campaign_id=str(campaign_id), relation_id=str(existing.id),
                idempotency_key=key, path="authoritative_precheck",
            )
            return existing, None

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        row, _ = supersede_relation_inline(
            db, campaign, prior_relation_id,
            operation_id=operation_id, idempotency_key=key, **kwargs,
        )
        holder["relation_id"] = row.id
        holder["visibility"] = row.visibility

    def _payload() -> dict[str, Any]:
        return {
            "relation_id": str(holder["relation_id"]),
            "supersedes_id": str(prior_relation_id),
            "idempotency_key": key,
        }

    def _targets() -> dict[str, Any]:
        return {"relation_id": str(holder["relation_id"])}

    def _visibility() -> str:
        return world_event_visibility(holder.get("visibility") or "dm_only")

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.relation_superseded",
        payload_builder=_payload,
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=_targets,
        visibility="public",
        visibility_builder=_visibility,
        provenance={"source": "world_api", "idempotency_key": key},
        mutate=_mutate,
    )
    return db.get(WorldRelation, holder["relation_id"]), event


def create_fact_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    *,
    content: str,
    entity_refs: list | None = None,
    epistemic_state: str = "claimed",
    visibility: str | None = None,
    grants: dict | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any | None = None,
    source_attempt_id: Any | None = None,
    source_event_id: Any | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldFact, Any]:
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_fact_by_idempotency(db, campaign_id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_fact_duplicate_conflict",
                campaign_id=str(campaign_id), fact_id=str(existing.id),
                idempotency_key=key, path="authoritative_precheck",
            )
            return existing, None

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        row, _ = create_fact_inline(
            db, campaign, content=content, entity_refs=entity_refs,
            epistemic_state=epistemic_state, visibility=visibility, grants=grants,
            provenance=provenance, details=details,
            source_turn_id=source_turn_id, source_attempt_id=source_attempt_id,
            source_event_id=source_event_id, operation_id=operation_id,
            idempotency_key=key,
        )
        holder["fact_id"] = row.id

    validated_epistemic = validate_epistemic_state(epistemic_state)

    def _payload() -> dict[str, Any]:
        return {
            "fact_id": str(holder["fact_id"]),
            "epistemic_state": validated_epistemic,
            "idempotency_key": key,
        }

    def _targets() -> dict[str, Any]:
        return {"fact_id": str(holder["fact_id"])}

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.fact_asserted",
        payload_builder=_payload,
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=_targets,
        visibility=world_event_visibility(visibility or "dm_only"),
        provenance={"source": "world_api", "idempotency_key": key, **_normalize_provenance(provenance)},
        mutate=_mutate,
    )
    return db.get(WorldFact, holder["fact_id"]), event


def supersede_fact_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    prior_fact_id: Any,
    *,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    **kwargs: Any,
) -> tuple[WorldFact, Any]:
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_fact_by_idempotency(db, campaign_id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_fact_duplicate_conflict",
                campaign_id=str(campaign_id), fact_id=str(existing.id),
                idempotency_key=key, path="authoritative_precheck",
            )
            return existing, None

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        row, _ = supersede_fact_inline(
            db, campaign, prior_fact_id,
            operation_id=operation_id, idempotency_key=key, **kwargs,
        )
        holder["fact_id"] = row.id
        holder["visibility"] = row.visibility

    def _payload() -> dict[str, Any]:
        return {
            "fact_id": str(holder["fact_id"]),
            "supersedes_id": str(prior_fact_id),
            "idempotency_key": key,
        }

    def _targets() -> dict[str, Any]:
        return {"fact_id": str(holder["fact_id"])}

    def _visibility() -> str:
        return world_event_visibility(holder.get("visibility") or "dm_only")

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.fact_superseded",
        payload_builder=_payload,
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=_targets,
        visibility="public",
        visibility_builder=_visibility,
        provenance={"source": "world_api", "idempotency_key": key},
        mutate=_mutate,
    )
    return db.get(WorldFact, holder["fact_id"]), event
