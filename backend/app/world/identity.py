"""Deterministic and bounded semantic identity resolution (issue #214).

JIT promotion: :func:`promote_new_entities_from_contract` assigns durable
canonical identity to committed ``new_entities`` proposals exactly once,
keyed by :func:`stable_jit_key` per (attempt, temp_id). Duplicate retry
returns the existing row without creating a duplicate.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decisions import (
    ACTIVE, DIRECT_EXECUTE, CandidateRecord, DecisionClassPolicy, DecisionError,
    DecisionFrame, DecisionService, build_frame, build_record, evaluate_execution,
    get_policy, record_fail_soft, register_policy, revalidate_for_execution, to_decision_request,
)
from app.visibility.policy import RESTRICTED_VISIBILITIES, normalize_visibility
from app.idempotency import compose_operation_id
from app.world.knowledge import assert_knowledge
from app.world.service import (
    apply_scene_update, create_entity, find_entity_by_idempotency, validate_entity_name,
)
from models.campaigns import Campaign
from models.dm import DmTurnAttempt
from models.world import CampaignCurrentScene, WorldEntity, WorldEntityAlias

IDENTITY_DECISION_CLASS = "world_entity_identity"
IDENTITY_QUESTION_ID = "resolve_world_entity_identity"
IDENTITY_FRAME_INSTRUCTIONS = (
    "Select only a supplied existing identity or an explicit outcome. "
    "Defer when evidence is insufficient."
)
NEW_ENTITY = "NEW_ENTITY"
KEEP_DISTINCT = "KEEP_DISTINCT"
DEFER = "DEFER"


class IdentityDeferredError(ValueError):
    """Identity decision deferred: fail-closed signal with retry context.

    Subclasses :class:`ValueError` so existing fail-closed handling (and
    tests matching ``"deferred"``) behaves identically — no entity is
    inserted. Carries the deferred proposal plus the domain candidate
    labels the decision weighed, so explicit-retry re-adjudication can
    disambiguate the proposal instead of looping on the same frame.
    """

    def __init__(self, temp_id: str, *, proposal: dict | None = None,
                 candidate_labels: list | None = None):
        super().__init__(f"identity resolution deferred for new entity {temp_id!r}")
        self.temp_id = temp_id
        self.proposal = dict(proposal or {})
        self.candidate_labels = list(candidate_labels or [])


class IdentityReuseRequiresReadjudication(ValueError):
    """A proposed new entity resolved to an existing canonical entity before narration."""

    def __init__(self, *, temp_id: str, proposed_name: str, entity: WorldEntity):
        super().__init__(f"new entity {temp_id!r} resolved to existing identity")
        self.temp_id = temp_id
        self.proposed_name = proposed_name
        self.canonical_id = str(entity.id)
        self.canonical_name = entity.name
        self.canonical_kind = entity.entity_type
        self.canonical_summary = str(entity.summary or "")[:200]
        self.canonical_revision = str(entity.revision or 1)

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
        if is_authority or alias.visibility not in RESTRICTED_VISIBILITIES:
            aliases_by_entity.setdefault(alias.entity_id, []).append(alias)
    scored = []
    for entity in rows:
        if not is_authority and entity.visibility in RESTRICTED_VISIBILITIES:
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
        if is_authority or alias_row.visibility not in RESTRICTED_VISIBILITIES:
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
        decision_class=IDENTITY_DECISION_CLASS, question_id=IDENTITY_QUESTION_ID,
        instructions=IDENTITY_FRAME_INSTRUCTIONS,
        state={"proposed": {"name": name, "entity_type": entity_type, "location_ref": location_ref,
                            "provenance_refs": list(provenance_refs)}},
        state_revision=identity_revision(db, campaign), candidates=records, include_escapes=False,
    )


@dataclass(frozen=True)
class IdentityDecision:
    selected_id: str
    directive: str
    frame: DecisionFrame
    runner_up_applied: bool = False


def _near_tie_runner_up(probabilities: dict, *, exclude: set) -> tuple[str | None, float]:
    """Best non-excluded candidate and its margin behind the top choice.

    Returns ``(None, inf)`` when no legal runner-up exists.
    """
    ranked = sorted(
        ((cid, float(p)) for cid, p in (probabilities or {}).items() if cid not in exclude),
        key=lambda item: item[1], reverse=True,
    )
    if not ranked:
        return None, float("inf")
    top_selected = max((float(p) for p in (probabilities or {}).values()), default=0.0)
    runner_id, runner_prob = ranked[0]
    return runner_id, top_selected - runner_prob


def decide_identity(
    db: Session, campaign: Campaign, frame: DecisionFrame, service: DecisionService,
    *, session_factory: Any = None, record_outbox: list | None = None,
    prior_deferrals: int = 0,
) -> IdentityDecision:
    """Run, policy-check, revalidate, and record one bounded identity choice.

    Telemetry never opens an independent write while the caller holds the
    campaign lock: pass ``record_outbox`` to collect the record for a
    post-commit flush instead of ``session_factory`` on locked paths.

    ``prior_deferrals`` counts earlier DEFER memos for the same proposal in
    the retry chain. When positive and this decision also DEFERs on a
    near-tie, the model's own runner-up is accepted (flagged
    ``runner_up_applied``) instead of looping forever: the first DEFER
    stays fail-closed, and every downstream revalidation (collision,
    alias, revision) still applies unchanged. Confident DEFERs (margin
    above the policy near-tie band) keep raising.
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
    runner_up_applied = False
    if selected == DEFER and prior_deferrals >= 1:
        # Retry after a memoed DEFER: the disambiguated proposal still
        # hedged. Accept the model's own near-tie runner-up rather than
        # looping — a chronic hedger would otherwise never converge.
        # Confident DEFERs and illegal runner-ups keep failing closed.
        runner_id, margin = _near_tie_runner_up(result.probabilities, exclude={DEFER})
        try:
            near_tie_margin = float(get_policy(IDENTITY_DECISION_CLASS).near_tie_margin)
        except Exception:
            near_tie_margin = 0.25
        if (
            runner_id is not None
            and runner_id in legal_ids
            and margin <= near_tie_margin
        ):
            selected = runner_id
            runner_up_applied = True
    return IdentityDecision(selected, verdict.directive, frame, runner_up_applied)


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
    payload = dict(details or {})
    payload["identity_resolution"] = {
        "frame_id": frame.frame_id, "outcome": candidate.id,
        "state_revision": frame.state_revision,
    }
    return create_entity(db, campaign, entity_type=entity_type, name=name,
                         summary=summary, details=payload,
                         source_turn_id=source_turn_id,
                         source_attempt_id=source_attempt_id,
                         operation_id=operation_id,
                         idempotency_key=idempotency_key)


