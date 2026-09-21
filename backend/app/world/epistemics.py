"""Per-knower epistemic knowledge + arbitrary-subset visibility — issue #211.

Three concepts stay separate:

- Objective truth: WorldFact / WorldRelation rows (epistemic_state, #210).
- Fictional knowledge: WorldKnowledge rows — what a character/NPC/party
  fictionally holds (knows/believes/suspects/claims/does_not_know). Writing
  here never mutates truth tables.
- Human disclosure: ``visibility`` (public/campaign/party/dm_only/private)
  plus WorldVisibilityGrant rows for arbitrary authorized subsets. The AI is
  the only DM; campaign-owner status grants ``dm_only`` access (DM authority)
  but never ``private`` access — private disclosure requires an explicit
  active grant naming the human user.

Reusable server-side queries (all SQL-expressible, RLS-compatible):

- ``may_user_receive(db, campaign, target_kind, target_id, user_id)``
- ``what_does_subject_know(db, campaign, subject_entity_id, viewer_user_id, ...)``
- ``who_knows_target(db, campaign, target_kind, target_id, viewer_user_id, ...)``

Fail-closed everywhere: missing/ambiguous visibility, unknown records,
non-membership, and revoked/missing grants all deny with a reason code.
Access is never inferred from a related shared record — the subject entity,
the knowledge row, and its truth target are authorized independently.

Observability: acquisition_source on every knowledge row; grant/revoke emit
domain events + structured logs; denials return reason codes; projections
return filter counts without leaking hidden content (denied rows contribute
only counts, never ids/content).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member
from app.observability.tracing import structured_log
from app.world.service import is_world_authority
from models.campaigns import Campaign
from models.profiles import Profile
from models.world import (
    ACCESS_DENIED_REASONS,
    GRANT_TARGET_KINDS,
    KNOWLEDGE_STATES,
    KNOWLEDGE_TARGET_KINDS,
    KNOWER_KINDS,
    WorldEntity,
    WorldFact,
    WorldKnowledge,
    WorldRelation,
    WorldVisibilityGrant,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KNOWLEDGE_STATES",
    "KNOWER_KINDS",
    "KNOWLEDGE_TARGET_KINDS",
    "GRANT_TARGET_KINDS",
    "ACCESS_DENIED_REASONS",
    "MEMBER_VISIBILITIES",
    "AUTHORITY_VISIBILITIES",
    "validate_knowledge_state",
    "validate_knower_kind",
    "validate_knowledge_target_kind",
    "validate_grant_target_kind",
    "validate_acquisition_source",
    "normalize_record_visibility",
    "assert_knowledge_inline",
    "assert_knowledge_authoritative",
    "get_knowledge_strict",
    "list_knowledge_for_subject",
    "list_knowledge_for_target",
    "grant_visibility_inline",
    "grant_visibility_authoritative",
    "revoke_visibility_inline",
    "revoke_visibility_authoritative",
    "list_active_grants",
    "has_active_grant",
    "may_user_receive",
    "what_does_subject_know",
    "who_knows_target",
    "project_facts_for_user",
    "project_relations_for_user",
]

# Disclosure tiers. ``party`` is an alias of campaign-wide member disclosure
# (kept canonical here so callers use one spelling downstream).
MEMBER_VISIBILITIES = frozenset({"public", "campaign", "party"})
AUTHORITY_VISIBILITIES = frozenset({"dm_only"})
PRIVATE_VISIBILITIES = frozenset({"private"})
KNOWN_VISIBILITIES = MEMBER_VISIBILITIES | AUTHORITY_VISIBILITIES | PRIVATE_VISIBILITIES

_VISIBILITY_ALIASES = {"party_known": "campaign", "dm_private": "dm_only"}


# ── Validation ──────────────────────────────────────────────────────────────

def validate_knowledge_state(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in {"does-not-know", "does not know", "doesnt_know"}:
        s = "does_not_know"
    if s not in KNOWLEDGE_STATES:
        raise ValueError(f"knowledge_state must be one of {sorted(KNOWLEDGE_STATES)}")
    return s


def validate_knower_kind(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s not in KNOWER_KINDS:
        raise ValueError(f"subject_kind must be one of {sorted(KNOWER_KINDS)}")
    return s


def validate_knowledge_target_kind(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s not in KNOWLEDGE_TARGET_KINDS:
        raise ValueError(f"target_kind must be one of {sorted(KNOWLEDGE_TARGET_KINDS)}")
    return s


def validate_grant_target_kind(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s not in GRANT_TARGET_KINDS:
        raise ValueError(f"target_kind must be one of {sorted(GRANT_TARGET_KINDS)}")
    return s


def validate_acquisition_source(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if len(s) > 64:
        raise ValueError("acquisition_source must be 64 characters or fewer")
    return s


def normalize_record_visibility(value: Any) -> str:
    """Canonical visibility for knowledge/grant targets. Fail-closed: unknown
    or missing values raise (callers deny) instead of guessing disclosure."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("visibility is required (fail closed)")
    canonical = _VISIBILITY_ALIASES.get(raw, raw)
    if canonical not in KNOWN_VISIBILITIES:
        raise ValueError(f"visibility must be one of {sorted(KNOWN_VISIBILITIES)}")
    return canonical


def _coerce_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field} {value!r}") from exc


def _normalize_idempotency_key(value: Any) -> str | None:
    key = str(value or "").strip() or None
    if key and len(key) > 128:
        raise ValueError("idempotency_key must be 128 characters or fewer")
    return key


