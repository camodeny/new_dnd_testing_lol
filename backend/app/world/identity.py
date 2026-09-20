"""Deterministic and bounded semantic identity resolution (issue #214)."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.decisions import (
    ACTIVE, DIRECT_EXECUTE, CandidateRecord, DecisionClassPolicy, DecisionError,
    DecisionFrame, DecisionService, build_frame, build_record, evaluate_execution,
    record_fail_soft, register_policy, revalidate_for_execution, to_decision_request,
)
from models.campaigns import Campaign
from models.world import WorldEntity, WorldEntityAlias

IDENTITY_DECISION_CLASS = "world_entity_identity"
NEW_ENTITY = "NEW_ENTITY"
KEEP_DISTINCT = "KEEP_DISTINCT"
DEFER = "DEFER"

register_policy(DecisionClassPolicy(
    decision_class=IDENTITY_DECISION_CLASS,
    min_probability_direct=.85, min_confidence_direct=.8,
    min_margin_direct=.25, near_tie_margin=.25,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))


def normalize_alias(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _active_entities(db: Session, campaign_id: uuid.UUID):
    return select(WorldEntity).where(
        WorldEntity.campaign_id == campaign_id,
        WorldEntity.superseded_by_id.is_(None),
    )


def exact_identity_match(db: Session, campaign_id: uuid.UUID, reference: Any) -> tuple[WorldEntity | None, str | None]:
    """Deterministic match plus how it matched: ``"uuid"``, ``"alias"``, or ``"name"``.

    ``(None, None)`` when nothing resolves. UUID/alias hits are stable
    refs that must reuse the owner without any model call; only exact
    canonical-name collisions may enter bounded resolution (where
    ``KEEP_DISTINCT`` is legal).
    """
    raw = str(reference or "").strip()
    if not raw:
        return None, None
    try:
        entity_id = uuid.UUID(raw)
    except ValueError:
        entity_id = None
    if entity_id:
        entity = db.get(WorldEntity, entity_id)
        if entity and entity.campaign_id == campaign_id and not entity.superseded_by_id:
            return entity, "uuid"
        return None, None
    normalized = normalize_alias(raw)
    alias = db.execute(select(WorldEntityAlias).where(
        WorldEntityAlias.campaign_id == campaign_id,
        WorldEntityAlias.normalized_alias == normalized,
    )).scalars().first()
    if alias:
        entity = db.get(WorldEntity, alias.entity_id)
        return (entity, "alias") if entity and not entity.superseded_by_id else (None, None)
    matches = list(db.execute(_active_entities(db, campaign_id)).scalars())
    exact = [e for e in matches if normalize_alias(e.name) == normalized]
    return (exact[0], "name") if len(exact) == 1 else (None, None)


def exact_identity(db: Session, campaign_id: uuid.UUID, reference: Any) -> WorldEntity | None:
    """Resolve a stable UUID, canonical name, or alias without an AI call."""
    entity, _ = exact_identity_match(db, campaign_id, reference)
    return entity


def add_alias(
    db: Session, entity: WorldEntity, alias: str, *, visibility: str = "campaign",
    provenance: dict | None = None,
) -> WorldEntityAlias:
    from app.world.service import normalize_visibility, validate_entity_name
    display = validate_entity_name(alias)
    normalized = normalize_alias(display)
    existing = db.execute(select(WorldEntityAlias).where(
        WorldEntityAlias.campaign_id == entity.campaign_id,
        WorldEntityAlias.normalized_alias == normalized,
    )).scalars().first()
    if existing:
        if existing.entity_id != entity.id:
            raise ValueError("alias already belongs to another canonical entity")
        return existing
    canonical_name_collision = next((candidate for candidate in
        db.execute(_active_entities(db, entity.campaign_id)).scalars()
        if candidate.id != entity.id and normalize_alias(candidate.name) == normalized), None)
    if canonical_name_collision:
        raise ValueError("alias collides with another canonical entity name")
    row = WorldEntityAlias(campaign_id=entity.campaign_id, entity_id=entity.id,
                           alias=display, normalized_alias=normalized,
                           visibility=normalize_visibility(visibility), provenance=dict(provenance or {}))
    db.add(row)
    entity.revision = int(entity.revision or 1) + 1
    db.flush()
    return row


def alias_owner(db: Session, campaign_id: uuid.UUID, name: Any) -> WorldEntity | None:
    """Live canonical entity holding ``name`` as an exact alias, if any."""
    normalized = normalize_alias(name)
    if not normalized:
        return None
    row = db.execute(select(WorldEntityAlias).where(
        WorldEntityAlias.campaign_id == campaign_id,
        WorldEntityAlias.normalized_alias == normalized,
    )).scalars().first()
    if row is None:
        return None
    entity = db.get(WorldEntity, row.entity_id)
    return entity if entity is not None and not entity.superseded_by_id else None


def candidate_entities(
    db: Session, campaign_id: uuid.UUID, *, name: str, entity_type: str | None = None,
    location_ref: str | None = None, provenance_refs: tuple[str, ...] = (),
    is_authority: bool = True, limit: int = 8,
) -> list[WorldEntity]:
    """Return only plausible, authorized real entities in deterministic order."""
    normalized = normalize_alias(name)
    rows = list(db.execute(_active_entities(db, campaign_id)).scalars())
    aliases = list(db.execute(select(WorldEntityAlias).where(WorldEntityAlias.campaign_id == campaign_id)).scalars())
    aliases_by_entity: dict[uuid.UUID, list[WorldEntityAlias]] = {}
    for alias in aliases:
        if is_authority or alias.visibility not in {"private", "dm_only"}:
            aliases_by_entity.setdefault(alias.entity_id, []).append(alias)
    scored = []
    for entity in rows:
        if not is_authority and entity.visibility in {"private", "dm_only"}:
            continue
        names = [entity.name] + [a.alias for a in aliases_by_entity.get(entity.id, [])]
        score = max(SequenceMatcher(None, normalized, normalize_alias(n)).ratio() for n in names)
        if entity_type and entity.entity_type == entity_type:
            score += .2
        details = entity.details or {}
        if location_ref and str(details.get("location_ref") or "") == str(location_ref):
            score += .15
        known_provenance = set(map(str, details.get("provenance_refs") or ()))
        if known_provenance.intersection(map(str, provenance_refs)):
            score += .15
        if score >= .45:
            scored.append((score, str(entity.id), entity))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:max(1, min(limit, 20))]]


def _candidate_label(entity: WorldEntity, *, aliases: list[str]) -> str:
    """Model-visible description with authorized disambiguating evidence.

    Same-name candidates must stay distinguishable in the serialized
    decision request (only IDs + descriptions cross the adapter
    boundary). Location, audience-visible aliases, and provenance hints
    travel with the label. Callers filter ``aliases`` to the frame's
    audience first, so hidden aliases never reach non-authority frames.
    """
    label = f"{entity.entity_type}: {entity.name}"
    hints: list[str] = []
    details = entity.details or {}
    location = details.get("location_ref")
    if location and str(location).strip():
        hints.append(f"location {str(location).strip()}"[:80])
    seen: list[str] = []
    for alias in sorted({a for a in aliases if a}):
        if normalize_alias(alias) == normalize_alias(entity.name):
            continue
        if alias not in seen:
            seen.append(alias)
        if len(seen) >= 3:
            break
    if seen:
        hints.append("also known as " + ", ".join(seen))
    provenance = details.get("provenance_refs") or ()
    if isinstance(provenance, (list, tuple)):
        refs = [str(p) for p in provenance if str(p).strip()][:3]
        if refs:
            hints.append("known from " + ", ".join(refs))
    if hints:
        label += " (" + "; ".join(hints) + ")"
    return label[:500]


def identity_revision(db: Session, campaign: Campaign) -> str:
    """Fingerprint every identity-bearing row, not merely campaign revision."""
    entities = list(db.execute(_active_entities(db, campaign.id)).scalars())
    aliases = list(db.execute(select(WorldEntityAlias).where(
        WorldEntityAlias.campaign_id == campaign.id
    )).scalars())
    entity_part = ",".join(
        f"{entity.id}:{int(entity.revision or 1)}:{entity.superseded_by_id or '-'}"
        for entity in sorted(entities, key=lambda row: str(row.id))
    )
    alias_part = ",".join(
        f"{alias.id}:{alias.entity_id}:{alias.normalized_alias}"
        for alias in sorted(aliases, key=lambda row: str(row.id))
    )
    return f"campaign:{int(campaign.revision)}|entities:{entity_part}|aliases:{alias_part}"


def build_identity_frame(
    db: Session, campaign: Campaign, *, name: str, entity_type: str,
    location_ref: str | None = None, provenance_refs: tuple[str, ...] = (),
    is_authority: bool = True,
) -> DecisionFrame:
    candidates = candidate_entities(db, campaign.id, name=name, entity_type=entity_type,
                                    location_ref=location_ref, provenance_refs=provenance_refs,
                                    is_authority=is_authority)
    alias_rows = list(db.execute(select(WorldEntityAlias).where(
        WorldEntityAlias.campaign_id == campaign.id)).scalars())
    aliases_by_entity: dict[Any, list[str]] = {}
    for alias_row in alias_rows:
        if is_authority or alias_row.visibility not in {"private", "dm_only"}:
            aliases_by_entity.setdefault(alias_row.entity_id, []).append(alias_row.alias)
    records = [CandidateRecord(id=str(e.id), label=_candidate_label(
        e, aliases=aliases_by_entity.get(e.id, [])),
        source="world:identity_search", payload_ref=str(e.id)) for e in candidates]
    records.extend((
        CandidateRecord(id=NEW_ENTITY, label="Create a new canonical entity", source="world:identity_outcome"),
        CandidateRecord(id=KEEP_DISTINCT, label="Keep distinct from similar existing entities", source="world:identity_outcome"),
        CandidateRecord(id=DEFER, label="Identity is ambiguous; defer", source="world:identity_outcome", risk="low"),
    ))
    return build_frame(
        decision_class=IDENTITY_DECISION_CLASS, question_id="resolve_world_entity_identity",
        instructions="Select only a supplied existing identity or an explicit outcome. Defer when evidence is insufficient.",
        state={"proposed": {"name": name, "entity_type": entity_type, "location_ref": location_ref,
                            "provenance_refs": list(provenance_refs)}},
        state_revision=identity_revision(db, campaign), candidates=records, include_escapes=False,
    )


@dataclass(frozen=True)
class IdentityDecision:
    selected_id: str
    directive: str
    frame: DecisionFrame


def decide_identity(
    db: Session, campaign: Campaign, frame: DecisionFrame, service: DecisionService,
    *, session_factory: Any = None, record_outbox: list | None = None,
) -> IdentityDecision:
    """Run, policy-check, revalidate, and record one bounded identity choice.

    Telemetry never opens an independent write while the caller holds the
    campaign lock: pass ``record_outbox`` to collect the record for a
    post-commit flush instead of ``session_factory`` on locked paths.
    """
    try:
        response = service.decide(to_decision_request(frame))
    except Exception:
        # Provider failure must never silently become NEW_ENTITY.
        return IdentityDecision(DEFER, "escalate", frame)
    result = response.results[frame.question_id]
    current = db.get(Campaign, campaign.id)
    legal_ids = {c.id for c in frame.candidates}
    revalidate_for_execution(frame, result.selected_id, identity_revision(db, current), legal_ids=legal_ids)
    verdict = evaluate_execution(frame, result.selected_id, result.probabilities,
                                 result.confidence, verified=True)
    if record_outbox is not None or session_factory is not None:
        record = build_record(frame, result, verdict, provider=response.provider,
                              model=response.model or "unknown", mode=ACTIVE,
                              trace_id=response.trace_id, operation_id=response.operation_id,
                              campaign_id=campaign.id, latency_ms=response.latency_ms, verified=True)
        if record_outbox is not None:
            record_outbox.append(record)
        else:
            record_fail_soft(session_factory, record)
    selected = result.selected_id if verdict.directive == DIRECT_EXECUTE else DEFER
    return IdentityDecision(selected, verdict.directive, frame)


def create_entity_after_resolution(
    db: Session, campaign: Campaign, frame: DecisionFrame, selected_id: str, *,
    entity_type: str, name: str, idempotency_key: str, details: dict | None = None,
    summary: str | None = None, source_turn_id: uuid.UUID | None = None,
    source_attempt_id: uuid.UUID | None = None, operation_id: str | None = None,
) -> tuple[WorldEntity, bool]:
    """Revalidate a resolved outcome immediately before its durable write."""
    prior = db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == campaign.id,
        WorldEntity.idempotency_key == str(idempotency_key).strip(),
    )).scalars().first()
    if prior is not None:
        resolution = (prior.details or {}).get("identity_resolution") or {}
        if resolution.get("frame_id") != frame.frame_id or resolution.get("outcome") != selected_id:
            raise ValueError("idempotency key belongs to a different identity resolution")
        return prior, False
    current = db.get(Campaign, campaign.id)
    candidate = revalidate_for_execution(
        frame, selected_id, identity_revision(db, current),
        legal_ids={item.id for item in frame.candidates},
    )
    if candidate.id == DEFER:
        raise DecisionError("identity resolution deferred", kind="malformed")
    if candidate.id not in {NEW_ENTITY, KEEP_DISTINCT}:
        entity = db.get(WorldEntity, uuid.UUID(candidate.id))
        if entity is None or entity.campaign_id != campaign.id or entity.superseded_by_id:
            raise DecisionError("selected canonical entity is no longer legal", kind="stale")
        return entity, False
    collision = exact_identity(db, campaign.id, name)
    if collision is not None and candidate.id != KEEP_DISTINCT:
        raise ValueError(f"new entity collides with canonical identity {collision.id}")
    if candidate.id == KEEP_DISTINCT:
        owner = alias_owner(db, campaign.id, name)
        if owner is not None:
            # The proposed canonical name is already another live entity's
            # exact alias. Persisting it as a new canonical name would leave
            # deterministic exact lookup (aliases win over canonical names)
            # pointed at the wrong identity, so fail closed. Same
            # canonical-name KEEP_DISTINCT (no alias owner) stays allowed.
            raise ValueError(
                f"new entity name {name!r} is already an alias of canonical identity {owner.id}"
            )
    from app.world.service import create_entity_inline
    payload = dict(details or {})
    payload["identity_resolution"] = {
        "frame_id": frame.frame_id, "outcome": candidate.id,
        "state_revision": frame.state_revision,
    }
    return create_entity_inline(db, campaign, entity_type=entity_type, name=name,
                                summary=summary, details=payload,
                                source_turn_id=source_turn_id,
                                source_attempt_id=source_attempt_id,
                                operation_id=operation_id,
                                idempotency_key=idempotency_key)


def supersede_entity(db: Session, duplicate: WorldEntity, canonical: WorldEntity, *, provenance: dict) -> None:
    """Auditable repair hook; callers must apply their stronger merge policy first."""
    if duplicate.campaign_id != canonical.campaign_id or duplicate.id == canonical.id:
        raise ValueError("merge identities must be distinct entities in one campaign")
    duplicate.superseded_by_id = canonical.id
    duplicate.status = "archived"
    duplicate.revision = int(duplicate.revision or 1) + 1
    details = dict(duplicate.details or {})
    details["identity_supersession"] = {"canonical_id": str(canonical.id), "provenance": dict(provenance)}
    duplicate.details = details
    db.flush()