def serialize_identity_frame(frame: DecisionFrame) -> dict:
    """Persist a bounded identity frame onto an attempt-local outcome payload.

    Only JSON-safe code-owned enumeration data crosses: candidate IDs +
    labels + sources, the proposed-entity state, frame ID, and the revision
    the candidates were enumerated against. Model telemetry stays out;
    commit-time revalidation rebuilds via :func:`rebuild_identity_frame`.
    """
    return {
        "frame_id": frame.frame_id,
        "state_revision": frame.state_revision,
        "state": frame.state,
        "candidates": [
            {
                "id": candidate.id, "label": candidate.label,
                "source": candidate.source,
                "source_ref": candidate.source_ref,
                "payload_ref": candidate.payload_ref,
                "debug_hint": candidate.debug_hint,
                "risk": candidate.risk, "reversible": candidate.reversible,
            }
            for candidate in frame.candidates
        ],
    }


def rebuild_identity_frame(payload: dict) -> DecisionFrame:
    """Rebuild a persisted pre-narration frame for commit-time revalidation.

    The rebuilt frame carries the ORIGINAL frame ID and state revision so
    :func:`create_entity_after_resolution` fails closed (stale) when fresh
    identity state drifted since the pre-narration decision. No model call.
    """
    stored = dict(payload or {})
    return DecisionFrame(
        decision_class=IDENTITY_DECISION_CLASS,
        question_id=IDENTITY_QUESTION_ID,
        instructions=IDENTITY_FRAME_INSTRUCTIONS,
        state=stored.get("state"),
        state_revision=stored.get("state_revision"),
        candidates=tuple(
            CandidateRecord(
                id=str(item.get("id")), label=str(item.get("label")),
                source=str(item.get("source")),
                source_ref=item.get("source_ref"),
                payload_ref=item.get("payload_ref"),
                debug_hint=item.get("debug_hint"),
                risk=item.get("risk") or "standard",
                reversible=bool(item.get("reversible", True)),
            )
            for item in stored.get("candidates") or []
        ),
        frame_id=str(stored.get("frame_id") or ""),
    )