def _normalize_mapping(value: Any, *, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


# ── Target resolution (fail closed; never infer across records) ─────────────

def _resolve_subject(db: Session, campaign_id: uuid.UUID, subject_entity_id: Any) -> WorldEntity:
    eid = _coerce_uuid(subject_entity_id, field="subject_entity_id")
    entity = db.get(WorldEntity, eid)
    if entity is None or entity.campaign_id != campaign_id:
        structured_log(
            logger, logging.WARNING, "world_knowledge_subject_unresolved",
            campaign_id=str(campaign_id), subject_entity_id=str(eid),
            reason="entity_not_in_campaign",
        )
        raise ValueError(f"subject entity {eid} not found in campaign {campaign_id}")
    return entity


def _resolve_target(
    db: Session, campaign_id: uuid.UUID, target_kind: str, *,
    target_fact_id: Any = None, target_relation_id: Any = None,
    target_entity_id: Any = None, target_id: Any = None,
) -> tuple[str, uuid.UUID]:
    """Resolve exactly one truth target in-campaign. Raises fail-closed."""
    kind = validate_knowledge_target_kind(target_kind)
    # Polymorphic shorthand: target_id + kind.
    fact_ref = target_fact_id if target_fact_id is not None else (target_id if kind == "fact" else None)
    rel_ref = target_relation_id if target_relation_id is not None else (target_id if kind == "relation" else None)
    ent_ref = target_entity_id if target_entity_id is not None else (target_id if kind == "entity" else None)
    provided = [v for v in (fact_ref, rel_ref, ent_ref) if v is not None]
    if len(provided) != 1:
        raise ValueError("exactly one target reference is required")
    if kind == "fact":
        fid = _coerce_uuid(fact_ref, field="target_fact_id")
        row = db.get(WorldFact, fid)
        if row is None or row.campaign_id != campaign_id:
            raise ValueError(f"target fact {fid} not found in campaign {campaign_id}")
        return kind, fid
    if kind == "relation":
        rid = _coerce_uuid(rel_ref, field="target_relation_id")
        row = db.get(WorldRelation, rid)
        if row is None or row.campaign_id != campaign_id:
            raise ValueError(f"target relation {rid} not found in campaign {campaign_id}")
        return kind, rid
    eid = _coerce_uuid(ent_ref, field="target_entity_id")
    row = db.get(WorldEntity, eid)
    if row is None or row.campaign_id != campaign_id:
        raise ValueError(f"target entity {eid} not found in campaign {campaign_id}")
    return kind, eid


def _load_record(db: Session, target_kind: str, target_id: uuid.UUID):
    if target_kind == "fact":
        return db.get(WorldFact, target_id)
    if target_kind == "relation":
        return db.get(WorldRelation, target_id)
    if target_kind == "entity":
        return db.get(WorldEntity, target_id)
    if target_kind == "knowledge":
        return db.get(WorldKnowledge, target_id)
    return None


def _record_visibility(record: Any) -> str | None:
    return getattr(record, "visibility", None)


# ── Knowledge writers (never touch truth tables) ────────────────────────────

def _find_knowledge_by_idempotency(
    db: Session, campaign_id: uuid.UUID, key: str | None
) -> WorldKnowledge | None:
    """Single-key idempotency precheck on the live row (same pattern as #210).

    Fresh-key re-assertion of the same (subject, target) is a legitimate
    update (new revision + event); same-key retry returns the row without
    re-mutating. Historical-key tracking lives in the central
    command-idempotency boundary when callers need it — not in a
    world-specific ledger.
    """
    if not key:
        return None
    return db.execute(
        select(WorldKnowledge).where(
            WorldKnowledge.campaign_id == campaign_id,
            WorldKnowledge.idempotency_key == key,
        )
    ).scalars().first()


def _find_current_knowledge(
    db: Session, campaign_id: uuid.UUID, subject_id: uuid.UUID,
    target_kind: str, target_id: uuid.UUID,
) -> WorldKnowledge | None:
    q = select(WorldKnowledge).where(
        WorldKnowledge.campaign_id == campaign_id,
        WorldKnowledge.subject_entity_id == subject_id,
        WorldKnowledge.target_kind == target_kind,
    )
    if target_kind == "fact":
        q = q.where(WorldKnowledge.target_fact_id == target_id)
    elif target_kind == "relation":
        q = q.where(WorldKnowledge.target_relation_id == target_id)
    else:
        q = q.where(WorldKnowledge.target_entity_id == target_id)
    return db.execute(q.order_by(WorldKnowledge.created_at.asc())).scalars().first()


def assert_knowledge_inline(
    db: Session,
    campaign: Campaign,
    *,
    subject_kind: str,
    subject_entity_id: Any,
    target_kind: str,
    target_fact_id: Any = None,
    target_relation_id: Any = None,
    target_entity_id: Any = None,
    target_id: Any = None,
    knowledge_state: str = "believes",
    acquisition_source: Any = None,
    visibility: str | None = None,
    provenance: dict | None = None,
    details: dict | None = None,
    source_turn_id: Any = None,
    source_attempt_id: Any = None,
    source_event_id: Any = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldKnowledge, bool]:
    """Assert what one subject fictionally holds toward one truth record.

    Upserts the single current row per (subject, target) inside the caller's
    transaction (flushes; never commits). All references validate BEFORE any
    write, so a failed assertion leaves both the prior knowledge row and the
    underlying objective truth untouched.
    """
    skind = validate_knower_kind(subject_kind)
    state = validate_knowledge_state(knowledge_state)
    vis = normalize_record_visibility(visibility or "dm_only")
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)

    subject = _resolve_subject(db, campaign.id, subject_entity_id)
    kind, tid = _resolve_target(
        db, campaign.id, target_kind, target_fact_id=target_fact_id,
        target_relation_id=target_relation_id, target_entity_id=target_entity_id,
        target_id=target_id,
    )
    if key:
        dup = _find_knowledge_by_idempotency(db, campaign.id, key)
        if dup is not None:
            structured_log(
                logger, logging.INFO, "world_knowledge_duplicate_conflict",
                campaign_id=str(campaign.id), knowledge_id=str(dup.id),
                idempotency_key=key,
            )
            return dup, False
    # Re-assertion path: validate the effective row first, then mutate — the
    # truth tables are never written here by construction. All source
    # references validate BEFORE any write so a failed assertion leaves both
    # the prior knowledge row and the underlying truth untouched.
    current = _find_current_knowledge(db, campaign.id, subject.id, kind, tid)
    source = validate_acquisition_source(acquisition_source)
    prov = _normalize_mapping(provenance, field="provenance")
    if source and "acquisition_source" not in prov:
        prov = {**prov, "acquisition_source": source}
    det = dict(details or {})
    op = (str(operation_id)[:128] if operation_id else None)
    # Coerce explicitly supplied source refs up front (None = not supplied,
    # leave the stored value alone on re-assertion).
    new_source_turn = _coerce_optional_uuid(source_turn_id) if source_turn_id is not None else None
    new_source_attempt = _coerce_optional_uuid(source_attempt_id) if source_attempt_id is not None else None
    new_source_event = _coerce_optional_uuid(source_event_id) if source_event_id is not None else None

    if current is not None:
        current.subject_kind = skind
        current.knowledge_state = state
        current.acquisition_source = source
        current.visibility = vis
        current.provenance = {**(current.provenance or {}), **prov}
        if details is not None:
            current.details = det
        if source_turn_id is not None:
            current.source_turn_id = new_source_turn
        if source_attempt_id is not None:
            current.source_attempt_id = new_source_attempt
        if source_event_id is not None:
            current.source_event_id = new_source_event
        if op:
            current.operation_id = op
        if key and not current.idempotency_key:
            current.idempotency_key = key
        db.flush()
        structured_log(
            logger, logging.INFO, "world_knowledge_updated",
            campaign_id=str(campaign.id), knowledge_id=str(current.id),
            subject_kind=skind, knowledge_state=state, visibility=vis,
            operation_id=op,
        )
        return current, False

    row = WorldKnowledge(
        id=uuid.uuid4(), campaign_id=campaign.id,
        subject_kind=skind, subject_entity_id=subject.id,
        target_kind=kind,
        target_fact_id=tid if kind == "fact" else None,
        target_relation_id=tid if kind == "relation" else None,
        target_entity_id=tid if kind == "entity" else None,
        knowledge_state=state, acquisition_source=source,
        visibility=vis, provenance=prov, details=det,
        source_turn_id=new_source_turn,
        source_attempt_id=new_source_attempt,
        source_event_id=new_source_event,
        operation_id=op, idempotency_key=key,
    )
    db.add(row)
    db.flush()
    structured_log(
        logger, logging.INFO, "world_knowledge_asserted",
        campaign_id=str(campaign.id), knowledge_id=str(row.id),
        subject_kind=skind, target_kind=kind, knowledge_state=state,
        visibility=vis, acquisition_source=source, operation_id=op,
    )
    return row, True


