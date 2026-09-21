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

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
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
from models.world import WorldEntity

logger = logging.getLogger(__name__)

MATERIALIZE_CONTRACT_VERSION = 1

# Committed-domain-event payload key carrying compiler hints. Turn-commit
# code (or future forward-DM work) populates these; events without hints
# contribute provenance context only. Hint schema is validated strictly —
# malformed mechanical hints fail the run, never guess into canon.
MATERIALIZE_HINT_KEY = "post_turn_materialize"

WRITE_CATEGORIES = (
    "entities", "relations", "facts", "npc_state",
    "knowledge", "scene", "visibility_grants",
)

# Visibility lattice for the widening cap (finding: a private source event
# must not certify party-visible memory absent explicit disclosure).
# Aliases collapse before ranking: party_known -> campaign,
# dm_private -> dm_only.
_VISIBILITY_RANK = {"dm_only": 0, "private": 1, "campaign": 2, "public": 3}

# Staged-effect visibility vocabulary (RecordWorldEventArgs/RevealFactArgs)
# onto the world lattice.
_EFFECT_VISIBILITY_MAP = {
    "public": "campaign", "party_known": "campaign", "dm_private": "dm_only",
    "campaign": "campaign", "private": "private", "dm_only": "dm_only",
}

# Committed turn events the default compiler reads. Their payloads carry
# turn_id/attempt_id locators into the durable attempt row, whose staged
# effects and contract snapshot are committed gameplay — never rewritten.
TURN_EVENT_TYPES = frozenset({"dm.turn_committed", "dm.turn_resolved"})