# ── JIT promotion from a committed structured turn ──────────────────────────

def stable_jit_key(attempt_id: uuid.UUID, temp_id: str) -> str:
    return f"jit:{attempt_id}:{str(temp_id).strip()}"[:128]


def _extract_identity_proposals(source: Any) -> list[dict]:
    """Normalize ``new_entities`` proposals from a snapshot dict, contract model, or raw list."""
    if source is None:
        return []
    if isinstance(source, dict):
        raw_list = source.get("new_entities") or []
    elif isinstance(source, (list, tuple)):
        raw_list = list(source)
    else:
        raw_list = getattr(source, "new_entities", None) or []
    proposals: list[dict] = []
    for raw in raw_list:
        if isinstance(raw, dict):
            proposals.append({
                "temp_id": str(raw.get("temp_id") or "").strip(),
                "kind": str(raw.get("kind") or "npc").strip().lower() or "npc",
                "public_name": raw.get("public_name"),
                "public_summary": raw.get("public_summary"),
                "role": raw.get("role"),
                "location_ref": raw.get("location_ref"),
            })
        else:
            proposals.append({
                "temp_id": str(getattr(raw, "temp_id", "") or "").strip(),
                "kind": str(getattr(raw, "kind", "npc") or "npc").strip().lower(),
                "public_name": getattr(raw, "public_name", None),
                "public_summary": getattr(raw, "public_summary", None),
                "role": getattr(raw, "role", None),
                "location_ref": getattr(raw, "location_ref", None),
            })
    return proposals


def _location_value(location_ref: Any) -> Any:
    if isinstance(location_ref, dict) or location_ref is None:
        return location_ref
    if hasattr(location_ref, "model_dump"):
        return location_ref.model_dump(mode="json")
    return location_ref