def _coerce_optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _coerce_uuid(value, field="source_ref")


def get_knowledge_strict(db: Session, campaign_id: uuid.UUID, knowledge_id: Any) -> WorldKnowledge:
    row = db.get(WorldKnowledge, _coerce_uuid(knowledge_id, field="knowledge_id"))
    if row is None or row.campaign_id != campaign_id:
        raise ValueError(f"World knowledge {knowledge_id} not found in campaign {campaign_id}")
    return row


def list_knowledge_for_subject(
    db: Session, campaign_id: uuid.UUID, subject_entity_id: Any, *,
    knowledge_state: str | None = None, limit: int = 100,
) -> list[WorldKnowledge]:
    sid = _coerce_uuid(subject_entity_id, field="subject_entity_id")
    q = select(WorldKnowledge).where(
        WorldKnowledge.campaign_id == campaign_id,
        WorldKnowledge.subject_entity_id == sid,
    )
    if knowledge_state is not None:
        q = q.where(WorldKnowledge.knowledge_state == validate_knowledge_state(knowledge_state))
    q = q.order_by(WorldKnowledge.created_at.asc()).limit(max(1, min(int(limit or 100), 200)))
    return list(db.execute(q).scalars().all())


def list_knowledge_for_target(
    db: Session, campaign_id: uuid.UUID, target_kind: str, target_id: Any, *,
    knowledge_state: str | None = None, limit: int = 100,
) -> list[WorldKnowledge]:
    kind = validate_knowledge_target_kind(target_kind)
    tid = _coerce_uuid(target_id, field="target_id")
    q = select(WorldKnowledge).where(
        WorldKnowledge.campaign_id == campaign_id,
        WorldKnowledge.target_kind == kind,
    )
    if kind == "fact":
        q = q.where(WorldKnowledge.target_fact_id == tid)
    elif kind == "relation":
        q = q.where(WorldKnowledge.target_relation_id == tid)
    else:
        q = q.where(WorldKnowledge.target_entity_id == tid)
    if knowledge_state is not None:
        q = q.where(WorldKnowledge.knowledge_state == validate_knowledge_state(knowledge_state))
    q = q.order_by(WorldKnowledge.created_at.asc()).limit(max(1, min(int(limit or 100), 200)))
    return list(db.execute(q).scalars().all())