# Staged effects already applied durably at turn commit: recompiling them
# here would duplicate canon, so the default compiler skips them (counted
# for observability instead).
_COMMIT_APPLIED_EFFECTS = frozenset({
    "assert_fact", "upsert_relation", "transfer_knowledge", "update_scene",
})

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
    source_effect_id: str | None = None
    source_turn_id: uuid.UUID | None = None
    source_attempt_id: uuid.UUID | None = None


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
    if category == "knowledge":
        from app.world.epistemics import validate_knowledge_state
        try:
            validate_knowledge_state(data.get("knowledge_state", "knows"))
        except ValueError as exc:
            raise MaterializeError(
                f"event {event_sequence}: hint {key!r} has invalid knowledge_state: {exc}"
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


# ── Visibility widening cap ──────────────────────────────────────────────

def visibility_rank(value: Any) -> int:
    """Rank a visibility string on the disclosure lattice (fail closed)."""
    from app.world.service import normalize_visibility
    normalized = normalize_visibility(_EFFECT_VISIBILITY_MAP.get(str(value or "").strip(), value))
    return _VISIBILITY_RANK[normalized]


def _disclosure_allows(
    assertion: CandidateAssertion,
    disclosures: dict[tuple[str, str], int],
    *,
    resolved_entity_id: str | None = None,
) -> int:
    """Highest same-range reveal rank authorizing this assertion.

    Matches mirror the reveal contract: entities by canonical id, name, or
    assertion key; facts/relations/knowledge/scene by assertion key or the
    staged effect the compiler derived them from.
    """
    best = -1
    wanted: set[tuple[str, str]] = set()
    if assertion.category == "entities":
        names = {assertion.key, str(assertion.data.get("name") or "")}
        if resolved_entity_id:
            names.add(resolved_entity_id)
        for name in names:
            if name:
                wanted.add(("entity", name))
    else:
        kind = {"facts": "fact", "relations": "relation"}.get(assertion.category, assertion.category)
        for token in (assertion.key, assertion.source_effect_id):
            if token:
                wanted.add((kind, token))
                wanted.add((assertion.category, token))
    for key in wanted:
        rank = disclosures.get(key)
        if rank is not None and rank > best:
            best = rank
    return best


def enforce_visibility_cap(
    assertion: CandidateAssertion,
    source_event: CampaignDomainEvent,
    disclosures: dict[tuple[str, str], int],
    *,
    resolved_entity_id: str | None = None,
) -> None:
    """Reject DM-private evidence certifying party-visible memory.

    Allowed rank is the max of the cited source event's rank and any
    same-range reveal_fact disclosure matching the assertion. Mechanical
    violations fail the run; generated ones are rejected by the caller.
    """
    from app.world.service import normalize_visibility
    target = normalize_visibility(_EFFECT_VISIBILITY_MAP.get(
        str(assertion.visibility or "").strip(), assertion.visibility))
    assertion.visibility = target
    allowed = visibility_rank(source_event.visibility)
    disclosed = _disclosure_allows(
        assertion, disclosures, resolved_entity_id=resolved_entity_id)
    allowed = max(allowed, disclosed)
    if _VISIBILITY_RANK[target] > allowed:
        raise MaterializeError(
            f"{assertion.category}/{assertion.key}: visibility {target!r} widens "
            f"source seq {assertion.source_sequence} ({source_event.visibility!r}) "
            f"without explicit disclosure"
        )


# ── Committed-structure compiler ─────────────────────────────────────────

def _coerce_uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def compile_committed_candidates(
    db: Session, campaign: Campaign, events: list[CampaignDomainEvent],
) -> tuple[list[CandidateAssertion], dict[tuple[str, str], int], dict[str, int]]:
    """Compile candidates from committed turn structures (default source).

    Loads each dm.turn_committed/dm.turn_resolved event's durable attempt
    and translates staged effects the turn commit left unpersisted:

    - record_world_event -> confirmed-history fact (the commit handler
      explicitly persists nothing; this materializer is that extension);
    - reveal_fact -> same-range disclosure authorization for the
      visibility cap (no row of its own);
    - contract new_entities proposals missing from canon -> entity
      assertions (promotion normally covers these; anything left is a
      genuine gap, never a duplicate — exact identity reuses the owner);
    - assert_fact/upsert_relation/transfer_knowledge/update_scene ->
      skipped: already durable from the turn commit.

    Returns (candidates, disclosures, counts). Unknown attempt references
    fail closed; events without turn locators are skipped and counted.
    """
    from models.dm import DmTurnAttempt

    candidates: list[CandidateAssertion] = []
    disclosures: dict[tuple[str, str], int] = {}
    counts = {"turn_events": 0, "skipped_no_locator": 0,
              "already_committed": 0, "compiled": 0, "disclosures": 0}
    for event in events:
        if event.event_type not in TURN_EVENT_TYPES:
            continue
        counts["turn_events"] += 1
        payload = event.payload or {}
        attempt = None
        attempt_id = _coerce_uuid_or_none(payload.get("attempt_id"))
        turn_id = _coerce_uuid_or_none(payload.get("turn_id"))
        if attempt_id is not None:
            attempt = db.get(DmTurnAttempt, attempt_id)
            if attempt is None or attempt.campaign_id != campaign.id:
                raise MaterializeError(
                    f"event {event.sequence}: turn locator references "
                    f"unknown attempt {payload.get('attempt_id')!r}"
                )
        if attempt is None:
            counts["skipped_no_locator"] += 1
            continue
        for eff in attempt.staged_effects or []:
            if not isinstance(eff, dict):
                raise MaterializeError(
                    f"event {event.sequence}: staged effect must be an object")
            eff_type = eff.get("effect_type")
            eff_id = str(eff.get("id") or "").strip()
            args = eff.get("arguments") or {}
            if not isinstance(args, dict):
                raise MaterializeError(
                    f"event {event.sequence}: staged effect {eff_id!r} arguments "
                    f"must be an object")
            if eff_type in _COMMIT_APPLIED_EFFECTS:
                counts["already_committed"] += 1
                continue
            if eff_type == "reveal_fact":
                item_type = str(args.get("item_type") or "").strip()
                item_id = str(args.get("item_id") or "").strip()
                if not item_type or not item_id:
                    raise MaterializeError(
                        f"event {event.sequence}: reveal_fact {eff_id!r} "
                        f"requires item_type and item_id")
                rank = visibility_rank(args.get("visibility") or "dm_private")
                key = (item_type, item_id)
                disclosures[key] = max(disclosures.get(key, -1), rank)
                counts["disclosures"] += 1
                continue
            if eff_type == "record_world_event":
                summary = args.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    raise MaterializeError(
                        f"event {event.sequence}: record_world_event {eff_id!r} "
                        f"requires a summary")
                # Preserve the full historical-event metadata (#217): the
                # fact content carries the summary, while event_type,
                # payload, source facets, and the staged effect id travel
                # in details/provenance — never dropped.
                facet_ids = args.get("source_facet_ids")
                if facet_ids is not None and not isinstance(facet_ids, list):
                    raise MaterializeError(
                        f"event {event.sequence}: record_world_event {eff_id!r} "
                        f"source_facet_ids must be a list")
                effect_payload = args.get("payload")
                if effect_payload is not None and not isinstance(effect_payload, dict):
                    raise MaterializeError(
                        f"event {event.sequence}: record_world_event {eff_id!r} "
                        f"payload must be an object")
                assertion = CandidateAssertion(
                    category="facts", key=f"rec-{eff_id}",
                    data={
                        "content": summary.strip(),
                        "details": {
                            "historical_event": {
                                "event_type": args.get("event_type"),
                                "payload": effect_payload or {},
                                "source_facet_ids": facet_ids or [],
                                "effect_id": eff_id or None,
                            },
                        },
                    },
                    visibility=_EFFECT_VISIBILITY_MAP.get(
                        str(args.get("visibility") or "dm_private").strip(),
                        "dm_only"),
                    epistemic_state="confirmed", mechanical=True,
                    source_event_id=event.id, source_sequence=event.sequence,
                    source_effect_id=eff_id or None,
                    source_turn_id=turn_id, source_attempt_id=attempt_id,
                )
                candidates.append(assertion)
                counts["compiled"] += 1
                continue
            # Other staged types (mechanics, encounters, sheets) carry no
            # durable memory semantics; they are not materialization input.
        snapshot = attempt.contract_snapshot or {}
        proposals = snapshot.get("new_entities") or []
        if not isinstance(proposals, list):
            raise MaterializeError(
                f"event {event.sequence}: contract new_entities must be a list")
        from app.world.service import _stable_jit_key
        for proposal in proposals:
            if not isinstance(proposal, dict):
                raise MaterializeError(
                    f"event {event.sequence}: entity proposal must be an object")
            name = proposal.get("public_name") or proposal.get("name")
            if not isinstance(name, str) or not name.strip():
                raise MaterializeError(
                    f"event {event.sequence}: entity proposal is missing a name")
            temp_id = str(proposal.get("temp_id") or "").strip()
            if temp_id:
                # Promotion at turn commit is keyed per (attempt, temp_id):
                # a live row under that key means this proposal already
                # went through #214 (including KEEP_DISTINCT, which
                # legitimately shares its name). Re-emitting it would
                # split the duplicate into a third entity.
                jit_key = _stable_jit_key(attempt.id, temp_id)
                promoted = db.execute(select(WorldEntity).where(
                    WorldEntity.campaign_id == campaign.id,
                    WorldEntity.idempotency_key == jit_key,
                )).scalars().first()
                if promoted is not None and not promoted.superseded_by_id:
                    counts["already_committed"] += 1
                    continue
            entity, _ = resolve_entity_ref(db, campaign.id, name.strip())
            if entity is not None:
                continue  # Promoted at commit; reuse, never duplicate.
            candidates.append(CandidateAssertion(
                category="entities", key=f"proposal-{len(candidates)}",
                data={"name": name.strip(), "entity_type": "npc",
                      "summary": proposal.get("public_summary"),
                      "ref": name.strip()},
                visibility="campaign", mechanical=True,
                source_event_id=event.id, source_sequence=event.sequence,
                source_turn_id=turn_id, source_attempt_id=attempt_id,
            ))
            counts["compiled"] += 1
    return candidates, disclosures, counts


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
    assertion: CandidateAssertion, evidence_events: list[CampaignDomainEvent],
    *, from_sequence: int, to_sequence: int,
) -> DecisionFrame:
    """Build the bounded frame over the candidate's cited source evidence.

    Evidence is bound to the cited source event(s) — never the whole
    range — so a candidate citing a public event cannot be judged
    SUPPORTED on the strength of unrelated DM-private evidence and then
    pass the visibility cap against its false public source.
    """
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
            "evidence": _evidence_excerpt(evidence_events),
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
    """Bounded deterministic key: fixed prefix + digest (always <= 128 chars).

    Contract keys may be up to 128 chars themselves, so embedding them
    verbatim would overflow the world writers' idempotency cap and fail
    the run. The digest preserves exactly-once semantics per
    (campaign, range, category, key).
    """
    raw = f"{campaign_id}:{from_sequence}-{to_sequence}:{assertion.category}:{assertion.key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"pt217-{assertion.category}-{digest}"