def _resolve_identity_proposal(
    db: Session,
    campaign: Campaign,
    *,
    temp_id: str,
    kind: str,
    public_name: Any,
    location_ref: Any,
    turn_id: Any,
    attempt_id: Any,
    identity_decision_service: Any | None = None,
    identity_session_factory: Any | None = None,
    identity_telemetry_outbox: list | None = None,
    prior_deferrals: int = 0,
) -> tuple[Any | None, str, WorldEntity | None, Any | None, bool]:
    """Run deterministic + bounded identity resolution for one proposal.

    No durable entity write happens here. Returns ``(frame, selected_id,
    reused_entity, decision_service, runner_up_applied)`` where
    ``reused_entity`` is set for deterministic stable UUID/alias hits
    (reuse with zero model calls) and ``frame``/``selected_id`` otherwise.
    Raises
    :class:`~app.world.identity.IdentityDeferredError` fail-closed
    (no insert) on ``DEFER`` or ``ValueError`` on plain ``NEW_ENTITY``
    against an exact canonical collision. The returned service is the
    (lazily constructed) service to reuse for subsequent proposals.

    ``prior_deferrals`` counts earlier DEFER memos for the same proposal
    in the retry chain; when positive, a repeated near-tie DEFER falls
    back to the model's own runner-up (flagged) instead of looping.
    """
    collision, match_kind = exact_identity_match(db, campaign.id, public_name)
    if collision is not None and match_kind in {"uuid", "alias"}:
        # Deterministic stable ref: the proposal IS the existing
        # canonical entity. Reuse it with zero model calls — aliases
        # can never reach KEEP_DISTINCT creation, so the frame has
        # nothing legal left to decide.
        return None, str(collision.id), collision, identity_decision_service, False
    location_value = _location_value(location_ref)
    frame = build_identity_frame(
        db, campaign, name=validate_entity_name(public_name), entity_type=kind,
        location_ref=str(location_value) if location_value is not None else None,
        provenance_refs=tuple(filter(None, (str(turn_id) if turn_id else None,
                                            str(attempt_id) if attempt_id else None))),
        is_authority=True,
    )
    if collision is not None and str(collision.id) not in {c.id for c in frame.candidates}:
        # Exact stable hit must stay a bounded candidate even when fuzzy
        # scoring misses it (e.g. alias/UUID reference). The model may
        # still only choose among supplied candidates. Same enriched
        # label as framed candidates so the hit stays distinguishable.
        _collision_aliases = [
            a.alias for a in db.execute(select(WorldEntityAlias).where(
                WorldEntityAlias.campaign_id == campaign.id,
                WorldEntityAlias.entity_id == collision.id,
            )).scalars()
        ]
        extra = CandidateRecord(
            id=str(collision.id),
            label=_candidate_label(collision, aliases=_collision_aliases),
            source="world:identity_search",
            payload_ref=str(collision.id),
        )
        frame = replace(frame, candidates=(extra, *frame.candidates))
    domain_candidates = {
        candidate.id for candidate in frame.candidates
        if candidate.id not in {NEW_ENTITY, KEEP_DISTINCT, DEFER}
    }
    if domain_candidates:
        if identity_decision_service is None:
            identity_decision_service = DecisionService()
        decision = decide_identity(
            db, campaign, frame, identity_decision_service,
            session_factory=identity_session_factory,
            record_outbox=identity_telemetry_outbox,
            prior_deferrals=prior_deferrals,
        )
        selected_id = decision.selected_id
        runner_up_applied = decision.runner_up_applied
    else:
        # Exhaustive deterministic search found no plausible identity.
        # NEW_ENTITY is therefore an explicit code-owned bounded outcome,
        # still revalidated against the frame immediately before insert.
        selected_id = NEW_ENTITY
        runner_up_applied = False
    if selected_id == DEFER:
        candidate_labels = [
            str(candidate.label)
            for candidate in frame.candidates
            if candidate.id not in {NEW_ENTITY, KEEP_DISTINCT, DEFER}
        ][:8]
        raise IdentityDeferredError(
            temp_id,
            proposal={
                "kind": kind,
                "public_name": public_name if isinstance(public_name, str) else str(public_name or ""),
                "location_ref": location_value,
            },
            candidate_labels=candidate_labels,
        )
    if collision is not None and selected_id == NEW_ENTITY:
        # Exact canonical collision: only policy-approved KEEP_DISTINCT
        # (same-name distinct entity) or reuse of the canonical entity
        # may proceed. Plain NEW_ENTITY fails closed with no insert.
        raise ValueError(
            f"new entity {temp_id!r} collides with canonical identity {collision.id}"
        )
    return frame, selected_id, None, identity_decision_service, runner_up_applied


def stored_identity_outcomes(attempt: Any) -> dict:
    stored = getattr(attempt, "identity_resolutions", None) or []
    outcomes: dict = {}
    for item in stored:
        if isinstance(item, dict) and item.get("temp_id"):
            outcomes[str(item["temp_id"])] = item
    return outcomes