def assert_knowledge_authoritative(
    db: Session, campaign_id: uuid.UUID, expected_revision: int, *,
    operation_id: str | None = None, actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None, **kwargs: Any,
) -> tuple[WorldKnowledge, Any]:
    """Revision-ordered knowledge assertion with a durable domain event."""
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_knowledge_by_idempotency(db, campaign_id, key)
        if existing is not None:
            return existing, None
    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        from app.campaigns.service import require_playable_campaign

        require_playable_campaign(campaign)
        row, _ = assert_knowledge_inline(
            db, campaign, operation_id=operation_id, idempotency_key=key, **kwargs
        )
        holder["knowledge_id"] = row.id

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.knowledge_asserted",
        payload_builder=lambda: {
            "knowledge_id": str(holder["knowledge_id"]),
            "idempotency_key": key,
        },
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=lambda: {"knowledge_id": str(holder["knowledge_id"])},
        visibility="dm_only",
        provenance={"source": "world_api", "idempotency_key": key},
        mutate=_mutate,
    )
    return db.get(WorldKnowledge, holder["knowledge_id"]), event


# ── Visibility grants (arbitrary authorized subsets) ────────────────────────

def _find_grant_by_idempotency(
    db: Session, campaign_id: uuid.UUID, key: str | None
) -> WorldVisibilityGrant | None:
    if not key:
        return None
    return db.execute(
        select(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign_id,
            WorldVisibilityGrant.idempotency_key == key,
        )
    ).scalars().first()


def _resolve_grant_target(
    db: Session, campaign_id: uuid.UUID, target_kind: str, target_id: Any
) -> uuid.UUID:
    kind = validate_grant_target_kind(target_kind)
    tid = _coerce_uuid(target_id, field="target_id")
    record = _load_record(db, kind, tid)
    if record is None or getattr(record, "campaign_id", None) != campaign_id:
        structured_log(
            logger, logging.WARNING, "world_grant_target_unresolved",
            campaign_id=str(campaign_id), target_kind=kind,
            reason="record_not_in_campaign",
        )
        raise ValueError(f"grant target {kind}:{tid} not found in campaign {campaign_id}")
    return tid


def _resolve_grantee(db: Session, campaign: Campaign, grantee_user_id: Any) -> uuid.UUID:
    gid = _coerce_uuid(grantee_user_id, field="grantee_user_id")
    profile = db.get(Profile, gid)
    if profile is None:
        raise ValueError(f"grantee {gid} is not a known user")
    if campaign.owner_id != gid and not is_campaign_member(db, campaign.id, gid):
        raise ValueError(f"grantee {gid} is not a member of campaign {campaign.id}")
    return gid


def list_active_grants(
    db: Session, campaign_id: uuid.UUID, target_kind: str, target_id: Any,
) -> list[WorldVisibilityGrant]:
    kind = validate_grant_target_kind(target_kind)
    tid = _coerce_uuid(target_id, field="target_id")
    return list(db.execute(
        select(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign_id,
            WorldVisibilityGrant.target_kind == kind,
            WorldVisibilityGrant.target_id == tid,
            WorldVisibilityGrant.revoked_at.is_(None),
        ).order_by(WorldVisibilityGrant.created_at.asc())
    ).scalars().all())


