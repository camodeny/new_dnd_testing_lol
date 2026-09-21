"""Rebuildable campaign summaries with bounded claim-support verification — issue #219.

Post-turn maintains compact narrative/context summaries as rebuildable
derived projections of stronger authoritative evidence. Summary prose stays
generative; claim-level support/secrecy verification uses bounded semantic
decisions before a summary becomes eligible for forward-DM context.

Authority split (under #379; the AI is the only DM):

- Generation (an injected provider, default deterministic stub) proposes
  prose only. It never authorizes anything.
- Deterministic code owns source existence/range/provenance, the visibility
  widening cap, idempotency/versioning, and the status transition. A
  positive semantic verdict never overrides a deterministic failure.
- Bounded decisions (``campaign_summary_verify`` role, #380/#381 runtime)
  judge each claim SUPPORTED / UNSUPPORTED / DEFER against the authorized
  source evidence. They can reject or defer, never authorize or widen
  visibility.

Role separation for #383 calibration: generation is recorded on the summary
row + structured logs under role ``summary_generation``; verification runs
through the decisions runtime under decision class ``campaign_summary_verify``
(role ``summary_verification``), persisted fail-soft via the session factory.
The two roles are never collapsed into one aggregate.

Lifecycle: ``pending`` (generated, awaiting verification) → ``current``
(all claims SUPPORTED); ``deferred`` (uncertain claims, escalatable);
``stale`` (source repair/retcon invalidated the range); ``failed``
(generation error, deterministic rejection, or exhausted regeneration).
Only ``current`` rows are eligible for forward-DM context, and then only as
the lower-authority lane — direct sources always outrank them. Claim spans
cover every stored character (overlong sentences chunk, never truncate);
drafts exceeding the claim budget are deterministically rejected, so
unverified prose can never become ``current`` by omission. The source
snapshot (hash + verifier evidence) covers the range events plus the
current active facts/relations citing them, so an inline repair changes
what the judge sees: a rebuild cannot re-verify stale pre-repair prose
against pre-repair evidence.

Summary/index failures are independently retryable derived work: they never
delete or rewrite authoritative source records.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decisions import (
    CandidateRecord,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    build_frame,
    register_policy,
)
from app.observability.tracing import structured_log
from models.campaigns import Campaign, CampaignDomainEvent
from models.world import (
    SUMMARY_SCOPE_RUNNING,
    CampaignSummary,
)

logger = logging.getLogger(__name__)

SUMMARY_SOURCE = "campaign_summary_219"

# ── Roles (kept separate for #383 per-role calibration) ──────────────────────

GENERATION_ROLE = "summary_generation"
VERIFICATION_ROLE = "summary_verification"

# ── Bounded verification vocabulary (mirrors #217 materialize) ───────────────

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
DEFER = "DEFER"

SUMMARY_DECISION_CLASS = "campaign_summary_verify"
SUMMARY_QUESTION_ID = "verify_summary_claim"
SUMMARY_FRAME_INSTRUCTIONS = (
    "Select only the supplied outcome that matches the authorized evidence. "
    "SUPPORTED requires the claim to follow directly from the supplied "
    "evidence excerpts; paraphrase still counts when the meaning matches. "
    "When the evidence is insufficient or ambiguous, defer. Never invent "
    "facts outside the supplied evidence."
)
SUMMARY_FRAME_SCHEMA_VERSION = 1

register_policy(DecisionClassPolicy(
    decision_class=SUMMARY_DECISION_CLASS,
    min_probability_direct=.85, min_confidence_direct=.8,
    min_margin_direct=.25, near_tie_margin=.25,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))

# Verification policy recorded on every summary row (observability).
VERIFICATION_POLICY = {
    "decision_class": SUMMARY_DECISION_CLASS,
    "question_id": SUMMARY_QUESTION_ID,
    "frame_schema": SUMMARY_FRAME_SCHEMA_VERSION,
    "unsupported_action": "reject_and_regenerate_once",
    "uncertain_action": "defer_escalate",
    "max_regenerations": 1,
}

# Visibility lattice (same disclosure order as #217: a private source must
# not certify party-visible prose absent explicit disclosure; summaries take
# no disclosures, so the cap is strict).
_VISIBILITY_RANK = {"dm_only": 0, "private": 1, "campaign": 2, "public": 3}
_MEMBER_VISIBLE = frozenset({"campaign", "public"})

# Deterministic bounds.
MAX_PROSE_CHARS = 8000
MAX_CLAIMS = 50
MAX_CLAIM_SPAN = 500
MAX_EVIDENCE_EXCERPT = 1000


class SummaryError(ValueError):
    """Caller error (bad range/visibility) — not a retryable derived failure."""


# ── Generation (generative role; proposes prose only) ────────────────────────

@dataclass
class SummaryDraft:
    """One generated prose proposal with its own generation telemetry."""

    prose: str
    provider: str = "stub"
    model: str = "stub-summary-v1"
    latency_ms: float = 0.0


class SummaryProvider(Protocol):
    """Injectable prose generator. Never authorizes; only proposes text.

    ``evidence`` is the visibility-filtered source snapshot: domain-event
    excerpts (``kind == "domain_event"`` with ``sequence``/``event_type``)
    plus current record excerpts (``kind`` ``world_fact``/``world_relation``
    with ``record_id``/``status``/``epistemic_state``/``content_excerpt``,
    including terminal retractions as explicit markers). Providers must
    tolerate mixed item shapes — select by ``kind``.
    """

    def __call__(
        self,
        evidence: list[dict[str, Any]],
        *,
        from_sequence: int,
        to_sequence: int,
        visibility: str,
        attempt: int,
        feedback: str | None,
    ) -> SummaryDraft: ...


def default_stub_provider(
    evidence: list[dict[str, Any]],
    *,
    from_sequence: int,
    to_sequence: int,
    visibility: str,
    attempt: int,
    feedback: str | None,
) -> SummaryDraft:
    """Deterministic convergent stub: one sentence per committed event."""
    started = time.monotonic()
    sentences = [
        f"Sequence {item['sequence']} records {item['event_type']}."
        for item in evidence
        if item.get("kind", "domain_event") == "domain_event"
    ]
    prose = f"Campaign events {from_sequence}-{to_sequence}: " + " ".join(sentences)
    return SummaryDraft(
        prose=prose, provider="stub", model="stub-summary-v1",
        latency_ms=(time.monotonic() - started) * 1000,
    )


_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])|\n+")


def _chunk_span(part: str, *, limit: int = MAX_CLAIM_SPAN) -> list[str]:
    """Split one overlong sentence into fully-covering claim spans.

    Chunking prefers whitespace boundaries but coverage is exact: no
    character is ever truncated, so no stored prose can escape
    verification. A trailing fragment below the verifiable minimum cannot
    stand alone, so it folds into the previous span rather than dropping.
    """
    rest = part.strip()
    if not rest:
        return []
    chunks: list[str] = []
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind(" ", 0, limit + 1)
        cut = cut if cut > 0 else limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].strip()
    if len(chunks) > 1 and len(chunks[-1]) < 3:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {tail}".strip()
    return [c for c in chunks if c and len(c) >= 3]


def split_claims(prose: str) -> list[str]:
    """Deterministically split prose into verifiable claim spans.

    Every character of the prose is covered by exactly one span: overlong
    sentences are chunked (never truncated) and no span is dropped for
    count. When the span count exceeds MAX_CLAIMS, deterministic validation
    rejects the draft — verification never silently skips stored prose.
    """
    text = str(prose or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _CLAIM_SPLIT_RE.split(text) if p and p.strip()]
    if not parts:
        return [text] if len(text) >= 3 else []
    spans: list[str] = []
    for part in parts:
        spans.extend(_chunk_span(part))
    # Fold fragments below the verifiable minimum into a neighbor so no
    # stored character escapes verification (coverage stays exact).
    merged: list[str] = []
    for span in spans:
        if len(span) < 3 and merged:
            merged[-1] = f"{merged[-1]} {span}".strip()
        else:
            merged.append(span)
    if len(merged) > 1 and len(merged[0]) < 3:
        merged[1] = f"{merged[0]} {merged[1]}".strip()
        del merged[0]
    return [s for s in merged if len(s) >= 3]


# ── Deterministic validation (code-owned; never delegated) ───────────────────

def visibility_rank(value: Any) -> int:
    """Rank a visibility string on the disclosure lattice (fail closed)."""
    from app.world.service import normalize_visibility

    return _VISIBILITY_RANK[normalize_visibility(value)]


def _evidence_excerpt(events: list[CampaignDomainEvent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        out.append({
            "kind": "domain_event",
            "sequence": event.sequence,
            "event_type": event.event_type,
            "visibility": event.visibility,
            "event_id": str(event.id),
            "payload_excerpt": str(event.payload or "")[:MAX_EVIDENCE_EXCERPT],
        })
    return out


def _source_hash(
    events: list[CampaignDomainEvent],
    facts: Any = (),
    relations: Any = (),
) -> str:
    """Snapshot hash over events plus current repaired record versions.

    Inline repairs (fact/relation supersession) carry forward the prior
    ``source_event_id`` without emitting a new domain event, so an
    events-only hash cannot see them: a rebuild would re-verify the same
    pre-repair evidence and restore stale prose to ``current``. Hashing the
    active in-range record versions makes the snapshot repair-sensitive.
    """
    digest = hashlib.sha256()
    for event in events:
        digest.update(
            f"{event.sequence}:{event.event_type}:{event.visibility}:"
            f"{hashlib.sha256(str(event.payload or '').encode()).hexdigest()}|".encode()
        )
    for fact in facts:
        digest.update(
            f"fact:{fact.id}:{fact.version}:{fact.status}:{fact.visibility}:"
            f"{hashlib.sha256(str(fact.content or '').encode()).hexdigest()}|".encode()
        )
    for relation in relations:
        digest.update(
            f"relation:{relation.id}:{relation.version}:{relation.status}:"
            f"{relation.visibility}:"
            f"{relation.relation_type}:{relation.subject_entity_id}:"
            f"{relation.object_entity_id or relation.object_label}|".encode()
        )
    return digest.hexdigest()[:64]


def _range_records(
    db: Session, campaign_id: uuid.UUID, event_ids: set[Any],
) -> tuple[list[Any], list[Any]]:
    """Latest source-linked record versions (repair-sensitive sources).

    ``active`` rows plus terminal ``retracted`` rows: supersession only ever
    replaces an active prior, so a ``superseded`` row always has a successor
    (active or retracted) representing the chain, while a ``retracted`` row
    is terminal — excluding it would hide the retcon and let a rebuild
    re-verify stale prose against pre-repair event evidence alone. Records
    without an in-range source event are out of scope and excluded.
    """
    from models.world import WorldFact, WorldRelation

    if not event_ids:
        return [], []
    latest = ("active", "retracted")
    facts = list(db.execute(select(WorldFact).where(
        WorldFact.campaign_id == campaign_id,
        WorldFact.status.in_(latest),
        WorldFact.source_event_id.in_(event_ids),
    )).scalars().all())
    relations = list(db.execute(select(WorldRelation).where(
        WorldRelation.campaign_id == campaign_id,
        WorldRelation.status.in_(latest),
        WorldRelation.source_event_id.in_(event_ids),
    )).scalars().all())
    return facts, relations


def _record_excerpts(facts: Any, relations: Any) -> list[dict[str, Any]]:
    """Verifier excerpts exposing lifecycle so retractions can't support claims."""
    out: list[dict[str, Any]] = []
    for fact in facts:
        out.append({
            "kind": "world_fact",
            "record_id": str(fact.id),
            "visibility": fact.visibility,
            "status": fact.status,
            "epistemic_state": getattr(fact, "epistemic_state", None),
            "content_excerpt": str(fact.content or "")[:MAX_EVIDENCE_EXCERPT],
        })
    for relation in relations:
        out.append({
            "kind": "world_relation",
            "record_id": str(relation.id),
            "visibility": relation.visibility,
            "status": relation.status,
            "epistemic_state": getattr(relation, "epistemic_state", None),
            "content_excerpt": (
                f"{relation.subject_entity_id} {relation.relation_type} "
                f"{relation.object_entity_id or relation.object_label}"
            )[:MAX_EVIDENCE_EXCERPT],
        })
    return out