def _apply_stored_identity_outcome(
    db: Session,
    campaign: Campaign,
    *,
    proposal: dict,
    outcome: dict,
    jit_key: str,
    turn_id: Any,
    attempt_id: Any,
    operation_id: Any,
) -> WorldEntity:
    """Apply one pre-narration identity outcome against fresh commit-time state.

    Revalidates deterministically in code; never makes a model call. Stale
    or illegal outcomes fail closed with no insert.
    """
    temp_id = proposal["temp_id"]
    selected_id = str(outcome.get("outcome") or "")
    if not selected_id or selected_id == DEFER:
        raise IdentityDeferredError(temp_id, proposal={
            "kind": proposal.get("kind"),
            "public_name": proposal.get("public_name"),
            "location_ref": _location_value(proposal.get("location_ref")),
        })
    if outcome.get("frame"):
        # Bounded NEW_ENTITY / KEEP_DISTINCT (or reuse-by-selection):
        # rebuild the original frame so revision drift fails closed as
        # stale, then apply with no second decision call.
        frame = rebuild_identity_frame(outcome["frame"])
        entity, _ = create_entity_after_resolution(
            db, campaign, frame, selected_id,
            entity_type=proposal["kind"],
            name=validate_entity_name(proposal["public_name"]),
            idempotency_key=jit_key,
            summary=str(proposal["public_summary"])[:2000] if proposal["public_summary"] else None,
            source_turn_id=turn_id,
            source_attempt_id=attempt_id,
            operation_id=str(operation_id) if operation_id else None,
            details={
                "temp_id": temp_id, "role": proposal["role"],
                "location_ref": _location_value(proposal["location_ref"]),
                "promoted_from": "dm_turn_contract",
            },
        )
        return entity
    # Deterministic stable reuse recorded pre-narration: the same public
    # name must still resolve to the same live canonical entity, else the
    # alias moved (or the entity was archived) and reuse fails closed.
    try:
        expected_id = uuid.UUID(selected_id)
    except ValueError:
        raise ValueError(f"stored identity outcome for new entity {temp_id!r} is not a canonical identity")
    current, _ = exact_identity_match(db, campaign.id, proposal["public_name"])
    if current is None or current.id != expected_id or current.superseded_by_id:
        raise ValueError(
            f"stored identity outcome for new entity {temp_id!r} is no longer canonical"
        )
    return current


def _count_prior_deferrals(db: Session, attempt: Any, *, temp_id: str,
                           public_name: Any, max_levels: int = 5) -> int:
    """Count DEFER memos for the same proposal up the abandoned-retry chain.

    Never raises: unreadable ancestry means zero, never a blocked turn.
    """
    count = 0
    current = attempt
    try:
        for _ in range(max_levels):
            parent_id = getattr(current, "parent_attempt_id", None)
            if not parent_id:
                break
            parent = db.get(DmTurnAttempt, parent_id)
            if parent is None:
                break
            for item in getattr(parent, "identity_resolutions", None) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("outcome") != DEFER or item.get("via") != "deferred":
                    continue
                if str(item.get("temp_id") or "") != str(temp_id or ""):
                    continue
                proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
                if str(proposal.get("public_name") or "") != str(public_name or ""):
                    continue
                count += 1
            if getattr(parent, "status", None) != "abandoned":
                break
            current = parent
    except Exception:
        return count
    return count