def has_active_grant(
    db: Session, campaign_id: uuid.UUID, target_kind: str,
    target_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    return db.execute(
        select(func.count()).select_from(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign_id,
            WorldVisibilityGrant.target_kind == target_kind,
            WorldVisibilityGrant.target_id == target_id,
            WorldVisibilityGrant.grantee_user_id == user_id,
            WorldVisibilityGrant.revoked_at.is_(None),
        )
    ).scalar_one() > 0


def grant_visibility_inline(
    db: Session, campaign: Campaign, *, target_kind: str, target_id: Any,
    grantee_user_id: Any, granted_by: Any | None = None,
    operation_id: str | None = None, idempotency_key: str | None = None,
) -> tuple[WorldVisibilityGrant, bool]:
    """Authorize one human user for one record (idempotent; flushes only)."""
    kind = validate_grant_target_kind(target_kind)
    tid = _resolve_grant_target(db, campaign.id, kind, target_id)
    gid = _resolve_grantee(db, campaign, grantee_user_id)
    by = _coerce_uuid(granted_by, field="granted_by") if granted_by else None
    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if idempotency_key:
        key = _normalize_idempotency_key(idempotency_key)
    if key:
        dup = _find_grant_by_idempotency(db, campaign.id, key)
        if dup is not None:
            return dup, False
    existing = db.execute(
        select(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign.id,
            WorldVisibilityGrant.target_kind == kind,
            WorldVisibilityGrant.target_id == tid,
            WorldVisibilityGrant.grantee_user_id == gid,
            WorldVisibilityGrant.revoked_at.is_(None),
        )
    ).scalars().first()
    if existing is not None:
        return existing, False
    row = WorldVisibilityGrant(
        id=uuid.uuid4(), campaign_id=campaign.id, target_kind=kind,
        target_id=tid, grantee_user_id=gid, granted_by=by,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    structured_log(
        logger, logging.INFO, "world_visibility_granted",
        campaign_id=str(campaign.id), target_kind=kind,
        grantee_user_id=str(gid),
        operation_id=str(operation_id) if operation_id else None,
    )
    return row, True


def revoke_visibility_inline(
    db: Session, campaign: Campaign, *, target_kind: str, target_id: Any,
    grantee_user_id: Any, operation_id: str | None = None,
) -> bool:
    """Revoke one active grant (soft: sets revoked_at, history preserved).

    Returns True when an active grant was revoked; False when none existed
    (still fail-closed for future reads). Never deletes rows.
    """
    kind = validate_grant_target_kind(target_kind)
    tid = _coerce_uuid(target_id, field="target_id")
    gid = _coerce_uuid(grantee_user_id, field="grantee_user_id")
    row = db.execute(
        select(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign.id,
            WorldVisibilityGrant.target_kind == kind,
            WorldVisibilityGrant.target_id == tid,
            WorldVisibilityGrant.grantee_user_id == gid,
            WorldVisibilityGrant.revoked_at.is_(None),
        )
    ).scalars().first()
    if row is None:
        structured_log(
            logger, logging.INFO, "world_visibility_revoke_noop",
            campaign_id=str(campaign.id), target_kind=kind,
            grantee_user_id=str(gid),
        )
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.flush()
    structured_log(
        logger, logging.INFO, "world_visibility_revoked",
        campaign_id=str(campaign.id), target_kind=kind,
        grantee_user_id=str(gid),
        operation_id=str(operation_id) if operation_id else None,
    )
    return True


def grant_visibility_authoritative(
    db: Session, campaign_id: uuid.UUID, expected_revision: int, *,
    operation_id: str | None = None, actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None, target_kind: str = "",
    target_id: Any = None, grantee_user_id: Any = None,
) -> tuple[WorldVisibilityGrant, Any]:
    from app.campaigns.events import commit_campaign_mutation

    key = _normalize_idempotency_key(idempotency_key or operation_id)
    if key:
        existing = _find_grant_by_idempotency(db, campaign_id, key)
        if existing is not None:
            return existing, None
    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        from app.campaigns.service import require_playable_campaign

        require_playable_campaign(campaign)
        row, _ = grant_visibility_inline(
            db, campaign, target_kind=target_kind, target_id=target_id,
            grantee_user_id=grantee_user_id, granted_by=actor_id,
            operation_id=operation_id, idempotency_key=key,
        )
        holder["grant_id"] = row.id

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.visibility_granted",
        payload_builder=lambda: {
            "grant_id": str(holder["grant_id"]),
            "target_kind": validate_grant_target_kind(target_kind),
            "idempotency_key": key,
        },
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=lambda: {"grant_id": str(holder["grant_id"])},
        visibility="dm_only",
        provenance={"source": "world_api", "idempotency_key": key},
        mutate=_mutate,
    )
    row = db.get(WorldVisibilityGrant, holder["grant_id"])
    # Issue #250: a committed grant expands someone's projection — publish
    # the audience-neutral invalidation post-commit so clients reload. Best
    # effort: never breaks the authoritative commit.
    try:
        from app.realtime.service import publish_projection_invalidated_for_grantee

        publish_projection_invalidated_for_grantee(
            db, campaign_after, grantee_user_id=grantee_user_id,
        )
    except Exception:
        pass
    return row, event


def revoke_visibility_authoritative(
    db: Session, campaign_id: uuid.UUID, expected_revision: int, *,
    operation_id: str | None = None, actor_id: uuid.UUID | None = None,
    target_kind: str = "", target_id: Any = None,
    grantee_user_id: Any = None,
) -> tuple[bool, Any]:
    from app.campaigns.events import commit_campaign_mutation

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        from app.campaigns.service import require_playable_campaign

        require_playable_campaign(campaign)
        holder["revoked"] = revoke_visibility_inline(
            db, campaign, target_kind=target_kind, target_id=target_id,
            grantee_user_id=grantee_user_id, operation_id=operation_id,
        )

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.visibility_revoked",
        payload={
            "target_kind": validate_grant_target_kind(target_kind),
            "target_id": str(target_id),
            "grantee_user_id": str(grantee_user_id),
        },
        operation_id=operation_id,
        actor_id=actor_id,
        visibility="dm_only",
        provenance={"source": "world_api"},
        mutate=_mutate,
    )
    revoked = bool(holder.get("revoked"))
    if revoked:
        # Issue #250: a committed revoke contracts someone's projection —
        # publish the audience-neutral invalidation post-commit. Best effort.
        try:
            from app.realtime.service import publish_projection_invalidated_for_grantee

            publish_projection_invalidated_for_grantee(
                db, campaign_after, grantee_user_id=grantee_user_id,
            )
        except Exception:
            pass
    return revoked, event


# ── Authorization + projection (server-side, RLS-compatible) ────────────────

def _membership_allowed(db: Session, campaign: Campaign, user_id: uuid.UUID) -> bool:
    return campaign.owner_id == user_id or is_campaign_member(db, campaign.id, user_id)


