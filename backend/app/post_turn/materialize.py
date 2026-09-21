"""Post-turn memory/materialization — issue #217.

One explicit materializer converts a committed event/turn range into a
validated durable write patch covering entities, relations, facts, NPC
state, knowledge, and visibility. Under #379, generation proposes,
bounded decisions verify, deterministic code authorizes:

- generation (an injected candidate provider over approved roles) may
  propose novel names/descriptions/fact text only where the range cannot
  be compiled mechanically;
- bounded decisions judge whether a non-mechanical candidate assertion
  is SUPPORTED by the supplied committed evidence (SUPPORTED /
  UNSUPPORTED / DEFER). They can reject or defer, never authorize;
- deterministic code owns provenance/source existence, schema,
  visibility, identity outcome, revision/idempotency, and the apply —
  a positive semantic verdict never overrides a deterministic failure.

Committed visible reality is non-negotiable input: the compiler reads
events, never rewrites them. Invalid compiler output fails the run
before checkpoint advancement; unsupported/uncertain assertions stay
unapplied and recorded (deferred/rejected) while the range still
consumes, so one ambiguous candidate never wedges post-turn
consolidation (same posture as #218 clock DEFER).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.decisions import (
    ACTIVE,
    ESCALATE,
    CandidateRecord,
    ChoiceResult,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    build_frame,
    build_record,
    evaluate_execution,
    is_escape_id,
    record_fail_soft,
    register_policy,
    to_decision_request,
)
from app.observability.tracing import structured_log
from models.campaigns import Campaign, CampaignDomainEvent

logger = logging.getLogger(__name__)

MATERIALIZE_CONTRACT_VERSION = 1

# Committed-domain-event payload key carrying compiler hints. Turn-commit
# code (or future forward-DM work) populates these; events without hints
# contribute provenance context only. Hint schema is validated strictly —
# malformed mechanical hints fail the run, never guess into canon.
MATERIALIZE_HINT_KEY = "post_turn_materialize"

WRITE_CATEGORIES = ("entities", "relations", "facts", "npc_state")

# Bounded verification vocabulary for one candidate assertion.
SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
DEFER = "DEFER"

MATERIALIZE_DECISION_CLASS = "post_turn_materialize"
MATERIALIZE_QUESTION_ID = "verify_materialization_assertion"
MATERIALIZE_FRAME_INSTRUCTIONS = (
    "Select only the supplied outcome that matches the committed evidence. "
    "SUPPORTED requires the candidate assertion to follow directly from the "
    "supplied evidence; paraphrase still counts when the meaning matches. "
    "When evidence is insufficient or ambiguous, defer. Never invent facts "
    "outside the supplied evidence."
)
MATERIALIZE_FRAME_SCHEMA_VERSION = 1

register_policy(DecisionClassPolicy(
    decision_class=MATERIALIZE_DECISION_CLASS,
    min_probability_direct=.85, min_confidence_direct=.8,
    min_margin_direct=.25, near_tie_margin=.25,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))


class MaterializeError(ValueError):
    """Deterministic materialization failure — fails the run, checkpoint stays."""


# ── Candidate assertions ─────────────────────────────────────────────────

@dataclass
class CandidateAssertion:
    """One proposed durable write with provenance back to committed events."""

    category: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)
    visibility: str = "dm_only"
    epistemic_state: str = "claimed"
    mechanical: bool = True
    source_event_id: uuid.UUID | None = None
    source_sequence: int | None = None


def _require_str(mapping: dict, field_name: str, *, what: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MaterializeError(f"{what} is missing required string field {field_name!r}")
    return value.strip()


def validate_hint(hint: Any, *, event_sequence: int) -> CandidateAssertion:
    """Validate one raw compiler hint into a CandidateAssertion (fail closed)."""
    if not isinstance(hint, dict):
        raise MaterializeError(f"event {event_sequence}: materialize hint must be an object")
    category = hint.get("category")
    if category not in WRITE_CATEGORIES:
        raise MaterializeError(
            f"event {event_sequence}: unknown write category {category!r}; "
            f"expected one of {list(WRITE_CATEGORIES)}"
        )
    key = _require_str(hint, "key", what=f"event {event_sequence} {category} hint")
    if len(key) > 128:
        raise MaterializeError(f"event {event_sequence}: hint key must be 128 characters or fewer")
    data = hint.get("data", {})
    if not isinstance(data, dict):
        raise MaterializeError(f"event {event_sequence}: hint {key!r} data must be an object")
    visibility = hint.get("visibility", "dm_only")
    try:
        from app.world.service import normalize_visibility
        visibility = normalize_visibility(visibility)
    except ValueError as exc:
        raise MaterializeError(f"event {event_sequence}: hint {key!r} has invalid visibility: {exc}") from exc
    epistemic_state = hint.get("epistemic_state", "claimed")
    if category in ("relations", "facts"):
        from app.world.knowledge import validate_epistemic_state
        try:
            epistemic_state = validate_epistemic_state(epistemic_state)
        except ValueError as exc:
            raise MaterializeError(
                f"event {event_sequence}: hint {key!r} has invalid epistemic_state: {exc}"
            ) from exc
    mechanical = hint.get("mechanical", True)
    if not isinstance(mechanical, bool):
        raise MaterializeError(f"event {event_sequence}: hint {key!r} mechanical must be a boolean")
    return CandidateAssertion(
        category=category, key=key, data=dict(data),
        visibility=visibility, epistemic_state=epistemic_state,
        mechanical=mechanical,
    )


def extract_candidates(events: list[CampaignDomainEvent]) -> list[CandidateAssertion]:
    """Deterministically compile committed events into candidate assertions.

    Events without hints contribute nothing — committed reality is input,
    never rewritten. Malformed hints raise MaterializeError (run fails).
    """
    candidates: list[CandidateAssertion] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        payload = event.payload or {}
        hints = payload.get(MATERIALIZE_HINT_KEY, [])
        if hints is None:
            continue
        if not isinstance(hints, list):
            raise MaterializeError(
                f"event {event.sequence}: {MATERIALIZE_HINT_KEY!r} must be a list"
            )
        for hint in hints:
            assertion = validate_hint(hint, event_sequence=event.sequence)
            assertion.source_event_id = event.id
            assertion.source_sequence = event.sequence
            if (assertion.category, assertion.key) in seen:
                raise MaterializeError(
                    f"event {event.sequence}: duplicate hint key "
                    f"{assertion.category}/{assertion.key} in range"
                )
            seen.add((assertion.category, assertion.key))
            candidates.append(assertion)
    return candidates


# ── Reference resolution ─────────────────────────────────────────────────

def resolve_entity_ref(db: Session, campaign_id: uuid.UUID, ref: Any):
    """Resolve a stable entity reference deterministically (no model calls).

    Returns (entity, how) via exact uuid/alias/name match, or (None, None).
    Name collisions are returned with how="name" for the bounded path.
    """
    from app.world.identity import exact_identity_match
    return exact_identity_match(db, campaign_id, ref)


def _resolve_required_ref(
    db: Session, campaign_id: uuid.UUID, ref: Any, *, assertion: CandidateAssertion, role: str,
):
    entity, _ = resolve_entity_ref(db, campaign_id, ref)
    if entity is None:
        if assertion.mechanical:
            raise MaterializeError(
                f"{assertion.category}/{assertion.key}: {role} ref {ref!r} "
                f"matches no canonical entity (source seq {assertion.source_sequence})"
            )
        return None
    return entity


# ── Bounded verification ─────────────────────────────────────────────────

def _evidence_excerpt(events: list[CampaignDomainEvent]) -> list[dict[str, Any]]:
    """Committed evidence for the DM-internal verification role (truncated)."""
    out: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload or {}
        text = str(payload)[:2000]
        out.append({
            "sequence": event.sequence,
            "event_type": event.event_type,
            "visibility": event.visibility,
            "event_id": str(event.id),
            "payload_excerpt": text,
        })
    return out


def build_verification_frame(
    assertion: CandidateAssertion, events: list[CampaignDomainEvent],
    *, from_sequence: int, to_sequence: int,
) -> DecisionFrame:
    records = (
        CandidateRecord(
            id=SUPPORTED, label="Assertion is supported by the committed evidence",
            source="post-turn:materialize_outcome",
        ),
        CandidateRecord(
            id=UNSUPPORTED, label="Assertion is not supported by the committed evidence",
            source="post-turn:materialize_outcome",
        ),
        CandidateRecord(
            id=DEFER, label="Evidence is ambiguous; defer the assertion",
            source="post-turn:materialize_outcome", risk="low",
        ),
    )
    return build_frame(
        decision_class=MATERIALIZE_DECISION_CLASS,
        question_id=MATERIALIZE_QUESTION_ID,
        instructions=MATERIALIZE_FRAME_INSTRUCTIONS,
        state={
            "frame_schema": MATERIALIZE_FRAME_SCHEMA_VERSION,
            "candidate": {
                "category": assertion.category,
                "key": assertion.key,
                "visibility": assertion.visibility,
                "epistemic_state": assertion.epistemic_state,
                "data": assertion.data,
                "source_sequence": assertion.source_sequence,
            },
            "evidence": _evidence_excerpt(events),
            "source_range": [from_sequence, to_sequence],
        },
        state_revision=f"post-turn:{from_sequence}-{to_sequence}",
        candidates=records,
        include_escapes=False,
    )


@dataclass
class VerificationDecision:
    selected_id: str
    failure: str | None = None
    record: Any = None
    provider: str | None = None
    model: str | None = None


def decide_assertion(
    frame: DecisionFrame, service: DecisionService, *, session_factory: Any = None,
) -> VerificationDecision:
    """Run one bounded verification judgment (mirrors #218 decide_clock).

    Decision-plane failures, unknown candidates, and policy escalation all
    resolve to recorded DEFER — never guessed canon.
    """
    try:
        response = service.decide(to_decision_request(frame))
    except Exception as exc:
        logger.warning("materialize verification failed error=%s", exc)
        return VerificationDecision(DEFER, failure=f"{type(exc).__name__}: {exc}"[:500])
    result = response.results.get(frame.question_id)
    if not isinstance(result, ChoiceResult):
        logger.warning("materialize verification missing choice result")
        return VerificationDecision(
            DEFER, failure="malformed: missing choice result",
            provider=response.provider, model=response.model,
        )
    if is_escape_id(result.selected_id) or result.selected_id == DEFER:
        record = build_record(
            frame, result, evaluate_execution(
                frame, result.selected_id, dict(result.probabilities),
                result.confidence, verified=True,
            ),
            provider=response.provider, model=response.model or "unknown",
            mode=ACTIVE, trace_id=response.trace_id,
            operation_id=response.operation_id, campaign_id=None,
            latency_ms=response.latency_ms, verified=True,
        )
        record_fail_soft(session_factory, record)
        return VerificationDecision(
            DEFER, record=record, provider=response.provider, model=response.model,
        )
    try:
        if result.selected_id not in {c.id for c in frame.candidates}:
            raise ValueError(f"unknown candidate {result.selected_id!r}")
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True,
        )
    except Exception as exc:
        logger.warning("materialize verification policy failed error=%s", exc)
        return VerificationDecision(
            DEFER, failure=f"{type(exc).__name__}: {exc}"[:500],
            provider=response.provider, model=response.model,
        )
    record = build_record(
        frame, result, verdict, provider=response.provider,
        model=response.model or "unknown", mode=ACTIVE,
        trace_id=response.trace_id, operation_id=response.operation_id,
        campaign_id=None, latency_ms=response.latency_ms, verified=True,
    )
    record_fail_soft(session_factory, record)
    if verdict.directive == ESCALATE or result.selected_id != SUPPORTED:
        return VerificationDecision(
            DEFER if verdict.directive == ESCALATE else result.selected_id,
            record=record, provider=response.provider, model=response.model,
        )
    return VerificationDecision(
        SUPPORTED, record=record, provider=response.provider, model=response.model,
    )


# ── Apply ────────────────────────────────────────────────────────────────

def _idempotency_key(
    campaign_id: uuid.UUID, from_sequence: int, to_sequence: int,
    assertion: CandidateAssertion,
) -> str:
    return f"pt217:{campaign_id}:{from_sequence}-{to_sequence}:{assertion.category}:{assertion.key}"


def _apply_entity(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int,
    decision_service: DecisionService | None, session_factory: Any,
    verification_tally: dict[str, int],
) -> dict[str, Any]:
    """Apply one entity assertion: reuse exact identity, bounded-resolve
    collisions via #214, or create. Ambiguity without a decision service
    defers fail-closed."""
    from app.world.identity import (
        DEFER as IDENTITY_DEFER,
        KEEP_DISTINCT,
        NEW_ENTITY,
        build_identity_frame,
        create_entity_after_resolution,
        decide_identity,
        exact_identity,
    )
    from app.world.service import create_entity_inline, validate_entity_status, validate_entity_type

    data = assertion.data
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MaterializeError(f"entities/{assertion.key}: name is required")
    entity_type = data.get("entity_type", "object")
    try:
        validate_entity_type(entity_type)
    except ValueError as exc:
        raise MaterializeError(f"entities/{assertion.key}: {exc}") from exc
    status = data.get("status", "active")
    try:
        validate_entity_status(status)
    except ValueError as exc:
        raise MaterializeError(f"entities/{assertion.key}: {exc}") from exc
    key = _idempotency_key(campaign.id, from_sequence, to_sequence, assertion)
    ref = data.get("ref", name)

    entity, how = resolve_entity_ref(db, campaign.id, ref)
    if entity is not None and how in ("uuid", "alias"):
        return {"outcome": "resolved_existing", "entity_id": str(entity.id), "via": how}
    if entity is None:
        collision = exact_identity(db, campaign.id, name)
        if collision is None:
            created, is_new = create_entity_inline(
                db, campaign, entity_type=str(entity_type).strip().lower(),
                name=name.strip(), summary=data.get("summary"),
                status=status, visibility=assertion.visibility,
                details=data.get("details") if isinstance(data.get("details"), dict) else {},
                operation_id=f"post-turn-217:{from_sequence}-{to_sequence}",
                idempotency_key=key,
            )
            return {
                "outcome": "applied" if is_new else "duplicate",
                "entity_id": str(created.id), "via": "created",
            }
    # Ambiguous: exact canonical-name collision — route through #214.
    frame = build_identity_frame(
        db, campaign, name=name.strip(),
        entity_type=str(entity_type).strip().lower(),
        provenance_refs=(str(assertion.source_event_id),) if assertion.source_event_id else (),
    )
    if decision_service is None:
        return {"outcome": "deferred", "reason": "duplicate_identity_no_decision_service"}
    decision = decide_identity(db, campaign, frame, decision_service, session_factory=session_factory)
    verification_tally["identity_decisions"] += 1
    if decision.selected_id == IDENTITY_DEFER:
        return {"outcome": "deferred", "reason": "duplicate_identity_ambiguous"}
    if decision.selected_id not in {NEW_ENTITY, KEEP_DISTINCT}:
        return {"outcome": "resolved_existing", "entity_id": str(decision.selected_id), "via": "identity_decision"}
    try:
        created, is_new = create_entity_after_resolution(
            db, campaign, frame, decision.selected_id,
            entity_type=str(entity_type).strip().lower(), name=name.strip(),
            idempotency_key=key, details=data.get("details"),
            summary=data.get("summary"), operation_id=f"post-turn-217:{from_sequence}-{to_sequence}",
        )
    except ValueError as exc:
        return {"outcome": "deferred", "reason": f"identity_resolution_rejected: {exc}"}
    return {"outcome": "applied" if is_new else "duplicate", "entity_id": str(created.id), "via": "identity_decision"}


def _apply_relation(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int, operation_id: str | None,
) -> dict[str, Any]:
    from app.world.knowledge import create_relation_inline, validate_relation_type

    data = assertion.data
    relation_type = data.get("relation_type")
    if not isinstance(relation_type, str) or not relation_type.strip():
        raise MaterializeError(f"relations/{assertion.key}: relation_type is required")
    try:
        validate_relation_type(relation_type)
    except ValueError as exc:
        raise MaterializeError(f"relations/{assertion.key}: {exc}") from exc
    subject = _resolve_required_ref(db, campaign.id, data.get("subject_ref"), assertion=assertion, role="subject")
    if subject is None:
        return {"outcome": "deferred", "reason": "unresolvable_subject_ref"}
    obj = None
    object_label = data.get("object_label")
    if data.get("object_ref") is not None:
        obj = _resolve_required_ref(db, campaign.id, data.get("object_ref"), assertion=assertion, role="object")
        if obj is None:
            return {"outcome": "deferred", "reason": "unresolvable_object_ref"}
    if obj is None and not object_label:
        if assertion.mechanical:
            raise MaterializeError(
                f"relations/{assertion.key}: requires object_ref or object_label"
            )
        return {"outcome": "rejected", "reason": "missing_object"}
    row, created = create_relation_inline(
        db, campaign, subject_entity_id=subject.id,
        relation_type=relation_type.strip(),
        object_entity_id=obj.id if obj else None,
        object_label=object_label,
        epistemic_state=assertion.epistemic_state,
        visibility=assertion.visibility,
        provenance={
            "post_turn_range": [from_sequence, to_sequence],
            "source_sequence": assertion.source_sequence,
        },
        details=data.get("details") if isinstance(data.get("details"), dict) else {},
        source_event_id=assertion.source_event_id,
        operation_id=operation_id,
        idempotency_key=_idempotency_key(campaign.id, from_sequence, to_sequence, assertion),
    )
    return {"outcome": "applied" if created else "duplicate", "relation_id": str(row.id)}


def _apply_fact(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int, operation_id: str | None,
) -> dict[str, Any]:
    from app.world.knowledge import create_fact_inline, validate_fact_content

    data = assertion.data
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        if assertion.mechanical:
            raise MaterializeError(f"facts/{assertion.key}: content is required")
        return {"outcome": "rejected", "reason": "missing_content"}
    try:
        validate_fact_content(content)
    except ValueError as exc:
        if assertion.mechanical:
            raise MaterializeError(f"facts/{assertion.key}: {exc}") from exc
        return {"outcome": "rejected", "reason": f"invalid_content: {exc}"}
    entity_ids: list[str] = []
    for ref in data.get("entity_refs") or []:
        entity = _resolve_required_ref(db, campaign.id, ref, assertion=assertion, role="entity_ref")
        if entity is None:
            return {"outcome": "deferred", "reason": "unresolvable_entity_ref"}
        entity_ids.append(str(entity.id))
    row, created = create_fact_inline(
        db, campaign, content=content.strip(), entity_refs=entity_ids,
        epistemic_state=assertion.epistemic_state,
        visibility=assertion.visibility,
        provenance={
            "post_turn_range": [from_sequence, to_sequence],
            "source_sequence": assertion.source_sequence,
        },
        details=data.get("details") if isinstance(data.get("details"), dict) else {},
        source_event_id=assertion.source_event_id,
        operation_id=operation_id,
        idempotency_key=_idempotency_key(campaign.id, from_sequence, to_sequence, assertion),
    )
    return {"outcome": "applied" if created else "duplicate", "fact_id": str(row.id)}


def _apply_npc_state(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int, operation_id: str | None,
) -> dict[str, Any]:
    from app.world.npcs import apply_npc_state_inline, get_npc_state

    data = assertion.data
    entity = _resolve_required_ref(db, campaign.id, data.get("entity_ref"), assertion=assertion, role="entity_ref")
    if entity is None:
        return {"outcome": "deferred", "reason": "unresolvable_entity_ref"}
    run_stamp = f"{from_sequence}-{to_sequence}"
    existing = get_npc_state(db, campaign.id, entity.id)
    if existing is not None and (existing.provenance or {}).get("post_turn_run") == run_stamp:
        return {"outcome": "duplicate", "entity_id": str(entity.id)}
    allowed = {
        "role", "goals", "disposition", "resources", "current_activity",
        "location_entity_id", "location_name", "importance", "depth",
        "field_visibility",
    }
    updates: dict[str, Any] = {k: data[k] for k in allowed if k in data}
    location_ref = data.get("location_ref")
    if location_ref and "location_entity_id" not in updates and "location_name" not in updates:
        location, _ = resolve_entity_ref(db, campaign.id, location_ref)
        if location is not None:
            updates["location_entity_id"] = location.id
        else:
            updates["location_name"] = str(location_ref)
    row = apply_npc_state_inline(
        db, campaign, entity.id, new_revision=int(campaign.revision or 1),
        provenance={
            "post_turn_run": run_stamp,
            "source_sequence": assertion.source_sequence,
        },
        source_event_id=assertion.source_event_id,
        operation_id=operation_id,
        **updates,
    )
    return {"outcome": "applied", "entity_id": str(row.entity_id)}


# ── Range entry point ────────────────────────────────────────────────────

CandidateProvider = Callable[
    [list[CampaignDomainEvent]],
    "tuple[list[dict[str, Any]], dict[str, Any]]",
]


def materialize_range(
    db: Session,
    campaign: Campaign,
    events: list[CampaignDomainEvent],
    from_sequence: int,
    to_sequence: int,
    *,
    decision_service: DecisionService | None = None,
    candidate_provider: CandidateProvider | None = None,
    operation_id: str | None = None,
    session_factory: Any = None,
) -> dict[str, Any]:
    """Compile one committed range into validated durable writes and apply them.

    Raises MaterializeError on deterministic failure (run fails, checkpoint
    stays). Unsupported/uncertain assertions are recorded as rejected or
    deferred while the range still consumes. Flushes; never commits — the
    post-turn worker owns commit/rollback so retries converge.
    """
    service = decision_service or DecisionService()
    op_id = operation_id or f"post-turn-217:{campaign.id}:{from_sequence}-{to_sequence}"

    candidates = extract_candidates(events)
    generation_trace: dict[str, Any] = {
        "role": "deterministic-compiler", "model": None,
        "compiler_candidates": len(candidates), "generated_candidates": 0,
    }
    if candidate_provider is not None:
        try:
            extra_raw, gen_trace = candidate_provider(events)
        except Exception as exc:
            raise MaterializeError(f"candidate generation failed: {exc}") from exc
        if not isinstance(extra_raw, list):
            raise MaterializeError("candidate provider must return a list of hint objects")
        for raw in extra_raw:
            assertion = validate_hint(raw, event_sequence=from_sequence)
            assertion.mechanical = bool(raw.get("mechanical", False))
            if assertion.source_event_id is None:
                assertion.source_event_id = events[0].id if events else None
                assertion.source_sequence = events[0].sequence if events else from_sequence
            candidates.append(assertion)
        generation_trace.update({
            "role": (gen_trace or {}).get("role", "generative-candidate"),
            "model": (gen_trace or {}).get("model"),
            "generated_candidates": len(extra_raw),
        })

    verification_tally = {"decisions": 0, "supported": 0, "unsupported": 0,
                          "deferred": 0, "identity_decisions": 0}
    applied: dict[str, int] = {category: 0 for category in WRITE_CATEGORIES}
    duplicates = 0
    rejected: list[dict] = []
    deferred: list[dict] = []
    outcomes: list[dict] = []

    ordered = sorted(
        candidates,
        key=lambda a: (WRITE_CATEGORIES.index(a.category), a.key),
    )
    for assertion in ordered:
        record: dict[str, Any] = {
            "category": assertion.category, "key": assertion.key,
            "mechanical": assertion.mechanical,
            "source_sequence": assertion.source_sequence,
        }
        try:
            needs_verdict = not assertion.mechanical and assertion.category in ("relations", "facts")
            if needs_verdict:
                frame = build_verification_frame(
                    assertion, events, from_sequence=from_sequence, to_sequence=to_sequence,
                )
                verdict = decide_assertion(frame, service, session_factory=session_factory)
                verification_tally["decisions"] += 1
                record["verification"] = {
                    "selected": verdict.selected_id,
                    "provider": verdict.provider, "model": verdict.model,
                    "failure": verdict.failure,
                }
                if verdict.selected_id == UNSUPPORTED:
                    verification_tally["unsupported"] += 1
                    record["outcome"] = "rejected"
                    record["reason"] = "unsupported_by_evidence"
                    rejected.append(record)
                    outcomes.append(record)
                    continue
                if verdict.selected_id != SUPPORTED:
                    verification_tally["deferred"] += 1
                    record["outcome"] = "deferred"
                    record["reason"] = verdict.failure or "verification_deferred"
                    deferred.append(record)
                    outcomes.append(record)
                    continue
                verification_tally["supported"] += 1
                if assertion.epistemic_state == "confirmed":
                    # Generated content reaches confirmed only through an
                    # explicit SUPPORTED verdict — never by default.
                    pass
            if assertion.category == "entities":
                result = _apply_entity(
                    db, campaign, assertion,
                    from_sequence=from_sequence, to_sequence=to_sequence,
                    decision_service=decision_service,
                    session_factory=session_factory,
                    verification_tally=verification_tally,
                )
            elif assertion.category == "relations":
                result = _apply_relation(
                    db, campaign, assertion, from_sequence=from_sequence,
                    to_sequence=to_sequence, operation_id=op_id,
                )
            elif assertion.category == "facts":
                result = _apply_fact(
                    db, campaign, assertion, from_sequence=from_sequence,
                    to_sequence=to_sequence, operation_id=op_id,
                )
            else:
                result = _apply_npc_state(
                    db, campaign, assertion, from_sequence=from_sequence,
                    to_sequence=to_sequence, operation_id=op_id,
                )
        except MaterializeError:
            raise
        except ValueError as exc:
            # Writer-level deterministic refusal (unknown source refs,
            # visibility/schema violations): mechanical hints fail the run,
            # generated candidates are contained as deferred.
            if assertion.mechanical:
                raise MaterializeError(
                    f"{assertion.category}/{assertion.key}: {exc}"
                ) from exc
            record["outcome"] = "deferred"
            record["reason"] = f"deterministic_refusal: {exc}"[:300]
            deferred.append(record)
            outcomes.append(record)
            continue
        outcome = result.get("outcome", "applied")
        record.update(result)
        if outcome == "applied":
            applied[assertion.category] += 1
        elif outcome == "duplicate" or outcome == "resolved_existing":
            duplicates += 1
        elif outcome == "rejected":
            rejected.append(record)
        else:
            deferred.append(record)
        outcomes.append(record)

    summary = {
        "contract_version": MATERIALIZE_CONTRACT_VERSION,
        "from_sequence": from_sequence, "to_sequence": to_sequence,
        "event_count": len(events),
        "proposed": len(candidates),
        "applied": applied,
        "duplicates": duplicates,
        "rejected": len(rejected),
        "deferred": len(deferred),
        "generation": generation_trace,
        "verification": verification_tally,
        "outcomes": outcomes,
    }
    structured_log(
        logger, logging.INFO, "post_turn_materialized",
        campaign_id=str(campaign.id), from_sequence=from_sequence,
        to_sequence=to_sequence, proposed=len(candidates),
        applied=sum(applied.values()), duplicates=duplicates,
        rejected=len(rejected), deferred=len(deferred),
        operation_id=op_id,
    )
    return summary