def resolve_new_entity_identities_pre_narration(
    db: Session,
    campaign: Campaign,
    turn: Any,
    attempt: Any,
    contract: Any,
    *,
    identity_decision_service: Any | None = None,
    identity_session_factory: Any | None = None,
) -> list[dict]:
    """Resolve ``new_entities`` identity after normalization, before first visibility.

    Runs the same deterministic + bounded resolution the commit path
    applies, but pre-narration: ambiguous proposals ``DEFER`` here — before
    any #197 chunk can persist — instead of stranding visible narration
    that the later commit then refuses. Selected attempt-local outcomes
    persist on the attempt row (all-or-nothing for resolutions;
    a ``DEFER`` memo persists alone so explicit-retry re-adjudication can
    disambiguate instead of looping); commit-time
    :func:`promote_new_entities_from_contract` only revalidates/applies
    them against fresh state with no second model call. Reuse of an
    existing canonical entity raises before narration so the adjudicator
    can rewrite the contract with that identity.

    No campaign lock is held here, so fail-soft telemetry may write on its
    own session via ``identity_session_factory``. Commits the attempt
    update; pre-visibility failure stays freely retryable.
    """
    proposals = _extract_identity_proposals(contract)
    if not proposals:
        return []
    if len(proposals) > 8:
        raise ValueError("new_entities proposals exceed bound of 8")
    turn_id = getattr(turn, "id", None)
    attempt_id = getattr(attempt, "id", None)
    service = identity_decision_service
    outcomes: list[dict] = []
    for proposal in proposals:
        temp_id = proposal["temp_id"]
        if not temp_id:
            raise ValueError("new_entities proposal missing temp_id")
        jit_key = stable_jit_key(attempt_id, temp_id)
        existing_retry = find_entity_by_idempotency(db, campaign.id, jit_key)
        if existing_retry is not None:
            outcomes.append({
                "temp_id": temp_id, "outcome": str(existing_retry.id),
                "via": "idempotent_reuse", "jit_key": jit_key,
            })
            continue
        try:
            prior_deferrals = _count_prior_deferrals(
                db, attempt, temp_id=temp_id, public_name=proposal["public_name"])
            frame, selected_id, reused, service, runner_up_applied = _resolve_identity_proposal(
                db, campaign, temp_id=temp_id, kind=proposal["kind"],
                public_name=proposal["public_name"], location_ref=proposal["location_ref"],
                turn_id=turn_id, attempt_id=attempt_id,
                identity_decision_service=service,
                identity_session_factory=identity_session_factory,
                identity_telemetry_outbox=None,
                prior_deferrals=prior_deferrals,
            )
        except IdentityDeferredError as exc:
            # Fail closed (no insert) but persist a deferral memo on the
            # attempt before raising: explicit-retry re-adjudication reads
            # it to disambiguate the proposal instead of replaying the
            # identical frame (deterministic adjudication + near-tie DEFER
            # would otherwise loop forever). Memo content is DM-prompt-safe
            # (public proposal fields + canonical candidate labels only).
            outcomes.append({
                "temp_id": temp_id, "outcome": DEFER,
                "via": "deferred", "jit_key": jit_key,
                "proposal": {
                    "kind": proposal.get("kind"),
                    "public_name": proposal.get("public_name"),
                    "role": proposal.get("role"),
                    "public_summary": (str(proposal.get("public_summary") or "")[:500] or None),
                },
                "candidate_labels": list(getattr(exc, "candidate_labels", []) or [])[:8],
            })
            attempt.identity_resolutions = outcomes
            db.flush()
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            raise
        # A contract that proposes an introduction cannot be narrated as-is
        # when identity resolution chooses an existing person. Re-adjudication
        # must rewrite the beats against the canonical ref before visibility.
        if reused is not None or selected_id not in {NEW_ENTITY, KEEP_DISTINCT}:
            canonical = reused or db.get(WorldEntity, uuid.UUID(selected_id))
            if canonical is None or canonical.campaign_id != campaign.id or canonical.superseded_by_id:
                raise ValueError("resolved canonical entity is no longer live")
            raise IdentityReuseRequiresReadjudication(
                temp_id=temp_id,
                proposed_name=str(proposal["public_name"]),
                entity=canonical,
            )
        outcome_record = {
            "temp_id": temp_id, "outcome": selected_id,
            "via": "bounded_decision" if frame is not None and any(
                c.id not in {"NEW_ENTITY", "KEEP_DISTINCT", "DEFER"}
                for c in frame.candidates
            ) else "deterministic_no_candidate",
            "jit_key": jit_key,
            "frame": serialize_identity_frame(frame) if frame is not None else None,
        }
        if runner_up_applied:
            outcome_record["runner_up_fallback"] = True
        outcomes.append(outcome_record)
    attempt.identity_resolutions = outcomes
    db.flush()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return outcomes