def may_user_receive(
    db: Session, campaign: Campaign, target_kind: str, target_id: Any,
    user_id: Any,
) -> dict[str, Any]:
    """Reusable authorization query: may human U receive record R?

    Never infers from related records — only R's own visibility + R's own
    active grants. Unknown/ambiguous state denies (fail closed). Owner status
    grants ``dm_only`` (DM authority) but never ``private``.
    """
    try:
        kind = validate_grant_target_kind(target_kind)
        tid = _coerce_uuid(target_id, field="target_id")
        uid = _coerce_uuid(user_id, field="user_id")
    except ValueError:
        return {"allowed": False, "reason": "record_not_found"}
    if not _membership_allowed(db, campaign, uid):
        structured_log(
            logger, logging.INFO, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind, reason="not_campaign_member",
        )
        return {"allowed": False, "reason": "not_campaign_member"}
    record = _load_record(db, kind, tid)
    if record is None or getattr(record, "campaign_id", None) != campaign.id:
        return {"allowed": False, "reason": "record_not_found"}
    try:
        vis = normalize_record_visibility(_record_visibility(record))
    except ValueError:
        structured_log(
            logger, logging.WARNING, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind, reason="ambiguous_visibility",
        )
        return {"allowed": False, "reason": "ambiguous_visibility"}
    if vis in MEMBER_VISIBILITIES:
        return {"allowed": True, "reason": "member_visible"}
    if vis in AUTHORITY_VISIBILITIES:
        if is_world_authority(campaign, uid):
            return {"allowed": True, "reason": "dm_authority"}
        structured_log(
            logger, logging.INFO, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind,
            reason="dm_only_requires_authority",
        )
        return {"allowed": False, "reason": "dm_only_requires_authority"}
    # Private: exactly the active grantee set — owner included only if granted.
    if has_active_grant(db, campaign.id, kind, tid, uid):
        return {"allowed": True, "reason": "explicit_grant"}
    structured_log(
        logger, logging.INFO, "world_access_denied",
        campaign_id=str(campaign.id), target_kind=kind, reason="private_requires_grant",
    )
    return {"allowed": False, "reason": "private_requires_grant"}


def _knowledge_target_ref(row: WorldKnowledge) -> tuple[str, uuid.UUID]:
    if row.target_kind == "fact" and row.target_fact_id:
        return "fact", row.target_fact_id
    if row.target_kind == "relation" and row.target_relation_id:
        return "relation", row.target_relation_id
    if row.target_kind == "entity" and row.target_entity_id:
        return "entity", row.target_entity_id
    raise ValueError("knowledge row has ambiguous target (fail closed)")


def what_does_subject_know(
    db: Session, campaign: Campaign, subject_entity_id: Any, viewer_user_id: Any, *,
    knowledge_state: str | None = None, include_target: bool = True, limit: int = 100,
) -> dict[str, Any]:
    """Projection: what may viewer U see of subject X's knowledge?

    Fictional attribution (X holds stance S toward T) and human disclosure
    (may U receive the subject entity? the knowledge row? T?) are checked
    independently — all must allow, else the entry is counted as denied
    without leaking ids/content.
    """
    try:
        viewer = _coerce_uuid(viewer_user_id, field="viewer_user_id")
        subject = _resolve_subject(db, campaign.id, subject_entity_id)
    except ValueError:
        return {
            "subject_entity_id": str(subject_entity_id),
            "entries": [], "total": 0, "visible": 0, "denied": 0,
            "denied_reasons": {"record_not_found": 1},
        }
    if not may_user_receive(db, campaign, "entity", subject.id, viewer)["allowed"]:
        # The subject itself is hidden from this viewer: disclose neither its
        # knowledge rows nor their count.
        return {
            "subject_entity_id": str(subject.id),
            "entries": [], "total": 0, "visible": 0, "denied": 0,
            "denied_reasons": {"subject_not_visible": 1},
        }
    rows = list_knowledge_for_subject(
        db, campaign.id, subject.id,
        knowledge_state=knowledge_state, limit=limit,
    )
    entries: list[dict] = []
    denied_reasons: dict[str, int] = {}
    for row in rows:
        knowable = may_user_receive(db, campaign, "knowledge", row.id, viewer)
        if not knowable["allowed"]:
            denied_reasons["knowledge_not_visible"] = denied_reasons.get("knowledge_not_visible", 0) + 1
            continue
        try:
            tkind, tid = _knowledge_target_ref(row)
        except ValueError:
            denied_reasons["ambiguous_visibility"] = denied_reasons.get("ambiguous_visibility", 0) + 1
            continue
        target_ok = may_user_receive(db, campaign, tkind, tid, viewer)
        if not target_ok["allowed"]:
            denied_reasons["target_not_visible"] = denied_reasons.get("target_not_visible", 0) + 1
            continue
        entry: dict[str, Any] = {
            "knowledge_id": str(row.id),
            "subject_kind": row.subject_kind,
            "subject_entity_id": str(row.subject_entity_id),
            "target_kind": tkind,
            "target_id": str(tid),
            "knowledge_state": row.knowledge_state,
            "acquisition_source": row.acquisition_source,
        }
        if include_target:
            target = _load_record(db, tkind, tid)
            entry["target"] = target.to_dict() if target is not None else None
        entries.append(entry)
    denied = len(rows) - len(entries)
    return {
        "subject_entity_id": str(subject.id),
        "entries": entries,
        "total": len(rows),
        "visible": len(entries),
        "denied": denied,
        "denied_reasons": denied_reasons,
    }