def _canonical_name_count(db: Session, campaign_id: uuid.UUID, name: str) -> int:
    """Live canonical entities sharing one normalized name."""
    from app.world.identity import normalize_alias
    normalized = normalize_alias(name)
    rows = db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == campaign_id,
        WorldEntity.superseded_by_id.is_(None),
    )).scalars().all()
    return sum(1 for e in rows if normalize_alias(e.name) == normalized)


def _apply_entity(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int,
    decision_service: DecisionService | None, session_factory: Any,
    verification_tally: dict[str, int],
    disclosures: dict[tuple[str, str], int] | None = None,
    source_event: CampaignDomainEvent | None = None,
) -> dict[str, Any]:
    """Apply one entity assertion: reuse exact identity, bounded-resolve
    collisions via #214, or create. Ambiguity without a decision service
    defers fail-closed. The visibility cap re-checks once the canonical
    id is known, so an id-matched reveal authorizes precisely."""
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

    def _gate(entity_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        if source_event is None:
            return outcome
        try:
            enforce_visibility_cap(
                assertion, source_event, disclosures or {},
                resolved_entity_id=entity_id,
            )
        except MaterializeError as exc:
            if assertion.mechanical:
                raise
            return {"outcome": "rejected",
                    "reason": f"visibility_widening: {exc}"[:300]}
        return outcome

    entity, how = resolve_entity_ref(db, campaign.id, ref)
    if entity is not None and how in ("uuid", "alias"):
        return _gate(str(entity.id), {
            "outcome": "resolved_existing",
            "entity_id": str(entity.id), "via": how,
        })
    if entity is None:
        collision = exact_identity(db, campaign.id, name)
        if collision is None and _canonical_name_count(db, campaign.id, name) == 0:
            created, is_new = create_entity_inline(
                db, campaign, entity_type=str(entity_type).strip().lower(),
                name=name.strip(), summary=data.get("summary"),
                status=status, visibility=assertion.visibility,
                details=data.get("details") if isinstance(data.get("details"), dict) else {},
                source_turn_id=assertion.source_turn_id,
                source_attempt_id=assertion.source_attempt_id,
                operation_id=f"post-turn-217:{from_sequence}-{to_sequence}",
                idempotency_key=key,
            )
            return _gate(str(created.id), {
                "outcome": "applied" if is_new else "duplicate",
                "entity_id": str(created.id), "via": "created",
            })
    # Ambiguous: exact canonical-name collision (single or KEEP_DISTINCT
    # multi-match) — route through #214.
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
        return _gate(str(decision.selected_id), {
            "outcome": "resolved_existing",
            "entity_id": str(decision.selected_id), "via": "identity_decision",
        })
    try:
        created, is_new = create_entity_after_resolution(
            db, campaign, frame, decision.selected_id,
            entity_type=str(entity_type).strip().lower(), name=name.strip(),
            idempotency_key=key, details=data.get("details"),
            summary=data.get("summary"), operation_id=f"post-turn-217:{from_sequence}-{to_sequence}",
            source_turn_id=assertion.source_turn_id,
            source_attempt_id=assertion.source_attempt_id,
        )
    except ValueError as exc:
        return {"outcome": "deferred", "reason": f"identity_resolution_rejected: {exc}"}
    return _gate(str(created.id), {
        "outcome": "applied" if is_new else "duplicate",
        "entity_id": str(created.id), "via": "identity_decision",
    })


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
    run_stamp = f"{from_sequence}-{to_sequence}:{assertion.category}:{assertion.key}"
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
            "source": "post_turn_materialize",
            "post_turn_run": run_stamp,
            "source_sequence": assertion.source_sequence,
        },
        source_event_id=assertion.source_event_id,
        source_turn_id=assertion.source_turn_id,
        source_attempt_id=assertion.source_attempt_id,
        operation_id=operation_id,
        **updates,
    )
    return {"outcome": "applied", "entity_id": str(row.entity_id)}