def promote_new_entities_from_contract(
    db: Session,
    campaign: Campaign,
    turn: Any,
    attempt: Any,
    *,
    identity_decision_service: Any | None = None,
    identity_session_factory: Any | None = None,
    identity_telemetry_outbox: list | None = None,
) -> list[WorldEntity]:
    """Promote ``new_entities`` proposals to durable canonical identity.

    Called inside the turn-commit revision transaction (no commit here), so
    promotion is transactional with its source turn: failed commit leaves no
    half-created authority. Each proposal gets a stable idempotency key per
    (attempt, temp_id) → committed exactly once; duplicate retry returns the
    existing row.

    Identity itself is decided pre-narration by
    :func:`resolve_new_entity_identities_pre_narration` (after contract
    normalization, before the first visible chunk) and persisted
    attempt-local. This function only revalidates those stored outcomes
    against fresh state and applies them — no second model call. Proposals
    with no stored outcome (direct commit callers that skipped the
    pre-narration step) fall back to the same inline bounded resolution.

    Telemetry never opens an independent session while the campaign lock is
    held: pass ``identity_telemetry_outbox`` to collect decision records
    for a post-commit flush. ``identity_session_factory`` remains only for
    unlocked callers; locked paths must leave it None.
    """
    proposals = _extract_identity_proposals(getattr(attempt, "contract_snapshot", None))
    if not proposals:
        return []
    if len(proposals) > 8:
        raise ValueError("new_entities proposals exceed bound of 8")
    stored = stored_identity_outcomes(attempt)

    promoted: list[WorldEntity] = []
    turn_id = getattr(turn, "id", None)
    attempt_id = getattr(attempt, "id", None)
    operation_id = getattr(attempt, "commit_operation_id", None) or (str(attempt_id) if attempt_id else None)
    service = identity_decision_service

    for proposal in proposals:
        temp_id = proposal["temp_id"]
        if not temp_id:
            raise ValueError("new_entities proposal missing temp_id")
        jit_key = stable_jit_key(attempt_id, temp_id)
        existing_retry = find_entity_by_idempotency(db, campaign.id, jit_key)
        if existing_retry is not None:
            promoted.append(existing_retry)
            continue
        outcome = stored.get(temp_id)
        if outcome is not None:
            # Pre-narration decision: revalidate against fresh state and
            # apply with no second model call.
            promoted.append(_apply_stored_identity_outcome(
                db, campaign, proposal=proposal, outcome=outcome,
                jit_key=jit_key, turn_id=turn_id, attempt_id=attempt_id,
                operation_id=operation_id,
            ))
            continue
        # Fallback for direct commit callers without a pre-narration step.
        frame, selected_id, reused, service, _runner_up = _resolve_identity_proposal(
            db, campaign, temp_id=temp_id, kind=proposal["kind"],
            public_name=proposal["public_name"], location_ref=proposal["location_ref"],
            turn_id=turn_id, attempt_id=attempt_id,
            identity_decision_service=service,
            identity_session_factory=identity_session_factory,
            identity_telemetry_outbox=identity_telemetry_outbox,
        )
        if reused is not None:
            promoted.append(reused)
            continue
        entity, _ = create_entity_after_resolution(
            db, campaign, frame, selected_id,
            entity_type=proposal["kind"],
            name=validate_entity_name(proposal["public_name"]),
            idempotency_key=jit_key,
            summary=str(proposal["public_summary"])[:2000] if proposal["public_summary"] else None,
            source_turn_id=turn_id,
            source_attempt_id=attempt_id,
            operation_id=str(operation_id) if operation_id else None,
            details={
                "temp_id": temp_id, "role": proposal["role"],
                "location_ref": _location_value(proposal["location_ref"]),
                "promoted_from": "dm_turn_contract",
            },
        )
        promoted.append(entity)
    return promoted