def deterministic_validate(
    claims: list[str],
    events: list[CampaignDomainEvent],
    summary_visibility: str,
    *,
    prose: str,
    facts: Any = (),
    relations: Any = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Code-owned source/range/provenance + visibility checks per claim.

    Returns (validated_claims, failures). Any failure rejects the summary
    regardless of semantic judge output — the judge is never consulted on a
    deterministically rejected draft.
    """
    if not events:
        return [], ["no_source_events"]
    seqs = [e.sequence for e in events]
    if seqs != list(range(seqs[0], seqs[-1] + 1)):
        return [], ["source_range_not_contiguous"]
    for event in events:
        if not event.visibility:
            return [], [f"source seq {event.sequence} missing visibility metadata"]
    for record in (*facts, *relations):
        if not getattr(record, "visibility", None):
            return [], [f"source record {record.id} missing visibility metadata"]
    try:
        summary_rank = visibility_rank(summary_visibility)
    except ValueError as exc:
        raise SummaryError(f"invalid summary visibility: {exc}") from exc
    if len(prose) > MAX_PROSE_CHARS:
        return [], ["prose_too_long"]
    if not claims:
        return [], ["no_verifiable_claims"]
    if len(claims) > MAX_CLAIMS:
        return [], [
            f"too_many_claims: {len(claims)} spans exceed the "
            f"{MAX_CLAIMS}-claim verification budget; regenerate compressed prose"
        ]
    narrowest = min(
        [visibility_rank(e.visibility) for e in events]
        + [visibility_rank(r.visibility) for r in (*facts, *relations)]
    )
    if summary_rank > narrowest:
        narrowest_vis = min(
            [e.visibility for e in events]
            + [r.visibility for r in (*facts, *relations)],
            key=lambda v: visibility_rank(v),
        )
        return [], [
            f"visibility_widening: summary {summary_visibility!r} is broader "
            f"than narrowest source {narrowest_vis!r} in range "
            f"{seqs[0]}-{seqs[-1]}"
        ]
    from_seq, to_seq = seqs[0], seqs[-1]
    validated = [
        {
            "id": f"claim-{idx}",
            "text": claim,
            "source_from": from_seq,
            "source_to": to_seq,
            "verdict": "unverified",
            "reason": None,
        }
        for idx, claim in enumerate(claims)
    ]
    return validated, []


def _allowed_evidence(
    events: list[CampaignDomainEvent], summary_visibility: str,
) -> list[CampaignDomainEvent]:
    """Evidence the summary may lean on: never narrower than its visibility.

    A party-visible summary is judged only against party-visible evidence, so
    hidden content can neither support its claims nor leak into verification.
    """
    summary_rank = visibility_rank(summary_visibility)
    return [e for e in events if visibility_rank(e.visibility) >= summary_rank]


def _allowed_records(
    facts: Any, relations: Any, summary_visibility: str,
) -> tuple[list[Any], list[Any]]:
    """Repaired records the verifier may see (same visibility rule as events)."""
    summary_rank = visibility_rank(summary_visibility)
    return (
        [f for f in facts if visibility_rank(f.visibility) >= summary_rank],
        [r for r in relations if visibility_rank(r.visibility) >= summary_rank],
    )


# ── Bounded verification frames (one claim → SUPPORTED/UNSUPPORTED/DEFER) ────

def build_claim_frame(
    claim: dict[str, Any],
    evidence_events: list[CampaignDomainEvent],
    *,
    record_excerpts: list[dict[str, Any]] | None = None,
    from_sequence: int,
    to_sequence: int,
) -> DecisionFrame:
    records = (
        CandidateRecord(
            id=SUPPORTED,
            label="Claim is supported by the authorized evidence",
            source="summary:verify_outcome",
        ),
        CandidateRecord(
            id=UNSUPPORTED,
            label="Claim is not supported by the authorized evidence",
            source="summary:verify_outcome",
        ),
        CandidateRecord(
            id=DEFER,
            label="Evidence is ambiguous; defer the claim",
            source="summary:verify_outcome", risk="low",
        ),
    )
    return build_frame(
        decision_class=SUMMARY_DECISION_CLASS,
        question_id=SUMMARY_QUESTION_ID,
        instructions=SUMMARY_FRAME_INSTRUCTIONS,
        state={
            "frame_schema": SUMMARY_FRAME_SCHEMA_VERSION,
            "claim": {
                "id": claim["id"], "text": claim["text"],
                "source_from": claim["source_from"],
                "source_to": claim["source_to"],
            },
            "evidence": _evidence_excerpt(evidence_events) + list(record_excerpts or []),
            "source_range": [from_sequence, to_sequence],
        },
        state_revision=f"summary:{from_sequence}-{to_sequence}:{claim['id']}",
        candidates=records,
        include_escapes=False,
    )


@dataclass
class ClaimVerdict:
    selected_id: str
    failure: str | None = None
    provider: str | None = None
    model: str | None = None


def decide_claim(
    frame: DecisionFrame, service: DecisionService, *, session_factory: Any = None,
) -> ClaimVerdict:
    """Run one bounded claim-support judgment (reuses the #217 executor).

    Decision-plane failures, unknown candidates, and policy escalation all
    resolve to recorded DEFER — never guessed support. The decision record
    is persisted fail-soft under the ``campaign_summary_verify`` role,
    separate from the generative role.
    """
    from app.post_turn.materialize import decide_assertion

    decision = decide_assertion(frame, service, session_factory=session_factory)
    return ClaimVerdict(
        selected_id=decision.selected_id, failure=decision.failure,
        provider=decision.provider, model=decision.model,
    )


# ── Consolidation ────────────────────────────────────────────────────────────

def _load_range(
    db: Session, campaign_id: uuid.UUID, from_sequence: int, to_sequence: int,
) -> list[CampaignDomainEvent]:
    if from_sequence < 1 or to_sequence < from_sequence:
        raise SummaryError(
            f"invalid summary range {from_sequence}-{to_sequence}")
    events = list(db.execute(
        select(CampaignDomainEvent)
        .where(CampaignDomainEvent.campaign_id == campaign_id,
               CampaignDomainEvent.sequence >= from_sequence,
               CampaignDomainEvent.sequence <= to_sequence)
        .order_by(CampaignDomainEvent.sequence.asc())
    ).scalars().all())
    if not events:
        raise SummaryError(
            f"no committed events in range {from_sequence}-{to_sequence}")
    seqs = [e.sequence for e in events]
    if seqs != list(range(from_sequence, to_sequence + 1)):
        missing = sorted(set(range(from_sequence, to_sequence + 1)) - set(seqs))
        raise SummaryError(
            f"summary range {from_sequence}-{to_sequence} has gaps "
            f"(missing={missing}); refusing to compress")
    return events


def _get_or_create_row(
    db: Session, campaign_id: uuid.UUID,
    from_sequence: int, to_sequence: int, visibility: str,
) -> tuple[CampaignSummary, bool]:
    row = db.execute(select(CampaignSummary).where(
        CampaignSummary.campaign_id == campaign_id,
        CampaignSummary.scope == SUMMARY_SCOPE_RUNNING,
        CampaignSummary.from_sequence == from_sequence,
        CampaignSummary.to_sequence == to_sequence,
    )).scalars().first()
    if row is not None:
        return row, False
    row = CampaignSummary(
        id=uuid.uuid4(), campaign_id=campaign_id, scope=SUMMARY_SCOPE_RUNNING,
        from_sequence=from_sequence, to_sequence=to_sequence,
        source_revision=to_sequence, status="pending", visibility=visibility,
    )
    db.add(row)
    db.flush()
    return row, True


def _refresh_embeddings_for_range(
    db: Session, campaign_id: uuid.UUID, events: list[CampaignDomainEvent],
) -> dict[str, int]:
    """Trigger #213 embedding refresh from source-version changes.

    Collects the authoritative rows actually touched in the range (facts /
    relations citing a range event, plus the range events themselves) and
    routes them through the async index hook. Best-effort: never raises, so
    derived index work cannot threaten authoritative state.
    """
    counts = {"facts": 0, "relations": 0, "domain_events": 0}
    try:
        from app.world import semantic as _semantic
        from models.world import WorldFact, WorldRelation

        event_ids = {e.id for e in events}
        facts = db.execute(select(WorldFact).where(
            WorldFact.campaign_id == campaign_id,
            WorldFact.source_event_id.in_(event_ids),
        )).scalars().all()
        relations = db.execute(select(WorldRelation).where(
            WorldRelation.campaign_id == campaign_id,
            WorldRelation.source_event_id.in_(event_ids),
        )).scalars().all()
        if facts:
            _semantic.note_authoritative_write(
                db, campaign_id,
                [("world_fact", f.id) for f in facts])
            counts["facts"] = len(facts)
        if relations:
            _semantic.note_authoritative_write(
                db, campaign_id,
                [("world_relation", r.id) for r in relations])
            counts["relations"] = len(relations)
        for event in events:
            _semantic.request_semantic_index(
                db, campaign_id, "domain_event", event.id)
        counts["domain_events"] = len(events)
    except Exception as exc:
        logger.warning("summary embedding refresh failed error=%s", exc)
    return counts


def consolidate_summary_for_range(
    db: Session,
    campaign: Campaign,
    from_sequence: int,
    to_sequence: int,
    *,
    visibility: str = "campaign",
    provider: SummaryProvider | None = None,
    decision_service: DecisionService | None = None,
    session_factory: Any = None,
    operation_id: str | None = None,
    commit: bool = True,
    max_regenerations: int = 1,
) -> dict[str, Any]:
    """Generate, validate, and verify one running summary over a range.

    Generation proposes; deterministic code validates; bounded decisions
    verify. Unsupported claims reject/regenerate (never silently become
    quasi-canon); uncertain claims defer/escalate (never treated as
    supported). Re-summarization over unchanged sources converges (same
    prose/claims, version untouched, rebuild counted) rather than drifting.

    Generation/verification failures persist a retryable ``failed`` /
    ``deferred`` / ``pending`` row and return — they never raise, never
    delete source records, and never invalidate committed gameplay. Only
    caller errors (empty/gappy range, bad visibility) raise SummaryError.
    """
    from app.world.service import normalize_visibility

    try:
        visibility = normalize_visibility(visibility)
    except ValueError as exc:
        raise SummaryError(f"invalid summary visibility: {exc}") from exc
    events = _load_range(db, campaign.id, from_sequence, to_sequence)
    facts, relations = _range_records(db, campaign.id, {e.id for e in events})
    source_hash = _source_hash(events, facts, relations)
    # Generation and verification share one visibility-filtered snapshot:
    # after an inline repair the generator sees the corrected records (and
    # explicit retraction markers), so a rebuild can regenerate corrected
    # prose instead of merely failing stale prose.
    allowed_facts, allowed_relations = _allowed_records(
        facts, relations, visibility)
    record_excerpts = _record_excerpts(allowed_facts, allowed_relations)
    evidence = _evidence_excerpt(events) + record_excerpts

    row, _created = _get_or_create_row(
        db, campaign.id, from_sequence, to_sequence, visibility)
    row.visibility = visibility
    prior_prose = row.prose
    if operation_id is not None:
        row.operation_id = operation_id[:128]

    # Convergent re-summarization: unchanged sources + already current stays
    # current (rebuild counted, version untouched) — no drift, no dup rows.
    if row.status == "current" and row.source_hash == source_hash:
        row.rebuild_count = int(row.rebuild_count or 0) + 1
        row.error = None
        if commit:
            db.commit()
        else:
            db.flush()
        structured_log(
            logger, logging.INFO, "campaign_summary_converged",
            campaign_id=str(campaign.id), summary_id=str(row.id),
            scope=row.scope, source_range=[from_sequence, to_sequence],
            version=row.version, role=GENERATION_ROLE,
        )
        return {"status": "current", "converged": True,
                "version": row.version, "summary_id": str(row.id)}

    generate = provider or default_stub_provider
    feedback: str | None = None
    distribution: dict[str, int] = {SUPPORTED: 0, UNSUPPORTED: 0, DEFER: 0}
    verification_model: str | None = None
    generations = 0

    for attempt in range(max(0, int(max_regenerations)) + 1):
        generations += 1
        try:
            draft = generate(
                evidence, from_sequence=from_sequence,
                to_sequence=to_sequence, visibility=visibility,
                attempt=attempt, feedback=feedback,
            )
        except Exception as exc:
            row.status = "failed"
            row.error = f"generation_failed: {type(exc).__name__}: {exc}"[:2000]
            row.source_hash = source_hash
            row.source_revision = to_sequence
            if commit:
                db.commit()
            else:
                db.flush()
            structured_log(
                logger, logging.WARNING, "campaign_summary_generation_failed",
                campaign_id=str(campaign.id), summary_id=str(row.id),
                attempt=attempt, error=row.error, role=GENERATION_ROLE,
            )
            return {"status": "failed", "reason": "generation_failed",
                    "attempt": attempt, "summary_id": str(row.id),
                    "error": row.error}
        prose = str(getattr(draft, "prose", "") or "")
        row.generation_provider = str(getattr(draft, "provider", "unknown"))[:64]
        row.generation_model = str(getattr(draft, "model", "unknown"))[:128]
        try:
            row.generation_latency_ms = int(float(getattr(draft, "latency_ms", 0.0) or 0.0))
        except (TypeError, ValueError):
            row.generation_latency_ms = 0

        claims = split_claims(prose)
        validated, failures = deterministic_validate(
            claims, events, visibility, prose=prose,
            facts=facts, relations=relations)
        if failures:
            # Deterministic rejection wins regardless of any semantic judge
            # output — the judge is not even consulted on this draft.
            row.status = "failed"
            row.prose = prose[:MAX_PROSE_CHARS]
            row.claims = []
            row.claim_count = 0
            row.deterministic_failures = int(row.deterministic_failures or 0) + len(failures)
            row.unsupported_count = 0
            row.uncertain_count = 0
            row.support_distribution = dict(distribution)
            row.verification_policy = dict(VERIFICATION_POLICY)
            row.error = f"deterministic_rejection: {'; '.join(failures)}"[:2000]
            row.source_hash = source_hash
            row.source_revision = to_sequence
            if commit:
                db.commit()
            else:
                db.flush()
            structured_log(
                logger, logging.WARNING, "campaign_summary_deterministic_rejection",
                campaign_id=str(campaign.id), summary_id=str(row.id),
                failures=failures, role=GENERATION_ROLE,
            )
            return {"status": "failed", "reason": "deterministic_rejection",
                    "failures": failures, "attempt": attempt,
                    "summary_id": str(row.id)}

        if decision_service is None:
            # Generated but unverified: retryable, never context-eligible.
            row.status = "pending"
            row.prose = prose[:MAX_PROSE_CHARS]
            row.claims = validated
            row.claim_count = len(validated)
            row.support_distribution = dict(distribution)
            row.verification_policy = dict(VERIFICATION_POLICY)
            row.error = "awaiting_verification: no decision service supplied"
            row.source_hash = source_hash
            row.source_revision = to_sequence
            if commit:
                db.commit()
            else:
                db.flush()
            structured_log(
                logger, logging.INFO, "campaign_summary_pending_verification",
                campaign_id=str(campaign.id), summary_id=str(row.id),
                claim_count=len(validated), role=GENERATION_ROLE,
            )
            return {"status": "pending", "reason": "awaiting_verification",
                    "claim_count": len(validated), "summary_id": str(row.id)}

        allowed = _allowed_evidence(events, visibility)
        unsupported: list[str] = []
        uncertain: list[str] = []
        for claim in validated:
            frame = build_claim_frame(
                claim, allowed, record_excerpts=record_excerpts,
                from_sequence=from_sequence, to_sequence=to_sequence)
            verdict = decide_claim(
                frame, decision_service, session_factory=session_factory)
            distribution[verdict.selected_id] = (
                distribution.get(verdict.selected_id, 0) + 1)
            if verification_model is None and verdict.model:
                verification_model = str(verdict.model)[:128]
            claim["verdict"] = verdict.selected_id
            claim["reason"] = verdict.failure
            if verdict.selected_id == UNSUPPORTED:
                unsupported.append(claim["id"])
            elif verdict.selected_id != SUPPORTED:
                uncertain.append(claim["id"])

        row.claims = validated
        row.claim_count = len(validated)
        row.unsupported_count = len(unsupported)
        row.uncertain_count = len(uncertain)
        row.support_distribution = dict(distribution)
        row.verification_policy = dict(VERIFICATION_POLICY)
        row.verification_model = verification_model
        row.source_hash = source_hash
        row.source_revision = to_sequence

        if unsupported:
            feedback = (
                f"regenerate without unsupported claims: "
                f"{'; '.join(unsupported)}"
            )
            if attempt >= max(0, int(max_regenerations)):
                row.status = "failed"
                row.prose = prose[:MAX_PROSE_CHARS]
                row.error = (
                    f"unsupported_claims_rejected: {'; '.join(unsupported)}"
                )[:2000]
                if commit:
                    db.commit()
                else:
                    db.flush()
                structured_log(
                    logger, logging.WARNING, "campaign_summary_unsupported_rejected",
                    campaign_id=str(campaign.id), summary_id=str(row.id),
                    unsupported=unsupported,
                    distribution=dict(distribution), role=VERIFICATION_ROLE,
                )
                return {"status": "failed",
                        "reason": "unsupported_claims_rejected",
                        "unsupported": unsupported, "attempt": attempt,
                        "summary_id": str(row.id)}
            continue
        if uncertain:
            row.status = "deferred"
            row.prose = prose[:MAX_PROSE_CHARS]
            row.error = (
                f"uncertain_claims_deferred: {'; '.join(uncertain)}; "
                f"escalate per policy rather than treating as supported"
            )[:2000]
            if commit:
                db.commit()
            else:
                db.flush()
            structured_log(
                logger, logging.INFO, "campaign_summary_deferred",
                campaign_id=str(campaign.id), summary_id=str(row.id),
                uncertain=uncertain,
                distribution=dict(distribution), role=VERIFICATION_ROLE,
            )
            return {"status": "deferred", "reason": "uncertain_claims",
                    "uncertain": uncertain, "attempt": attempt,
                    "summary_id": str(row.id)}
        # All claims SUPPORTED: eligible for forward-DM context.
        row.status = "current"
        new_prose = prose[:MAX_PROSE_CHARS]
        row.prose = new_prose
        row.error = None
        if prior_prose is not None and prior_prose != new_prose:
            row.version = int(row.version or 1) + 1
        row.rebuild_count = int(row.rebuild_count or 0) + 1
        embedding_refresh = _refresh_embeddings_for_range(db, campaign.id, events)
        if commit:
            db.commit()
        else:
            db.flush()
        structured_log(
            logger, logging.INFO, "campaign_summary_verified",
            campaign_id=str(campaign.id), summary_id=str(row.id),
            version=row.version, claim_count=len(validated),
            generations=generations, distribution=dict(distribution),
            embedding_refresh=embedding_refresh, role=VERIFICATION_ROLE,
        )
        return {"status": "current", "version": row.version,
                "claim_count": len(validated), "generations": generations,
                "distribution": dict(distribution),
                "embedding_refresh": embedding_refresh,
                "summary_id": str(row.id)}
    # Unreachable (loop always returns), kept fail-closed.
    raise SummaryError("summary consolidation exhausted without a verdict")


# ── Repair / retcon invalidation ─────────────────────────────────────────────

def mark_summaries_stale(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    from_sequence: int | None = None,
    to_sequence: int | None = None,
    reason: str = "source_repaired",
    commit: bool = True,
) -> int:
    """Invalidate affected summaries after a source repair/retcon.

    Rows whose source range overlaps [from_sequence, to_sequence] (or all
    rows when no bounds are given) move current/pending/deferred → stale
    with the stale counter bumped. Stale rows are rebuildable derived work:
    the authoritative repair itself is untouched.
    """
    query = select(CampaignSummary).where(
        CampaignSummary.campaign_id == campaign_id,
        CampaignSummary.status.in_(("current", "pending", "deferred")),
    )
    if from_sequence is not None and to_sequence is not None:
        query = query.where(
            CampaignSummary.from_sequence <= to_sequence,
            CampaignSummary.to_sequence >= from_sequence,
        )
    rows = db.execute(query).scalars().all()
    for row in rows:
        row.status = "stale"
        row.stale_count = int(row.stale_count or 0) + 1
        row.error = f"stale: {reason}"[:2000]
        db.add(row)
    if rows:
        if commit:
            db.commit()
        else:
            db.flush()
        structured_log(
            logger, logging.INFO, "campaign_summaries_marked_stale",
            campaign_id=str(campaign_id), count=len(rows), reason=reason,
        )
    return len(rows)


def note_source_repair(
    db: Session | None,
    campaign_id: Any,
    *,
    from_sequence: int | None = None,
    to_sequence: int | None = None,
    reason: str = "source_repaired",
) -> int:
    """Best-effort writer hook: repairs/retcons invalidate affected summaries.

    Never raises — derived invalidation must not break canon commits.
    """
    if db is None:
        return 0
    try:
        return mark_summaries_stale(
            db, campaign_id,  # type: ignore[arg-type]
            from_sequence=from_sequence, to_sequence=to_sequence,
            reason=reason, commit=False,
        )
    except Exception as exc:
        logger.warning("summary repair note failed error=%s", exc)
        return 0


def rebuild_stale_summaries(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    provider: SummaryProvider | None = None,
    decision_service: DecisionService | None = None,
    session_factory: Any = None,
    operation_id: str | None = None,
    limit: int = 10,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Rebuild stale summaries so revalidation converges after leaks/repairs."""
    rows = list(db.execute(
        select(CampaignSummary)
        .where(CampaignSummary.campaign_id == campaign_id,
               CampaignSummary.status == "stale")
        .order_by(CampaignSummary.from_sequence.asc())
        .limit(max(1, int(limit)))
    ).scalars().all())
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            outcomes.append({"summary_id": str(row.id), "status": "skipped",
                             "reason": "campaign_not_found"})
            continue
        try:
            outcome = consolidate_summary_for_range(
                db, campaign, row.from_sequence, row.to_sequence,
                visibility=row.visibility, provider=provider,
                decision_service=decision_service,
                session_factory=session_factory,
                operation_id=operation_id, commit=commit,
            )
        except SummaryError as exc:
            outcome = {"status": "failed", "reason": "rebuild_error",
                       "error": str(exc)[:500], "summary_id": str(row.id)}
        outcome["summary_id"] = str(row.id)
        outcomes.append(outcome)
    return outcomes


# ── Context-assembler access (lower-authority lane) ──────────────────────────

def _context_uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_campaign_member(db: Session, campaign: Campaign, viewer_id: uuid.UUID) -> bool:
    """Owner-or-member gate mirroring retrieval/clocks player-facing reads."""
    if campaign.owner_id == viewer_id:
        return True
    from app.campaigns.service import is_campaign_member

    try:
        return bool(is_campaign_member(db, campaign.id, viewer_id))
    except Exception:
        return False


def get_valid_summaries_for_context(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    viewer_user_id: Any = None,
    dm_internal: bool = False,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Current valid summaries as explicitly lower-authority context.

    Only ``current`` (fully verified) rows are eligible. Player-facing
    callers must supply a viewer with campaign membership (owner counts);
    unknown campaigns, missing viewers, and non-members get nothing — an
    arbitrary user UUID plus a known campaign ID must not read that
    campaign's summaries. DM-internal callers keep visibility metadata for
    later projection. Every item carries ``authority: "derived_summary"``
    plus the stronger lanes that outrank it, so forward-DM code can
    distinguish summary evidence from direct sources at a glance.
    """
    if not dm_internal:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return []
        raw = (
            viewer_user_id
            if isinstance(viewer_user_id, (list, tuple))
            else [viewer_user_id]
        )
        member = any(
            (vid := _context_uuid(value)) is not None
            and _is_campaign_member(db, campaign, vid)
            for value in raw
        )
        if not member:
            structured_log(
                logger, logging.INFO, "campaign_summary_context_denied",
                campaign_id=str(campaign_id),
                reason=(
                    "viewer_required" if viewer_user_id is None
                    else "not_campaign_member"
                ),
            )
            return []
    rows = list(db.execute(
        select(CampaignSummary)
        .where(CampaignSummary.campaign_id == campaign_id,
               CampaignSummary.status == "current")
        .order_by(CampaignSummary.to_sequence.desc())
        .limit(max(1, min(int(limit), 20)))
    ).scalars().all())
    lane: list[dict[str, Any]] = []
    for row in rows:
        if not dm_internal and row.visibility not in _MEMBER_VISIBLE:
            continue
        lane.append({
            "summary_id": str(row.id),
            "scope": row.scope,
            "source_range": [row.from_sequence, row.to_sequence],
            "source_revision": row.source_revision,
            "source_hash": row.source_hash,
            "version": row.version,
            "visibility": row.visibility,
            "prose": row.prose,
            "claims": [
                {k: c.get(k) for k in ("id", "text", "verdict")}
                for c in (row.claims or [])
            ],
            "claim_count": row.claim_count,
            "authority": "derived_summary",
            "lane": "summary",
            "is_derived": True,
            "outranked_by": [
                "domain_event", "world_fact", "world_relation",
                "world_entity", "repair",
            ],
        })
    return lane


# ── Observability ────────────────────────────────────────────────────────────

def get_summary_stats(db: Session, campaign_id: Any) -> dict[str, Any]:
    """Summary observability: spans, roles, verdicts, staleness, backlog."""
    from app.world import retrieval as retrieval_mod

    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    rows = list(db.execute(select(CampaignSummary).where(
        CampaignSummary.campaign_id == campaign.id,
    )).scalars().all())
    by_status: dict[str, int] = {}
    distribution: dict[str, int] = {SUPPORTED: 0, UNSUPPORTED: 0, DEFER: 0}
    latest: CampaignSummary | None = None
    totals = {"claims": 0, "deterministic_failures": 0, "unsupported": 0,
              "uncertain": 0, "stale": 0, "rebuilds": 0}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        for key, value in (row.support_distribution or {}).items():
            distribution[key] = distribution.get(key, 0) + int(value or 0)
        totals["claims"] += int(row.claim_count or 0)
        totals["deterministic_failures"] += int(row.deterministic_failures or 0)
        totals["unsupported"] += int(row.unsupported_count or 0)
        totals["uncertain"] += int(row.uncertain_count or 0)
        totals["stale"] += int(row.stale_count or 0)
        totals["rebuilds"] += int(row.rebuild_count or 0)
        if latest is None or (row.to_sequence or 0) >= (latest.to_sequence or 0):
            latest = row
    embedding_backlog: dict[str, Any] | None = None
    try:
        from app.world import semantic as _semantic

        stats = _semantic.get_semantic_stats(db, campaign.id)
        embedding_backlog = {
            "stale": stats.get("stale", 0), "failed": stats.get("failed", 0),
            "oldest_unindexed_lag_seconds": stats.get(
                "oldest_unindexed_lag_seconds", 0.0),
        }
    except Exception as exc:
        embedding_backlog = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return {
        "campaign_id": str(campaign.id),
        "summaries": len(rows),
        "by_status": by_status,
        "latest_source_span": (
            [latest.from_sequence, latest.to_sequence] if latest else None
        ),
        "latest_version": latest.version if latest else None,
        "generation_role": GENERATION_ROLE,
        "latest_generation_model": latest.generation_model if latest else None,
        "latest_generation_latency_ms": (
            latest.generation_latency_ms if latest else None),
        "verification_role": VERIFICATION_ROLE,
        "verification_decision_class": SUMMARY_DECISION_CLASS,
        "support_distribution": distribution,
        "totals": totals,
        "embedding_refresh_backlog": embedding_backlog,
    }