def _apply_knowledge(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int, operation_id: str | None,
) -> dict[str, Any]:
    """Assert one knower stance toward one truth record (#211 lanes).

    Never mutates truth tables by construction; re-assertion updates the
    single current row per (subject, target) in place.
    """
    from app.world.epistemics import (
        assert_knowledge_inline,
        validate_knower_kind,
        validate_knowledge_target_kind,
    )

    data = assertion.data
    try:
        subject_kind = validate_knower_kind(data.get("subject_kind"))
    except ValueError as exc:
        if assertion.mechanical:
            raise MaterializeError(f"knowledge/{assertion.key}: {exc}") from exc
        return {"outcome": "rejected", "reason": f"invalid_subject_kind: {exc}"}
    subject = _resolve_required_ref(
        db, campaign.id, data.get("subject_ref"), assertion=assertion, role="subject")
    if subject is None:
        return {"outcome": "deferred", "reason": "unresolvable_subject_ref"}
    try:
        target_kind = validate_knowledge_target_kind(data.get("target_kind"))
    except ValueError as exc:
        if assertion.mechanical:
            raise MaterializeError(f"knowledge/{assertion.key}: {exc}") from exc
        return {"outcome": "rejected", "reason": f"invalid_target_kind: {exc}"}
    target_ref = data.get("target_ref")
    target_ids: dict[str, Any] = {}
    if target_ref is not None:
        target = _resolve_required_ref(
            db, campaign.id, target_ref, assertion=assertion, role="target")
        if target is None:
            return {"outcome": "deferred", "reason": "unresolvable_target_ref"}
        target_ids = {"target_id": target.id}
    elif target_kind == "fact" and data.get("target_fact_id") is not None:
        target_ids = {"target_fact_id": data["target_fact_id"]}
    elif target_kind == "relation" and data.get("target_relation_id") is not None:
        target_ids = {"target_relation_id": data["target_relation_id"]}
    elif target_kind == "entity" and data.get("target_entity_id") is not None:
        target_ids = {"target_entity_id": data["target_entity_id"]}
    else:
        if assertion.mechanical:
            raise MaterializeError(
                f"knowledge/{assertion.key}: requires target_ref or a target id")
        return {"outcome": "rejected", "reason": "missing_target"}
    row, created = assert_knowledge_inline(
        db, campaign, subject_kind=subject_kind, subject_entity_id=subject.id,
        target_kind=target_kind, **target_ids,
        knowledge_state=data.get("knowledge_state", "knows"),
        acquisition_source=data.get("acquisition_source") or "post_turn_materialize",
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
    return {"outcome": "applied" if created else "duplicate", "knowledge_id": str(row.id)}


def _apply_scene(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, operation_id: str | None,
) -> dict[str, Any]:
    """Apply one current-state projection to the transient scene row (#209).

    Single-row upsert semantics converge on replay (no duplication
    possible). Uses the current campaign revision without bumping it —
    post-turn consolidation never advances the revision counter.
    """
    from app.world.service import UNSET, apply_scene_update_inline

    data = assertion.data
    if not isinstance(data.get("scene_patch", {}), dict) and "scene_patch" in data:
        if assertion.mechanical:
            raise MaterializeError(f"scene/{assertion.key}: scene_patch must be an object")
        return {"outcome": "rejected", "reason": "invalid_scene_patch"}
    patch = data.get("scene_patch") or {}
    location_ref = patch.get("location_entity_id", data.get("location_entity_id", UNSET))
    location_id: Any = UNSET
    if location_ref is not UNSET and location_ref is not None:
        location, _ = resolve_entity_ref(db, campaign.id, location_ref)
        if location is None:
            if assertion.mechanical:
                raise MaterializeError(
                    f"scene/{assertion.key}: location ref {location_ref!r} "
                    f"matches no canonical entity")
            return {"outcome": "deferred", "reason": "unresolvable_location_ref"}
        location_id = location.id
    elif location_ref is None:
        location_id = None
    row = apply_scene_update_inline(
        db, campaign, new_revision=int(campaign.revision or 0),
        location_entity_id=location_id,
        location_name=patch.get("location_name", data.get("location_name")),
        fictional_time=patch.get("fictional_time", data.get("fictional_time")),
        fictional_time_details=patch.get("fictional_time_details"),
        present_actors=patch.get("present_actors", data.get("present_actors")),
        environment=patch.get("environment", data.get("environment")),
        visibility=assertion.visibility,
        source_turn_id=None, source_attempt_id=None,
        operation_id=operation_id,
    )
    return {"outcome": "applied", "scene_revision": int(row.revision)}


def _apply_visibility_grant(
    db: Session, campaign: Campaign, assertion: CandidateAssertion,
    *, from_sequence: int, to_sequence: int, operation_id: str | None,
) -> dict[str, Any]:
    """Authorize one human user for one record (#211 grant lanes).

    Grants are the durable form of explicit disclosure: they widen human
    access without touching fictional-character knowledge or truth rows.
    """
    from app.world.epistemics import grant_visibility_inline, validate_grant_target_kind

    data = assertion.data
    try:
        target_kind = validate_grant_target_kind(data.get("target_kind"))
    except ValueError as exc:
        if assertion.mechanical:
            raise MaterializeError(f"visibility_grants/{assertion.key}: {exc}") from exc
        return {"outcome": "rejected", "reason": f"invalid_target_kind: {exc}"}
    target = data.get("target_ref")
    if target is None:
        target = (data.get("target_fact_id") or data.get("target_relation_id")
                  or data.get("target_entity_id") or data.get("target_id"))
    tid: uuid.UUID | None = None
    if target is not None:
        tid = _coerce_uuid_or_none(target)
        if tid is None:
            # Name/alias form: only canonical entities resolve this way.
            entity = _resolve_required_ref(
                db, campaign.id, target, assertion=assertion, role="target")
            if entity is None:
                return {"outcome": "deferred", "reason": "unresolvable_target_ref"}
            tid = entity.id
    if tid is None:
        if assertion.mechanical:
            raise MaterializeError(
                f"visibility_grants/{assertion.key}: requires target_ref or a target id")
        return {"outcome": "rejected", "reason": "missing_target"}
    try:
        row, created = grant_visibility_inline(
            db, campaign, target_kind=target_kind, target_id=tid,
            grantee_user_id=data.get("grantee_user_id"),
            granted_by=data.get("granted_by"),
            operation_id=operation_id,
            idempotency_key=_idempotency_key(
                campaign.id, from_sequence, to_sequence, assertion),
        )
    except ValueError as exc:
        if assertion.mechanical:
            raise MaterializeError(
                f"visibility_grants/{assertion.key}: {exc}") from exc
        return {"outcome": "rejected", "reason": f"invalid_grant: {exc}"}
    return {"outcome": "applied" if created else "duplicate", "grant_id": str(row.id)}


def _append_unique(
    candidates: list[CandidateAssertion], assertion: CandidateAssertion, from_sequence: int,
) -> None:
    """Append a hint/provider assertion, failing closed on key collision."""
    if any(a.category == assertion.category and a.key == assertion.key for a in candidates):
        raise MaterializeError(
            f"event {from_sequence}: duplicate assertion key "
            f"{assertion.category}/{assertion.key} in range"
        )
    candidates.append(assertion)


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

    Candidate sources, in order: the committed-structure compiler (turn
    staged effects left unpersisted at commit), explicit materialize hints
    on event payloads (validated strictly), and an optional generative
    candidate provider. Raises MaterializeError on deterministic failure
    (run fails, checkpoint stays). Unsupported/uncertain assertions are
    recorded as rejected or deferred while the range still consumes.
    Flushes; never commits — the post-turn worker owns commit/rollback
    so retries converge.
    """
    service = decision_service or DecisionService()
    op_id = operation_id or f"post-turn-217:{campaign.id}:{from_sequence}-{to_sequence}"
    events_by_id = {e.id: e for e in events}

    committed, disclosures, compile_counts = compile_committed_candidates(db, campaign, events)
    candidates = list(committed)
    generation_trace: dict[str, Any] = {
        "role": "deterministic-compiler", "model": None,
        "compiler_candidates": len(candidates), "generated_candidates": 0,
        "committed_compile": compile_counts,
    }
    for assertion in extract_candidates(events):
        _append_unique(candidates, assertion, from_sequence)
    if candidate_provider is not None:
        try:
            extra_raw, gen_trace = candidate_provider(events)
        except Exception as exc:
            raise MaterializeError(f"candidate generation failed: {exc}") from exc
        if not isinstance(extra_raw, list):
            raise MaterializeError("candidate provider must return a list of hint objects")
        for raw in extra_raw:
            assertion = validate_hint(raw, event_sequence=from_sequence)
            # The provider is the generative lane: its output can never
            # self-mark as mechanical to bypass bounded verification.
            # Generation proposes; bounded decisions verify (#379).
            assertion.mechanical = False
            # Generated candidates must cite their actual in-range source
            # event: defaulting to the first event would launder private
            # evidence through an unrelated public event and defeat the
            # visibility cap. Missing/foreign provenance rejects the
            # candidate below instead of failing the range.
            seq = raw.get("source_sequence")
            source = next((e for e in events if e.sequence == seq), None)
            if source is None:
                assertion.source_event_id = None
                assertion.source_sequence = seq if isinstance(seq, int) else None
            else:
                assertion.source_event_id = source.id
                assertion.source_sequence = source.sequence
            _append_unique(candidates, assertion, from_sequence)
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
        # Category grouping preserves write dependencies (entities before
        # relations/facts); within a category, committed chronology wins
        # so the latest committed value is final current state.
        key=lambda a: (WRITE_CATEGORIES.index(a.category),
                       a.source_sequence if a.source_sequence is not None else 0,
                       a.key),
    )
    for assertion in ordered:
        record: dict[str, Any] = {
            "category": assertion.category, "key": assertion.key,
            "mechanical": assertion.mechanical,
            "source_sequence": assertion.source_sequence,
        }
        try:
            # Source provenance resolves before anything else: every
            # assertion — grants included — must cite a real in-range
            # source event, and verification is bound to that cited
            # evidence so private range-mates cannot launder support.
            source_event = (
                events_by_id.get(assertion.source_event_id)
                if assertion.source_event_id is not None else None
            )
            if source_event is None:
                if assertion.mechanical:
                    raise MaterializeError(
                        f"{assertion.category}/{assertion.key}: missing "
                        f"source event for provenance"
                    )
                record["outcome"] = "rejected"
                record["reason"] = "invalid_provenance: unknown source event"
                rejected.append(record)
                outcomes.append(record)
                continue
            # Every generated (non-mechanical) assertion is verified —
            # entities included: verification judges evidential support
            # first, then #214 identity handling gates entity creation.
            # With no decision service configured the range runs
            # deterministic-only and generated candidates defer fail-safe
            # without touching any model adapter.
            needs_verdict = not assertion.mechanical
            if needs_verdict and decision_service is None:
                verification_tally["deferred"] += 1
                record["outcome"] = "deferred"
                record["reason"] = "no_decision_service"
                deferred.append(record)
                outcomes.append(record)
                continue
            if needs_verdict:
                frame = build_verification_frame(
                    assertion, [source_event],
                    from_sequence=from_sequence, to_sequence=to_sequence,
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
            # Grants ARE explicit disclosure, so only they skip the
            # widening cap itself (provenance above still applies).
            if assertion.category != "visibility_grants":
                try:
                    enforce_visibility_cap(
                        assertion, source_event, disclosures)
                except MaterializeError as exc:
                    if assertion.mechanical:
                        raise
                    record["outcome"] = "rejected"
                    record["reason"] = f"visibility_widening: {exc}"[:300]
                    rejected.append(record)
                    outcomes.append(record)
                    continue
            if assertion.category == "entities":
                result = _apply_entity(
                    db, campaign, assertion,
                    from_sequence=from_sequence, to_sequence=to_sequence,
                    decision_service=decision_service,
                    session_factory=session_factory,
                    verification_tally=verification_tally,
                    disclosures=disclosures,
                    source_event=events_by_id.get(assertion.source_event_id),
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
            elif assertion.category == "knowledge":
                result = _apply_knowledge(
                    db, campaign, assertion, from_sequence=from_sequence,
                    to_sequence=to_sequence, operation_id=op_id,
                )
            elif assertion.category == "scene":
                result = _apply_scene(
                    db, campaign, assertion, operation_id=op_id,
                )
            elif assertion.category == "visibility_grants":
                result = _apply_visibility_grant(
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