def register_promoted_npcs_in_scene(
    db: Session,
    campaign: Campaign,
    promoted: list[WorldEntity],
    *,
    attempt: Any,
    turn: Any = None,
) -> int:
    """Make freshly introduced NPCs present in the scene with baseline knowledge (#459).

    Introduction means present by construction: each promoted NPC gains an
    ``entity_id``-bearing ``present_actors`` entry (deduped by entity id; a
    name-only or ``temp_id`` entry the model wrote for the same NPC in a
    same-turn ``update_scene`` is replaced, not duplicated), and receives
    baseline knowledge of the scene location and the PCs present, so the
    next packet's knowledge lane covers it without perspective repair.
    Runs inside the turn-commit revision transaction (no commit). No scene
    row yet means no scene is established, so nothing is registered. Returns
    the number of NPCs newly added to ``present_actors``.
    """
    npcs = [e for e in promoted if getattr(e, "entity_type", None) == "npc"]
    scene = db.get(CampaignCurrentScene, campaign.id) if npcs else None
    if scene is None:
        return 0
    actors = [dict(a) for a in (scene.present_actors or []) if isinstance(a, dict)]
    attempt_id = getattr(attempt, "id", None)
    operation_id = getattr(attempt, "commit_operation_id", None) or (str(attempt_id) if attempt_id else None)
    turn_id = getattr(turn, "id", None)

    # Baseline knowledge targets: scene location + PCs already present.
    targets: list[uuid.UUID] = []
    if scene.location_entity_id:
        targets.append(scene.location_entity_id)
    for actor in actors:
        if actor.get("kind") != "pc":
            continue
        ref = actor.get("entity_id") or actor.get("name")
        pc, _ = exact_identity_match(db, campaign.id, ref)
        if pc is not None and pc.entity_type == "character" and pc.id not in targets:
            targets.append(pc.id)

    added = 0
    for entity in npcs:
        details = entity.details or {}
        temp_id = str(details.get("temp_id") or "").strip()
        eid = str(entity.id)
        entry = {"entity_id": eid, "name": entity.name, "kind": "npc"}
        if details.get("role"):
            entry["role"] = str(details["role"])[:160]
        aliases = {normalize_alias(entity.name)}
        if temp_id:
            aliases.add(normalize_alias(temp_id))
        replaced = False
        deduped: list[dict] = []
        for actor in actors:
            by_id = str(actor.get("entity_id") or "") == eid
            by_name = not actor.get("entity_id") and normalize_alias(str(actor.get("name") or "")) in aliases
            if not (by_id or by_name):
                deduped.append(actor)
            elif not replaced:
                replaced = True
                deduped.append(entry)
        actors = deduped
        if not replaced:
            actors.append(entry)
            added += 1
        jit_key = stable_jit_key(attempt_id, temp_id or eid)
        for target_id in targets:
            assert_knowledge(
                db, campaign, subject_kind="npc", subject_entity_id=entity.id,
                target_kind="entity", target_entity_id=target_id,
                knowledge_state="knows", acquisition_source="co_presence",
                visibility="dm_only",
                provenance={"source": "npc_introduction", "temp_id": temp_id or None},
                source_turn_id=turn_id, source_attempt_id=attempt_id,
                operation_id=operation_id,
                idempotency_key=compose_operation_id(jit_key, "know", target_id),
            )
    apply_scene_update(
        db, campaign, new_revision=int(campaign.revision or 0) + 1,
        present_actors=actors,
        source_turn_id=turn_id, source_attempt_id=attempt_id,
        operation_id=operation_id,
    )
    return added