def who_knows_target(
    db: Session, campaign: Campaign, target_kind: str, target_id: Any,
    viewer_user_id: Any, *, knowledge_state: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    """Projection: which subjects may viewer U see as holding target T?

    The viewer must independently be allowed the target, each knowledge row,
    AND each knower's subject entity; knower identities behind denied rows or
    hidden subjects are never listed.
    """
    try:
        kind = validate_knowledge_target_kind(target_kind)
        tid = _coerce_uuid(target_id, field="target_id")
        viewer = _coerce_uuid(viewer_user_id, field="viewer_user_id")
    except ValueError:
        return {
            "target_kind": str(target_kind), "target_id": str(target_id),
            "knowers": [], "total": 0, "visible": 0, "denied": 0,
            "denied_reasons": {"record_not_found": 1},
        }
    if may_user_receive(db, campaign, kind, tid, viewer)["allowed"] is False:
        return {
            "target_kind": kind, "target_id": str(tid),
            "knowers": [], "total": 0, "visible": 0, "denied": 0,
            "denied_reasons": {"target_not_visible": 1},
        }
    rows = list_knowledge_for_target(
        db, campaign.id, kind, tid, knowledge_state=knowledge_state, limit=limit,
    )
    knowers: list[dict] = []
    denied_reasons: dict[str, int] = {}
    for row in rows:
        if not may_user_receive(db, campaign, "knowledge", row.id, viewer)["allowed"]:
            denied_reasons["knowledge_not_visible"] = denied_reasons.get("knowledge_not_visible", 0) + 1
            continue
        if not may_user_receive(db, campaign, "entity", row.subject_entity_id, viewer)["allowed"]:
            denied_reasons["subject_not_visible"] = denied_reasons.get("subject_not_visible", 0) + 1
            continue
        knowers.append({
            "knowledge_id": str(row.id),
            "subject_kind": row.subject_kind,
            "subject_entity_id": str(row.subject_entity_id),
            "knowledge_state": row.knowledge_state,
            "acquisition_source": row.acquisition_source,
        })
    denied = len(rows) - len(knowers)
    reasons = dict(denied_reasons)
    return {
        "target_kind": kind, "target_id": str(tid),
        "knowers": knowers, "total": len(rows),
        "visible": len(knowers), "denied": denied,
        "denied_reasons": reasons,
    }


def _project_records_for_user(
    db: Session, campaign: Campaign, viewer_user_id: Any,
    records: list[Any], target_kind: str,
) -> dict[str, Any]:
    try:
        viewer = _coerce_uuid(viewer_user_id, field="viewer_user_id")
    except ValueError:
        return {
            "records": [], "total": len(records), "visible": 0,
            "denied": len(records), "denied_reasons": {"record_not_found": len(records)},
        }
    visible: list[dict] = []
    denied_reasons: dict[str, int] = {}
    for record in records:
        verdict = may_user_receive(db, campaign, target_kind, record.id, viewer)
        if verdict["allowed"]:
            visible.append(record.to_dict())
        else:
            reason = verdict["reason"]
            denied_reasons[reason] = denied_reasons.get(reason, 0) + 1
    return {
        "records": visible,
        "total": len(records),
        "visible": len(visible),
        "denied": len(records) - len(visible),
        "denied_reasons": denied_reasons,
    }


def project_facts_for_user(
    db: Session, campaign: Campaign, viewer_user_id: Any,
    facts: list[WorldFact],
) -> dict[str, Any]:
    """Viewer projection over an explicit fact set with leak-free counts."""
    return _project_records_for_user(db, campaign, viewer_user_id, facts, "fact")


# ── #251 judge scoping: subject-unknown restricted texts ─────────────────

RESTRICTED_FACT_SCAN_LIMIT = 100
RESTRICTED_TEXT_LIMIT = 32
RESTRICTED_TEXT_CHARS = 500


def collect_subject_restricted_fact_texts(
    db: Session, campaign: Campaign, speaker_subject_ids: Any, *,
    limit_facts: int = RESTRICTED_FACT_SCAN_LIMIT,
    limit_texts: int = RESTRICTED_TEXT_LIMIT,
    max_chars: int = RESTRICTED_TEXT_CHARS,
) -> dict[str, set[str]]:
    """Per-speaker fact texts that speaker could not know — for the judge.

    Derived from each speaker's ``WorldKnowledge`` independently of human
    visibility: a campaign/public OOC fact a speaker never learned is still
    that speaker's misuse to state, so all visibilities are scanned. A text
    maps to every resolved speaker lacking knowledge of that fact; speakers
    that do not resolve to a campaign entity are excluded (their scope is
    unknowable, never assumed). An explicit ``does_not_know`` stance never
    counts as coverage. Bounded to recent rows and capped texts; returns
    possibly-empty; never raises for misshapen input (callers treat failure
    as no extra scope).
    """
    try:
        speaker_ids = [str(s).strip() for s in (speaker_subject_ids or []) if str(s or "").strip()]
    except TypeError:
        return {}
    subjects: list[WorldEntity] = []
    for raw in speaker_ids[:16]:
        try:
            sid = _coerce_uuid(raw, field="subject_entity_id")
        except ValueError:
            continue
        entity = db.get(WorldEntity, sid)
        if entity is not None and entity.campaign_id == campaign.id:
            subjects.append(entity)
    if not subjects:
        return {}
    try:
        known_per_speaker: list[tuple[str, set[str]]] = []
        for subject in subjects:
            rows = list_knowledge_for_subject(db, campaign.id, subject.id, limit=200)
            known: set[str] = set()
            for row in rows:
                # An explicit does_not_know stance is not coverage.
                if row.knowledge_state == "does_not_know":
                    continue
                if row.target_kind == "fact" and row.target_fact_id is not None:
                    known.add(str(row.target_fact_id))
            known_per_speaker.append((str(subject.id), known))
        scan = max(1, min(int(limit_facts or 100), 500))
        facts = list(db.execute(
            select(WorldFact).where(
                WorldFact.campaign_id == campaign.id,
            ).order_by(WorldFact.created_at.desc()).limit(scan)
        ).scalars().all())
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    cap_texts = max(1, min(int(limit_texts or 32), 64))
    cap_chars = max(1, min(int(max_chars or 500), 4000))
    total = 0
    for fact in facts:
        fid = str(fact.id)
        content = str(getattr(fact, "content", None) or "").strip()
        if not content:
            continue
        for speaker_id, known in known_per_speaker:
            if fid in known:
                continue
            out.setdefault(speaker_id, set()).add(content[:cap_chars])
            total += 1
            if total >= cap_texts:
                return out
    return out


def project_relations_for_user(
    db: Session, campaign: Campaign, viewer_user_id: Any,
    relations: list[WorldRelation],
) -> dict[str, Any]:
    """Viewer projection over an explicit relation set with leak-free counts."""
    return _project_records_for_user(db, campaign, viewer_user_id, relations, "relation")


# ── #251 knowledge-visibility lane reader (DM-internal, adjudication-only) ──

KNOWLEDGE_LANE_MAX_ENTRIES_PER_SUBJECT = 50


def _resolve_subject_for_character(
    db: Session, campaign_id: uuid.UUID, character_id: Any,
) -> WorldEntity | None:
    """Map a canonical PC (characters.id) to its WorldEntity subject, if any.

    PCs materialize into world_entities via post-turn (#217); before that no
    subject row exists and the lane reports an explicit empty perspective
    rather than failing. Never raises: unresolved maps to None.
    """
    try:
        cid = _coerce_uuid(character_id, field="character_id")
    except ValueError:
        return None
    direct = db.get(WorldEntity, cid)
    if direct is not None and direct.campaign_id == campaign_id:
        return direct
    try:
        candidates = list(
            db.execute(
                select(WorldEntity).where(
                    WorldEntity.campaign_id == campaign_id,
                    WorldEntity.entity_type == "character",
                ).limit(200)
            ).scalars().all()
        )
    except Exception:
        return None
    needle = str(cid)
    for entity in candidates:
        details = getattr(entity, "details", None) or {}
        if isinstance(details, dict):
            for key in ("character_id", "pc_id", "canonical_character_id"):
                if str(details.get(key) or "") == needle:
                    return entity
    return None


def _subject_knowledge_value(
    db: Session, campaign: Campaign, subject: WorldEntity | None, *,
    character_id: Any = None, perspective: str = "character",
    limit: int = KNOWLEDGE_LANE_MAX_ENTRIES_PER_SUBJECT,
) -> dict[str, Any]:
    """One DM-internal perspective snapshot for a resolved-or-empty subject."""
    if subject is None:
        return {
            "character_id": str(character_id) if character_id is not None else None,
            "subject_entity_id": None,
            "subject_resolved": False,
            "perspective": perspective,
            "entries": [],
            "total": 0,
            "truncated": False,
        }
    rows = list_knowledge_for_subject(db, campaign.id, subject.id, limit=limit + 1)
    truncated = len(rows) > limit
    entries: list[dict[str, Any]] = []
    for row in rows[:limit]:
        try:
            tkind, tid = _knowledge_target_ref(row)
        except ValueError:
            continue
        entries.append({
            "knowledge_id": str(row.id),
            "target_kind": tkind,
            "target_id": str(tid),
            "knowledge_state": row.knowledge_state,
            "acquisition_source": row.acquisition_source,
            "visibility": getattr(row, "visibility", "dm_only"),
        })
    return {
        "character_id": str(character_id) if character_id is not None else None,
        "subject_entity_id": str(subject.id),
        "subject_resolved": True,
        "perspective": perspective,
        "entries": entries,
        "total": len(rows),
        "truncated": truncated,
    }


def build_knowledge_visibility_values(
    db: Session, campaign: Campaign, character_ids: Any, *,
    npc_entity_ids: Any = None,
    max_entries_per_subject: int = KNOWLEDGE_LANE_MAX_ENTRIES_PER_SUBJECT,
) -> list[dict[str, Any]]:
    """DM-internal per-subject knowledge snapshots for the #202 lane.

    Covers acting PCs (``character_ids``) plus scene-relevant non-player
    subjects (``npc_entity_ids``: NPC/group WorldEntity IDs, e.g. from the
    current scene's present actors). Returns one value dict per subject plus
    a single empty value when no subject is relevant at all, so the REQUIRED
    lane is always satisfiable without fabricating knowledge. Values carry
    target refs + stances only (no truth text); the DM retrieves full
    evidence through #212 tools. Raises only on DB failure (caller fails
    closed); unresolved subjects yield explicit empty entries.
    """
    try:
        ids = list(character_ids or [])
    except TypeError:
        ids = []
    try:
        npc_ids = list(npc_entity_ids or [])
    except TypeError:
        npc_ids = []
    if not ids and not npc_ids:
        return [{"perspectives": [], "note": "no_pc_in_attempt"}]
    limit = max(1, min(int(max_entries_per_subject or 50), 200))
    values: list[dict[str, Any]] = []
    for character_id in ids:
        subject = _resolve_subject_for_character(db, campaign.id, character_id)
        values.append(_subject_knowledge_value(
            db, campaign, subject, character_id=character_id,
            perspective="character", limit=limit,
        ))
    for npc_id in npc_ids[:32]:
        try:
            eid = _coerce_uuid(npc_id, field="subject_entity_id")
        except ValueError:
            continue
        entity = db.get(WorldEntity, eid)
        subject = entity if entity is not None and entity.campaign_id == campaign.id else None
        values.append(_subject_knowledge_value(
            db, campaign, subject, perspective="npc", limit=limit,
        ))
    return values
