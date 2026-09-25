"""Post-turn deterministic + semantic consistency incidents — issue #220.

After required materialization (#217), clocks (#218), and summaries (#219),
post-turn verifies the range for contradictions between newly materialized
state, completed visible turns, clocks, current scene, facts, and summaries —
recording explicit consistency incidents instead of silently normalizing
canon away. The #221 repair workflow consumes incidents; this verifier never
rewrites canon itself.

Authority split (under #379; the AI is the only DM):

- Deterministic code owns every exact conflict: entity status/location/clock
  IDs and numbers, source/revision existence and ordering, and a closed
  lexical contradiction table for derived prose. These checks run with no
  model call, and a positive semantic verdict never overrides them.
- Bounded decisions (``post_turn_consistency`` class, #380/#381 runtime)
  judge only the ambiguous semantic residue — paraphrased/implicit
  contradictions that cannot be decided structurally — among the explicit
  outcomes ``CONSISTENT`` / ``CONTRADICTION`` / ``UNCERTAIN`` (plus the
  standard OPEN_ENDED/DEFER escapes, which resolve to deferred). They can
  flag or defer, never authorize or clear a deterministic conflict.
- Newer committed gameplay is authority: a delayed hidden consequence or
  stale proposal conflicting with it creates an incident; the verifier
  never backdates an overwrite.

Lifecycle: ``open`` (deterministic or semantic contradiction, required) /
``deferred`` (uncertain/escape/decision failure or private evidence the
caller may not see — still required-unresolved) / ``verifier_failed``
(operational/retryable — still required-unresolved) → ``resolved`` (by the
#221 repair workflow only). Any required-unresolved incident keeps
:func:`is_range_complete` false so the post-turn checkpoint cannot report
complete.

Incident and decision payloads may carry private evidence: rows are
DM/operator-only by default, and visibility filtering happens before any
semantic candidate exposure.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from models.campaigns import CampaignDomainEvent
from models.post_turn import PostTurnConsistencyIncident

logger = logging.getLogger(__name__)

# ── Outcomes / decision role ───────────────────────────────────────────────

CONSISTENT = "CONSISTENT"
CONTRADICTION = "CONTRADICTION"
UNCERTAIN = "UNCERTAIN"

INCIDENT_DECISION_CLASS = "post_turn_consistency"
INCIDENT_QUESTION_ID = "verify_consistency_pair"
INCIDENT_FRAME_INSTRUCTIONS = (
    "Select only the supplied outcome that matches the authorized evidence. "
    "CONTRADICTION requires the new claim to directly contradict the canon "
    "claim; paraphrase still counts when the meaning conflicts. CONSISTENT "
    "requires the claims to be compatible. When the evidence is insufficient "
    "or ambiguous, select UNCERTAIN. Never invent facts outside the supplied "
    "evidence."
)
INCIDENT_FRAME_SCHEMA_VERSION = 1

register_policy(DecisionClassPolicy(
    decision_class=INCIDENT_DECISION_CLASS,
    min_probability_direct=.85, min_confidence_direct=.8,
    min_margin_direct=.25, near_tie_margin=.25,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))

INCIDENT_POLICY = {
    "decision_class": INCIDENT_DECISION_CLASS,
    "question_id": INCIDENT_QUESTION_ID,
    "frame_schema": INCIDENT_FRAME_SCHEMA_VERSION,
    "uncertain_action": "defer_escalate",
    "positive_overrides_deterministic": False,
}

# ── Incident vocabulary ────────────────────────────────────────────────────

# Statuses that keep a range incomplete (required-unresolved).
UNRESOLVED_STATUSES = frozenset({"open", "deferred", "verifier_failed"})
RESOLVED_STATUS = "resolved"

DETERMINISTIC = "deterministic"
SEMANTIC = "semantic"
OPERATIONAL = "operational"

# Deterministic incident types (code-owned, never delegated).
VISIBLE_TURN_CONFLICT = "visible_turn_conflict"
SCENE_LOCATION_CONFLICT = "scene_location_conflict"
STALE_HIDDEN_CONSEQUENCE = "stale_hidden_consequence"
CLOCK_CONTRADICTION = "clock_contradiction"
FACT_CONFLICT = "fact_conflict"
SOURCE_CONFLICT = "source_conflict"
SUMMARY_CONTRADICTION = "summary_contradiction"
# Semantic / operational types.
SEMANTIC_CONTRADICTION = "semantic_contradiction"
SEMANTIC_DEFERRED = "semantic_deferred"
VERIFIER_FAILURE = "verifier_failure"

_MEMBER_VISIBLE = frozenset({"campaign", "public"})


class ConsistencyBlocked(RuntimeError):
    """Required unresolved consistency incidents keep the range incomplete.

    Raised by the post-turn pipeline integration so the checkpoint stays
    put for cumulative retry after #221 repair. Carries the incident ids.
    """

    def __init__(self, incident_ids: list[str], detail: str = ""):
        self.incident_ids = list(incident_ids)
        super().__init__(
            f"post-turn consistency blocked by {len(incident_ids)} unresolved "
            f"incident(s): {detail[:300]}"
        )


# ── Inputs ─────────────────────────────────────────────────────────────────

@dataclass
class ProposedWrite:
    """One delayed/post-turn write proposal to check against committed canon.

    ``kind`` is one of ``entity_status`` / ``scene_location`` / ``clock`` /
    ``fact``. ``target`` identifies canon: an entity id/name, a clock id/name,
    or a fact id/idempotency key. ``value`` carries the proposed payload
    (e.g. ``{"status": "alive"}``). ``source_sequence`` is the committed
    sequence the proposal was derived from — older than current canon order
    means stale. ``visibility`` gates semantic exposure.
    """

    kind: str
    target: str
    value: dict[str, Any] = field(default_factory=dict)
    source_sequence: int | None = None
    visibility: str = "dm_only"


@dataclass
class SemanticPair:
    """One ambiguous claim pair for bounded semantic judgment.

    Both sides are already-authorized records/claims; the judge only selects
    among CONSISTENT / CONTRADICTION / UNCERTAIN (+ escapes). ``target_ref``
    links the pair to a deterministic target so a positive semantic result
    can be checked against (never override) deterministic findings.
    """

    pair_id: str
    canon_claim: dict[str, Any]
    new_claim: dict[str, Any]
    target_ref: str = ""
    context: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────

def _normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _incident_key(incident_type: str, target: str, fingerprint: str,
                  from_sequence: int, to_sequence: int) -> str:
    raw = f"{incident_type}:{_normalize(target)}:{_normalize(fingerprint)}:{from_sequence}-{to_sequence}"
    return f"pt220-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _event_by_id(events: list[CampaignDomainEvent]) -> dict[Any, CampaignDomainEvent]:
    return {e.id: e for e in events}


def _max_sequence(db: Session, campaign_id: uuid.UUID) -> int:
    from sqlalchemy import func as _func

    mx = db.execute(
        select(_func.max(CampaignDomainEvent.sequence)).where(
            CampaignDomainEvent.campaign_id == campaign_id)
    ).scalar()
    return int(mx) if mx is not None else 0


def _load_range_events(db: Session, campaign_id: uuid.UUID,
                       from_sequence: int, to_sequence: int) -> list[CampaignDomainEvent]:
    return list(db.execute(
        select(CampaignDomainEvent)
        .where(CampaignDomainEvent.campaign_id == campaign_id,
               CampaignDomainEvent.sequence >= from_sequence,
               CampaignDomainEvent.sequence <= to_sequence)
        .order_by(CampaignDomainEvent.sequence.asc())
    ).scalars().all())


def _resolve_entity(db: Session, campaign_id: uuid.UUID, ref: Any):
    """Exact entity resolution (deterministic; no model calls)."""
    from app.world.identity import exact_identity_match

    try:
        entity, _how = exact_identity_match(db, campaign_id, ref)
    except Exception:
        return None
    return entity


def _resolve_clock(db: Session, campaign_id: uuid.UUID, ref: Any):
    from models.world import CampaignClock

    try:
        row = db.get(CampaignClock, uuid.UUID(str(ref)))
        if row is not None and row.campaign_id == campaign_id:
            return row
    except (ValueError, AttributeError, TypeError):
        pass
    rows = db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == campaign_id,
        CampaignClock.name == str(ref or "").strip(),
    )).scalars().all()
    return rows[0] if rows else None


# Closed lexical contradiction table (code-owned): exact state-word pairs
# whose co-occurrence about the same named subject is a deterministic
# contradiction. Paraphrase ("passed away") is NOT here — that residue goes
# through bounded semantic judgment.
_STATE_OPPOSITES: tuple[tuple[str, str], ...] = (
    ("dead", "alive"),
    ("dead", "living"),
    ("dead", "survived"),
    ("destroyed", "intact"),
    ("destroyed", "standing"),
    ("sealed", "open"),
    ("present", "absent"),
)


def _word_present(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text or "") is not None


def lexical_contradiction(text_a: Any, text_b: Any, *, subject: str = "") -> bool:
    """Closed-table contradiction check between two prose strings.

    Requires the same subject token (when given) plus opposing state words.
    Deterministic and conservative: returns False on any ambiguity.
    """
    a, b = _normalize(text_a), _normalize(text_b)
    if not a or not b or a == b:
        return False
    if subject and (_normalize(subject) not in a or _normalize(subject) not in b):
        return False
    for left, right in _STATE_OPPOSITES:
        if ((_word_present(a, left) and _word_present(b, right))
                or (_word_present(a, right) and _word_present(b, left))):
            return True
    return False


# ── Persistence (idempotent) ───────────────────────────────────────────────

def _store_incident(
    db: Session, campaign_id: uuid.UUID,
    from_sequence: int, to_sequence: int,
    *, incident_type: str, category: str, severity: str, status: str,
    detection_path: str, target: str, fingerprint: str,
    evidence: dict[str, Any], affected: list[dict[str, Any]],
    decision_policy: dict[str, Any] | None = None,
    decision_model: str | None = None,
    decision_distribution: dict[str, int] | None = None,
    latency_ms: int | None = None,
    operation_id: str | None = None,
    error: str | None = None,
    commit: bool = True,
) -> tuple[PostTurnConsistencyIncident, bool]:
    """Idempotent incident creation keyed by (campaign, conflict, range).

    Returns (row, created). Re-verification reuses the row, bumps
    ``repeat_count``, and refreshes latency — never duplicates.
    """
    key = _incident_key(incident_type, target, fingerprint, from_sequence, to_sequence)
    existing = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == campaign_id,
        PostTurnConsistencyIncident.incident_key == key,
    )).scalars().first()
    if existing is not None:
        if existing.status != RESOLVED_STATUS and status != RESOLVED_STATUS:
            existing.status = status
        existing.repeat_count = int(existing.repeat_count or 0) + 1
        if latency_ms is not None:
            existing.detection_latency_ms = latency_ms
        if error:
            existing.error = error[:2000]
        db.add(existing)
        db.flush()
        return existing, False
    row = PostTurnConsistencyIncident(
        id=uuid.uuid4(), campaign_id=campaign_id,
        from_sequence=from_sequence, to_sequence=to_sequence,
        incident_key=key, incident_type=incident_type, category=category,
        severity=severity, status=status, detection_path=detection_path,
        evidence=dict(evidence or {}), affected_records=list(affected or []),
        decision_policy=dict(decision_policy or {}) if decision_policy else {},
        decision_model=decision_model,
        decision_distribution=dict(decision_distribution or {}) if decision_distribution else {},
        detection_latency_ms=latency_ms, repeat_count=1,
        operation_id=operation_id[:128] if operation_id else None,
        error=error[:2000] if error else None,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # Lost a race with a concurrent verifier for the same conflict.
        dup = db.execute(select(PostTurnConsistencyIncident).where(
            PostTurnConsistencyIncident.campaign_id == campaign_id,
            PostTurnConsistencyIncident.incident_key == key,
        )).scalars().first()
        if dup is None:
            raise
        dup.repeat_count = int(dup.repeat_count or 0) + 1
        db.add(dup)
        db.flush()
        return dup, False
    if commit:
        db.commit()
    else:
        db.flush()
    return row, True


# ── Deterministic detectors (no model calls) ───────────────────────────────

def _canon_order_for_entity(entity: Any) -> int:
    """Newest committed order backing an entity row (revision channel)."""
    try:
        return int(entity.revision or 0)
    except (TypeError, ValueError):
        return 0


def detect_proposal_conflicts(
    db: Session, campaign_id: uuid.UUID,
    events: list[CampaignDomainEvent],
    proposals: list[ProposedWrite],
    *, max_sequence: int,
) -> list[dict[str, Any]]:
    """Exact conflicts between proposals and committed canon (deterministic).

    Every conflict cites the exact canon record (ids/revisions/numbers) and
    treats newer committed gameplay as authority — the proposal is stale,
    never the canon.
    """
    from app.world.service import get_current_scene

    findings: list[dict[str, Any]] = []
    by_id = _event_by_id(events)
    for proposal in proposals or []:
        kind = str(proposal.kind or "").strip()
        source_order = proposal.source_sequence if proposal.source_sequence is not None else -1
        if kind == "entity_status":
            entity = _resolve_entity(db, campaign_id, proposal.target)
            if entity is None:
                findings.append({
                    "incident_type": SOURCE_CONFLICT, "category": "source",
                    "severity": "standard", "target": proposal.target,
                    "fingerprint": f"unknown-entity:{proposal.target}",
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": dict(proposal.value),
                                     "source_sequence": proposal.source_sequence},
                        "reason": "proposal target matches no canonical entity",
                    },
                    "affected": [{"kind": "proposal", "target": proposal.target}],
                })
                continue
            wanted = str(proposal.value.get("status") or "").strip().lower()
            current = str(entity.status or "").strip().lower()
            if wanted and wanted != current:
                backing = None
                if entity.source_event_id is not None and entity.source_event_id in by_id:
                    backing = by_id[entity.source_event_id]
                findings.append({
                    "incident_type": VISIBLE_TURN_CONFLICT
                    if (backing is not None and backing.visibility in _MEMBER_VISIBLE)
                    else FACT_CONFLICT,
                    "category": "canon_conflict",
                    "severity": "high",
                    "target": str(entity.id),
                    "fingerprint": f"entity-status:{current}-vs-{wanted}",
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": dict(proposal.value),
                                     "source_sequence": proposal.source_sequence},
                        "canon": {"entity_id": str(entity.id), "name": entity.name,
                                  "status": entity.status,
                                  "revision": _canon_order_for_entity(entity),
                                  "source_event_id": str(entity.source_event_id)
                                  if entity.source_event_id else None,
                                  "backing_event_visibility": backing.visibility
                                  if backing is not None else None},
                        "newer_committed_is_authority": True,
                        "stale_proposal": source_order < _canon_order_for_entity(entity),
                    },
                    "affected": [{"kind": "world_entity", "id": str(entity.id),
                                  "status": entity.status}],
                })
        elif kind == "scene_location":
            scene = get_current_scene(db, campaign_id)
            if scene is None:
                continue
            canon_id = str(scene.location_entity_id) if scene.location_entity_id else None
            canon_name = _normalize(scene.location_name)
            want_id = proposal.value.get("location_entity_id")
            want_name = _normalize(proposal.value.get("location_name"))
            conflict = False
            if want_id is not None and canon_id is not None:
                try:
                    conflict = uuid.UUID(str(want_id)) != uuid.UUID(str(canon_id))
                except (ValueError, AttributeError, TypeError):
                    conflict = _normalize(want_id) != _normalize(canon_id)
            elif want_name and canon_name:
                conflict = want_name != canon_name
            elif (want_id is not None) != (canon_id is not None):
                conflict = True
            if conflict:
                findings.append({
                    "incident_type": SCENE_LOCATION_CONFLICT, "category": "canon_conflict",
                    "severity": "high", "target": f"scene:{campaign_id}",
                    "fingerprint": f"location:{canon_id or canon_name}-vs-{want_id or want_name}",
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": dict(proposal.value),
                                     "source_sequence": proposal.source_sequence},
                        "canon": {"location_entity_id": canon_id,
                                  "location_name": scene.location_name,
                                  "revision": scene.revision},
                        "newer_committed_is_authority": True,
                    },
                    "affected": [{"kind": "scene", "campaign_id": str(campaign_id),
                                  "revision": scene.revision}],
                })
        elif kind == "clock":
            clock = _resolve_clock(db, campaign_id, proposal.target)
            if clock is None:
                findings.append({
                    "incident_type": SOURCE_CONFLICT, "category": "source",
                    "severity": "standard", "target": proposal.target,
                    "fingerprint": f"unknown-clock:{proposal.target}",
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": dict(proposal.value),
                                     "source_sequence": proposal.source_sequence},
                        "reason": "proposal target matches no canonical clock",
                    },
                    "affected": [{"kind": "proposal", "target": proposal.target}],
                })
                continue
            bits: list[str] = []
            if ("progress" in proposal.value
                    and int(proposal.value["progress"]) != int(clock.progress or 0)):
                bits.append(f"progress:{clock.progress}-vs-{proposal.value['progress']}")
            if ("status" in proposal.value
                    and _normalize(proposal.value["status"]) != _normalize(clock.status)):
                bits.append(f"status:{clock.status}-vs-{proposal.value['status']}")
            if bits:
                findings.append({
                    "incident_type": CLOCK_CONTRADICTION, "category": "canon_conflict",
                    "severity": "high", "target": str(clock.id),
                    "fingerprint": ";".join(bits),
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": dict(proposal.value),
                                     "source_sequence": proposal.source_sequence},
                        "canon": {"clock_id": str(clock.id), "name": clock.name,
                                  "progress": clock.progress, "threshold": clock.threshold,
                                  "status": clock.status, "revision": clock.revision,
                                  "evaluated_through_sequence":
                                      clock.evaluated_through_sequence},
                        "newer_committed_is_authority": True,
                    },
                    "affected": [{"kind": "campaign_clock", "id": str(clock.id),
                                  "progress": clock.progress, "status": clock.status}],
                })
        elif kind == "fact":
            from models.world import WorldFact

            row = None
            if proposal.value.get("fact_id") is not None:
                try:
                    row = db.get(WorldFact, uuid.UUID(str(proposal.value["fact_id"])))
                    if row is not None and row.campaign_id != campaign_id:
                        row = None
                except (ValueError, AttributeError, TypeError):
                    row = None
            if row is None and proposal.value.get("idempotency_key"):
                row = db.execute(select(WorldFact).where(
                    WorldFact.campaign_id == campaign_id,
                    WorldFact.idempotency_key == proposal.value["idempotency_key"],
                )).scalars().first()
            if row is None:
                continue
            want = _normalize(proposal.value.get("content"))
            current = _normalize(row.content)
            if want and want != current:
                stale = (source_order >= 0 and row.source_event_id is not None
                         and _event_sequence(by_id, row.source_event_id, max_sequence) > source_order)
                findings.append({
                    "incident_type": STALE_HIDDEN_CONSEQUENCE if stale else FACT_CONFLICT,
                    "category": "stale_hidden" if stale else "canon_conflict",
                    "severity": "standard", "target": str(row.id),
                    "fingerprint": f"fact-content:{hashlib.sha256(current.encode()).hexdigest()[:12]}"
                                   f"-vs-{hashlib.sha256(want.encode()).hexdigest()[:12]}",
                    "evidence": {
                        "proposal": {"kind": kind, "target": proposal.target,
                                     "value": {k: v for k, v in dict(proposal.value).items()
                                               if k != "content"},
                                     "source_sequence": proposal.source_sequence},
                        "canon": {"fact_id": str(row.id), "status": row.status,
                                  "epistemic_state": row.epistemic_state,
                                  "visibility": row.visibility,
                                  "source_event_id": str(row.source_event_id)
                                  if row.source_event_id else None},
                        "newer_committed_is_authority": True,
                    },
                    "affected": [{"kind": "world_fact", "id": str(row.id),
                                  "status": row.status}],
                })
        else:
            findings.append({
                "incident_type": SOURCE_CONFLICT, "category": "source",
                "severity": "standard", "target": proposal.target,
                "fingerprint": f"unknown-kind:{kind}",
                "evidence": {
                    "proposal": {"kind": kind, "target": proposal.target,
                                 "value": dict(proposal.value),
                                 "source_sequence": proposal.source_sequence},
                    "reason": f"unknown proposal kind {kind!r}",
                },
                "affected": [{"kind": "proposal", "target": proposal.target}],
            })
    return findings


def _event_sequence(by_id: dict[Any, CampaignDomainEvent], event_id: Any, default: int) -> int:
    event = by_id.get(event_id)
    if event is not None:
        return int(event.sequence or 0)
    return default


def detect_canon_self_conflicts(
    db: Session, campaign_id: uuid.UUID,
    events: list[CampaignDomainEvent],
    *, max_sequence: int,
) -> list[dict[str, Any]]:
    """Internal canon contradictions computable with no proposals (deterministic).

    Covers the deterministic clock contradiction (progress past threshold
    while still evaluable), dangling source refs, missing visibility
    metadata on range records, and impossible summary revisions.
    """
    from models.world import CampaignClock, CampaignSummary, WorldFact, WorldRelation
    from app.world.clocks import CLOCK_EVALUABLE_STATUSES

    findings: list[dict[str, Any]] = []
    known_ids = {e.id for e in events}
    # Clock invariant: progress past threshold while still evaluable.
    clocks = db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == campaign_id,
        CampaignClock.status.in_(sorted(CLOCK_EVALUABLE_STATUSES)),
    )).scalars().all()
    for clock in clocks:
        if int(clock.progress or 0) > int(clock.threshold or 1):
            findings.append({
                "incident_type": CLOCK_CONTRADICTION, "category": "canon_conflict",
                "severity": "high", "target": str(clock.id),
                "fingerprint": f"over-threshold:{clock.progress}-gt-{clock.threshold}",
                "evidence": {
                    "canon": {"clock_id": str(clock.id), "name": clock.name,
                              "progress": clock.progress, "threshold": clock.threshold,
                              "status": clock.status, "revision": clock.revision},
                    "reason": "clock progress exceeds threshold while still evaluable",
                },
                "affected": [{"kind": "campaign_clock", "id": str(clock.id),
                              "progress": clock.progress, "status": clock.status}],
            })
    # Range-linked record hygiene: source existence + visibility metadata.
    for record in (*db.execute(select(WorldFact).where(
            WorldFact.campaign_id == campaign_id,
            WorldFact.source_event_id.in_(known_ids),
    )).scalars().all(), *db.execute(select(WorldRelation).where(
            WorldRelation.campaign_id == campaign_id,
            WorldRelation.source_event_id.in_(known_ids),
    )).scalars().all()):
        label = "world_fact" if record.__tablename__ == "world_facts" else "world_relation"
        if not getattr(record, "visibility", None):
            findings.append({
                "incident_type": SOURCE_CONFLICT, "category": "source",
                "severity": "standard", "target": str(record.id),
                "fingerprint": f"missing-visibility:{label}",
                "evidence": {"canon": {"kind": label, "id": str(record.id)},
                             "reason": "range record missing visibility metadata"},
                "affected": [{"kind": label, "id": str(record.id)}],
            })
    # Dangling source refs on active truth rows (any source, must exist).
    for label, model in (("world_fact", WorldFact), ("world_relation", WorldRelation)):
        rows = db.execute(select(model).where(
            model.campaign_id == campaign_id,
            model.status == "active",
            model.source_event_id.is_not(None),
        )).scalars().all()
        for row in rows:
            exists = db.get(CampaignDomainEvent, row.source_event_id) is not None
            if not exists:
                findings.append({
                    "incident_type": SOURCE_CONFLICT, "category": "source",
                    "severity": "standard", "target": str(row.id),
                    "fingerprint": f"dangling-source:{row.source_event_id}",
                    "evidence": {
                        "canon": {"kind": label, "id": str(row.id),
                                  "source_event_id": str(row.source_event_id)},
                        "reason": "active truth row cites a source event that does not exist",
                    },
                    "affected": [{"kind": label, "id": str(row.id)}],
                })
    # Impossible summary revisions: derived rows cannot outrun canon.
    summaries = db.execute(select(CampaignSummary).where(
        CampaignSummary.campaign_id == campaign_id,
    )).scalars().all()
    for summary in summaries:
        if int(summary.source_revision or 0) > max_sequence:
            findings.append({
                "incident_type": SOURCE_CONFLICT, "category": "source",
                "severity": "standard", "target": str(summary.id),
                "fingerprint": f"summary-revision-ahead:{summary.source_revision}-gt-{max_sequence}",
                "evidence": {
                    "canon": {"summary_id": str(summary.id),
                              "source_range": [summary.from_sequence, summary.to_sequence],
                              "source_revision": summary.source_revision,
                              "max_committed_sequence": max_sequence},
                    "reason": "derived summary revision exceeds committed canon",
                },
                "affected": [{"kind": "campaign_summary", "id": str(summary.id)}],
            })
    return findings


def detect_summary_contradictions(
    db: Session, campaign_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Deterministic contradictions between current summaries and canon facts.

    Uses only the closed lexical table over a shared named subject — derived
    prose is checked even though it is lower-authority. Paraphrased conflicts
    are out of scope here; they belong to the semantic path.
    """
    from models.world import CampaignSummary, WorldFact, WorldEntity

    findings: list[dict[str, Any]] = []
    summaries = db.execute(select(CampaignSummary).where(
        CampaignSummary.campaign_id == campaign_id,
        CampaignSummary.status == "current",
    )).scalars().all()
    if not summaries:
        return findings
    facts = db.execute(select(WorldFact).where(
        WorldFact.campaign_id == campaign_id,
        WorldFact.status == "active",
        WorldFact.epistemic_state == "confirmed",
    )).scalars().all()
    if not facts:
        return findings
    names = [e.name for e in db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == campaign_id,
        WorldEntity.superseded_by_id.is_(None),
    )).scalars().all() if e.name]
    for summary in summaries:
        for claim in (summary.claims or []):
            text = claim.get("text") if isinstance(claim, dict) else str(claim)
            if not text:
                continue
            for fact in facts:
                subject = next(
                    (n for n in names
                     if _normalize(n) in _normalize(text)
                     and _normalize(n) in _normalize(fact.content)),
                    "",
                )
                if not subject:
                    continue
                if lexical_contradiction(text, fact.content, subject=subject):
                    findings.append({
                        "incident_type": SUMMARY_CONTRADICTION, "category": "summary",
                        "severity": "standard", "target": str(summary.id),
                        "fingerprint": f"claim-vs-fact:{claim.get('id') if isinstance(claim, dict) else '?'}"
                                       f":{fact.id}",
                        "evidence": {
                            "canon": {"fact_id": str(fact.id),
                                      "visibility": fact.visibility,
                                      "epistemic_state": fact.epistemic_state},
                            "derived": {"summary_id": str(summary.id),
                                        "source_range": [summary.from_sequence,
                                                         summary.to_sequence]},
                            "reason": "current summary claim deterministically "
                                      "contradicts a confirmed canon fact",
                        },
                        "affected": [{"kind": "campaign_summary", "id": str(summary.id)},
                                     {"kind": "world_fact", "id": str(fact.id)}],
                    })
    return findings


# ── Bounded semantic judgments (ambiguous residue only) ────────────────────

def _pair_visibility(pair: SemanticPair) -> str:
    from app.world.service import normalize_visibility

    ranks = {"dm_only": 0, "private": 1, "campaign": 2, "public": 3}
    sides = [pair.canon_claim.get("visibility"), pair.new_claim.get("visibility")]
    try:
        return min(sides, key=lambda v: ranks[normalize_visibility(v)])
    except (ValueError, KeyError, TypeError):
        return "dm_only"


def build_consistency_frame(pair: SemanticPair) -> DecisionFrame:
    """Bounded frame over exactly one authorized claim pair (escapes kept)."""
    records = (
        CandidateRecord(id=CONSISTENT,
                        label="Claims are compatible; no contradiction",
                        source="post-turn:consistency_outcome"),
        CandidateRecord(id=CONTRADICTION,
                        label="New claim contradicts the canon claim",
                        source="post-turn:consistency_outcome"),
        CandidateRecord(id=UNCERTAIN,
                        label="Evidence is ambiguous; defer for repair",
                        source="post-turn:consistency_outcome", risk="low"),
    )
    return build_frame(
        decision_class=INCIDENT_DECISION_CLASS,
        question_id=INCIDENT_QUESTION_ID,
        instructions=INCIDENT_FRAME_INSTRUCTIONS,
        state={
            "frame_schema": INCIDENT_FRAME_SCHEMA_VERSION,
            "pair_id": pair.pair_id,
            "target_ref": pair.target_ref,
            "canon_claim": {
                "text": str(pair.canon_claim.get("text") or "")[:2000],
                "record_ref": pair.canon_claim.get("record_ref"),
                "visibility": pair.canon_claim.get("visibility"),
            },
            "new_claim": {
                "text": str(pair.new_claim.get("text") or "")[:2000],
                "record_ref": pair.new_claim.get("record_ref"),
                "visibility": pair.new_claim.get("visibility"),
            },
            "context": str(pair.context or "")[:1000],
        },
        state_revision=f"post-turn-consistency:{pair.pair_id}",
        candidates=records,
        include_escapes=True,
    )


@dataclass
class PairVerdict:
    selected_id: str
    failure: str | None = None
    record: Any = None
    provider: str | None = None
    model: str | None = None


def decide_pair(frame: DecisionFrame, service: DecisionService,
                *, session_factory: Any = None) -> PairVerdict:
    """Run one bounded consistency judgment (mirrors #217 decide_assertion).

    Decision-plane failures, unknown candidates, policy escalation, and the
    explicit escapes all resolve to recorded UNCERTAIN — never guessed
    consistency.
    """
    try:
        response = service.decide(to_decision_request(frame))
    except Exception as exc:
        logger.warning("consistency judgment failed error=%s", exc)
        return PairVerdict(UNCERTAIN, failure=f"{type(exc).__name__}: {exc}"[:500])
    result = response.results.get(frame.question_id)
    if not isinstance(result, ChoiceResult):
        logger.warning("consistency judgment missing choice result")
        return PairVerdict(UNCERTAIN, failure="malformed: missing choice result",
                           provider=response.provider, model=response.model)
    if is_escape_id(result.selected_id) or result.selected_id == UNCERTAIN:
        record = build_record(
            frame, result, evaluate_execution(
                frame, result.selected_id, dict(result.probabilities),
                result.confidence, verified=True),
            provider=response.provider, model=response.model or "unknown",
            mode=ACTIVE, trace_id=response.trace_id,
            operation_id=response.operation_id, campaign_id=None,
            latency_ms=response.latency_ms, verified=True)
        record_fail_soft(session_factory, record)
        return PairVerdict(UNCERTAIN, record=record,
                           provider=response.provider, model=response.model)
    try:
        if result.selected_id not in {c.id for c in frame.candidates}:
            raise ValueError(f"unknown candidate {result.selected_id!r}")
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True)
    except Exception as exc:
        logger.warning("consistency judgment policy failed error=%s", exc)
        return PairVerdict(UNCERTAIN, failure=f"{type(exc).__name__}: {exc}"[:500],
                           provider=response.provider, model=response.model)
    record = build_record(
        frame, result, verdict, provider=response.provider,
        model=response.model or "unknown", mode=ACTIVE,
        trace_id=response.trace_id, operation_id=response.operation_id,
        campaign_id=None, latency_ms=response.latency_ms, verified=True)
    record_fail_soft(session_factory, record)
    if verdict.directive == ESCALATE or result.selected_id != CONTRADICTION:
        # Only an executed CONTRADICTION becomes a semantic incident; an
        # executed CONSISTENT clears nothing on its own (the caller checks
        # deterministic findings first).
        if verdict.directive == ESCALATE:
            return PairVerdict(UNCERTAIN, record=record,
                               provider=response.provider, model=response.model)
        return PairVerdict(result.selected_id, record=record,
                           provider=response.provider, model=response.model)
    return PairVerdict(CONTRADICTION, record=record,
                       provider=response.provider, model=response.model)


# ── Verification entry point ───────────────────────────────────────────────

def verify_post_turn_consistency(
    db: Session,
    campaign_id: uuid.UUID,
    from_sequence: int,
    to_sequence: int,
    *,
    proposed: list[ProposedWrite] | None = None,
    semantic_pairs: list[SemanticPair] | None = None,
    decision_service: DecisionService | None = None,
    session_factory: Any = None,
    operation_id: str | None = None,
    dm_internal: bool = True,
    commit: bool = True,
    durable_session_factory: Any = None,
) -> dict[str, Any]:
    """Verify one post-turn range and persist consistency incidents.

    Deterministic checks run with no model call; ambiguous semantic pairs go
    through the bounded runtime with visibility filtering applied first. A
    CONSISTENT semantic verdict never clears a deterministic conflict on the
    same target. Never raises for pair-level failures — they persist as
    deferred incidents. Only a catastrophic verifier failure raises (after
    recording an operational incident), so the range is never silently
    marked complete.

    ``durable_session_factory`` (when supplied) owns incident persistence on
    an independent transaction while detection keeps reading the caller's
    flushed-but-uncommitted ``db`` state: the verifier then never commits
    ``db`` itself, so a blocked range rolls back the caller's consolidation
    writes while incidents stay durable for repair. The caller owns ``db``'s
    transaction (commit on success, rollback on ``ConsistencyBlocked``).
    Without it, ``commit`` controls ``db`` directly (legacy behavior).
    """
    started = time.monotonic()
    campaign_id = campaign_id if isinstance(campaign_id, uuid.UUID) else uuid.UUID(str(campaign_id))
    durable_db: Session | None = (
        durable_session_factory() if durable_session_factory is not None else None
    )
    store_db = durable_db if durable_db is not None else db
    max_seq = _max_sequence(db, campaign_id)
    events = _load_range_events(db, campaign_id, from_sequence, to_sequence)
    stored: list[PostTurnConsistencyIncident] = []
    distribution: dict[str, int] = {CONSISTENT: 0, CONTRADICTION: 0, UNCERTAIN: 0}
    semantic_model: str | None = None
    deterministic_targets: set[str] = set()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        deterministic_findings = detect_proposal_conflicts(
            db, campaign_id, events, list(proposed or []), max_sequence=max_seq)
        deterministic_findings += detect_canon_self_conflicts(
            db, campaign_id, events, max_sequence=max_seq)
        deterministic_findings += detect_summary_contradictions(db, campaign_id)
        for finding in deterministic_findings:
            row, _created = _store_incident(
                store_db, campaign_id, from_sequence, to_sequence,
                incident_type=finding["incident_type"], category=finding["category"],
                severity=finding["severity"], status="open",
                detection_path=DETERMINISTIC,
                target=finding["target"], fingerprint=finding["fingerprint"],
                evidence=finding["evidence"], affected=finding["affected"],
                decision_policy=dict(INCIDENT_POLICY),
                latency_ms=_elapsed_ms(), operation_id=operation_id, commit=False)
            stored.append(row)
            deterministic_targets.add(_normalize(finding["target"]))
            structured_log(
                logger, logging.WARNING, "post_turn_consistency_deterministic",
                campaign_id=str(campaign_id),
                source_range=[from_sequence, to_sequence],
                incident_type=finding["incident_type"], target=finding["target"],
            )
        # Semantic residue: bounded judgments over authorized pairs only.
        for pair in semantic_pairs or []:
            pair_vis = _pair_visibility(pair)
            if not dm_internal and pair_vis not in _MEMBER_VISIBLE:
                # Visibility filtering before exposure: private evidence is
                # never sent to the judge from a non-DM caller — the pair
                # stays unresolved instead.
                row, _c = _store_incident(
                    store_db, campaign_id, from_sequence, to_sequence,
                    incident_type=SEMANTIC_DEFERRED, category="semantic",
                    severity="standard", status="deferred",
                    detection_path=SEMANTIC,
                    target=pair.target_ref or pair.pair_id,
                    fingerprint=f"visibility-deferred:{pair.pair_id}",
                    evidence={"pair_id": pair.pair_id,
                              "reason": "private evidence withheld from non-DM judge",
                              "pair_visibility": pair_vis},
                    affected=[{"kind": "semantic_pair", "pair_id": pair.pair_id}],
                    decision_policy=dict(INCIDENT_POLICY),
                    latency_ms=_elapsed_ms(), operation_id=operation_id, commit=False)
                stored.append(row)
                distribution[UNCERTAIN] += 1
                continue
            if decision_service is None:
                row, _c = _store_incident(
                    store_db, campaign_id, from_sequence, to_sequence,
                    incident_type=SEMANTIC_DEFERRED, category="semantic",
                    severity="standard", status="deferred",
                    detection_path=SEMANTIC,
                    target=pair.target_ref or pair.pair_id,
                    fingerprint=f"no-judge:{pair.pair_id}",
                    evidence={"pair_id": pair.pair_id,
                              "reason": "no decision service supplied; pair stays unresolved"},
                    affected=[{"kind": "semantic_pair", "pair_id": pair.pair_id}],
                    decision_policy=dict(INCIDENT_POLICY),
                    latency_ms=_elapsed_ms(), operation_id=operation_id, commit=False)
                stored.append(row)
                distribution[UNCERTAIN] += 1
                continue
            frame = build_consistency_frame(pair)
            verdict = decide_pair(frame, decision_service, session_factory=session_factory)
            distribution[verdict.selected_id] = distribution.get(verdict.selected_id, 0) + 1
            if semantic_model is None and verdict.model:
                semantic_model = str(verdict.model)[:128]
            pair_policy = dict(INCIDENT_POLICY)
            if verdict.selected_id == CONTRADICTION:
                row, _c = _store_incident(
                    store_db, campaign_id, from_sequence, to_sequence,
                    incident_type=SEMANTIC_CONTRADICTION, category="semantic",
                    severity="standard", status="open",
                    detection_path=SEMANTIC,
                    target=pair.target_ref or pair.pair_id,
                    fingerprint=f"pair:{pair.pair_id}",
                    evidence={"pair_id": pair.pair_id,
                              "canon_record": pair.canon_claim.get("record_ref"),
                              "new_record": pair.new_claim.get("record_ref")},
                    affected=[{"kind": "semantic_pair", "pair_id": pair.pair_id}],
                    decision_policy=pair_policy, decision_model=verdict.model,
                    decision_distribution={verdict.selected_id: 1},
                    latency_ms=_elapsed_ms(), operation_id=operation_id, commit=False)
                stored.append(row)
                structured_log(
                    logger, logging.WARNING, "post_turn_consistency_semantic",
                    campaign_id=str(campaign_id),
                    source_range=[from_sequence, to_sequence],
                    pair_id=pair.pair_id, outcome=CONTRADICTION,
                )
            elif verdict.selected_id == CONSISTENT:
                if _normalize(pair.target_ref or pair.pair_id) in deterministic_targets:
                    # A positive semantic result cannot override a
                    # deterministic conflict on the same target — recorded
                    # in telemetry, deterministic incident stands.
                    structured_log(
                        logger, logging.WARNING, "post_turn_consistency_semantic_overridden",
                        campaign_id=str(campaign_id),
                        source_range=[from_sequence, to_sequence],
                        pair_id=pair.pair_id,
                        reason="deterministic_conflict_stands",
                    )
                else:
                    structured_log(
                        logger, logging.INFO, "post_turn_consistency_pair_consistent",
                        campaign_id=str(campaign_id),
                        source_range=[from_sequence, to_sequence],
                        pair_id=pair.pair_id,
                    )
            else:
                row, _c = _store_incident(
                    store_db, campaign_id, from_sequence, to_sequence,
                    incident_type=SEMANTIC_DEFERRED, category="semantic",
                    severity="standard", status="deferred",
                    detection_path=SEMANTIC,
                    target=pair.target_ref or pair.pair_id,
                    fingerprint=f"uncertain:{pair.pair_id}",
                    evidence={"pair_id": pair.pair_id,
                              "reason": verdict.failure or "judge deferred",
                              "judge_outcome": UNCERTAIN},
                    affected=[{"kind": "semantic_pair", "pair_id": pair.pair_id}],
                    decision_policy=pair_policy, decision_model=verdict.model,
                    decision_distribution={UNCERTAIN: 1},
                    latency_ms=_elapsed_ms(), operation_id=operation_id, commit=False)
                stored.append(row)
    except Exception as exc:
        # Verifier failure is operational/retryable — never a silent complete.
        try:
            row, _c = _store_incident(
                store_db, campaign_id, from_sequence, to_sequence,
                incident_type=VERIFIER_FAILURE, category="operational",
                severity="operational", status="verifier_failed",
                detection_path=OPERATIONAL,
                target=f"range:{from_sequence}-{to_sequence}",
                fingerprint=f"verifier-failure:{type(exc).__name__}",
                evidence={"source_range": [from_sequence, to_sequence]},
                affected=[],
                latency_ms=_elapsed_ms(), operation_id=operation_id,
                error=f"{type(exc).__name__}: {exc}", commit=False)
            stored.append(row)
        except Exception:
            pass
        try:
            if durable_db is not None:
                # Incidents stay durable for repair; the caller's
                # transaction is only flushed — its owner rolls back
                # the failed range's consolidation writes.
                durable_db.commit()
                db.flush()
            elif commit:
                db.commit()
            else:
                db.flush()
        except Exception:
            pass
        finally:
            if durable_db is not None:
                durable_db.close()
        logger.warning("post_turn consistency verifier failed campaign=%s range=%s-%s error=%s",
                       campaign_id, from_sequence, to_sequence, exc)
        raise

    if durable_db is not None:
        # Materialize payloads before the durable commit (a caller factory
        # may expire attributes on commit), then commit incidents on their
        # own transaction. The caller's session is flush-only: it must not
        # become durable ahead of the range's atomic checkpoint commit.
        incidents = [r.to_dict() for r in stored]
        try:
            durable_db.commit()
        except Exception:
            try:
                durable_db.rollback()
            except Exception:
                pass
            durable_db.close()
            logger.warning("post_turn consistency durable commit failed campaign=%s range=%s-%s",
                           campaign_id, from_sequence, to_sequence)
            raise
        durable_db.close()
        db.flush()
    else:
        if commit:
            db.commit()
        else:
            db.flush()
        incidents = [r.to_dict() for r in stored]
    unresolved = [i for i in incidents if i["status"] in UNRESOLVED_STATUSES]
    complete = not unresolved and is_range_complete(
        db, campaign_id, from_sequence, to_sequence)
    structured_log(
        logger, logging.INFO if complete else logging.WARNING,
        "post_turn_consistency_verified",
        campaign_id=str(campaign_id), source_range=[from_sequence, to_sequence],
        incidents=len(incidents), unresolved=len(unresolved), complete=complete,
        decision_distribution=dict(distribution),
        decision_model=semantic_model,
        detection_latency_ms=_elapsed_ms(),
    )
    return {
        "complete": complete,
        "from_sequence": from_sequence,
        "to_sequence": to_sequence,
        "incidents": incidents,
        "unresolved": len(unresolved),
        "decision_distribution": dict(distribution),
        "decision_model": semantic_model,
        "detection_latency_ms": _elapsed_ms(),
    }


def is_range_complete(db: Session, campaign_id: uuid.UUID,
                      from_sequence: int, to_sequence: int) -> bool:
    """False when a required unresolved incident remains for the range."""
    rows = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == campaign_id,
        PostTurnConsistencyIncident.from_sequence == from_sequence,
        PostTurnConsistencyIncident.to_sequence == to_sequence,
        PostTurnConsistencyIncident.status.in_(sorted(UNRESOLVED_STATUSES)),
    )).scalars().all()
    return len(rows) == 0


def list_unresolved_incidents(db: Session, campaign_id: uuid.UUID, *,
                              dm_internal: bool = True) -> list[dict[str, Any]]:
    """Repair-facing incident listing (#221 consumes this).

    DM-internal callers get full evidence; anyone else gets redacted
    type/severity/status metadata only (private evidence never leaks to
    player-facing correction decisions made here).
    """
    rows = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == campaign_id,
        PostTurnConsistencyIncident.status.in_(sorted(UNRESOLVED_STATUSES)),
    ).order_by(PostTurnConsistencyIncident.created_at.asc())).scalars().all()
    return [r.to_dict(include_evidence=dm_internal) for r in rows]


def resolve_incident(db: Session, incident_id: uuid.UUID, *,
                     resolution: str = "repaired",
                     operation_id: str | None = None,
                     commit: bool = True) -> PostTurnConsistencyIncident:
    """Mark one incident resolved (repair-workflow hook for #221)."""
    from datetime import datetime, timezone

    row = db.get(PostTurnConsistencyIncident, incident_id)
    if row is None:
        raise ValueError(f"consistency incident {incident_id} not found")
    row.status = RESOLVED_STATUS
    row.error = (f"resolved: {resolution}"[:2000]) if resolution else row.error
    if operation_id:
        row.operation_id = operation_id[:128]
    row.resolved_at = datetime.now(timezone.utc)
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    structured_log(
        logger, logging.INFO, "post_turn_consistency_resolved",
        campaign_id=str(row.campaign_id), incident_id=str(row.id),
        incident_type=row.incident_type, resolution=resolution,
    )
    return row


def get_consistency_stats(db: Session, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Observability: categories, paths, records, ranges, decisions, latency."""
    rows = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == campaign_id,
    )).scalars().all()
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_path: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    distribution: dict[str, int] = {}
    models: dict[str, int] = {}
    latencies: list[int] = []
    repeated = 0
    verifier_failures = 0
    resolution_seconds: list[float] = []
    for row in rows:
        by_type[row.incident_type] = by_type.get(row.incident_type, 0) + 1
        by_category[row.category or "unknown"] = by_category.get(row.category or "unknown", 0) + 1
        by_path[row.detection_path] = by_path.get(row.detection_path, 0) + 1
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_severity[row.severity or "unknown"] = by_severity.get(row.severity or "unknown", 0) + 1
        for key, value in (row.decision_distribution or {}).items():
            distribution[key] = distribution.get(key, 0) + int(value or 0)
        if row.decision_model:
            models[row.decision_model] = models.get(row.decision_model, 0) + 1
        if row.detection_latency_ms is not None:
            latencies.append(int(row.detection_latency_ms))
        repeated += max(0, int(row.repeat_count or 1) - 1)
        if row.incident_type == VERIFIER_FAILURE:
            verifier_failures += 1
        if row.status == RESOLVED_STATUS and row.resolved_at and row.created_at:
            try:
                resolution_seconds.append(
                    max(0.0, (row.resolved_at - row.created_at).total_seconds()))
            except TypeError:
                pass
    affected = sum(len(r.affected_records or []) for r in rows)
    return {
        "campaign_id": str(campaign_id),
        "incidents": len(rows),
        "unresolved": sum(by_status.get(s, 0) for s in UNRESOLVED_STATUSES),
        "by_type": by_type,
        "by_category": by_category,
        "by_detection_path": by_path,
        "by_status": by_status,
        "by_severity": by_severity,
        "affected_records": affected,
        "decision_class": INCIDENT_DECISION_CLASS,
        "decision_distribution": distribution,
        "decision_models": models,
        "decision_policy": dict(INCIDENT_POLICY),
        "repeated_incidents": repeated,
        "verifier_failures": verifier_failures,
        "avg_detection_latency_ms": (
            sum(latencies) / len(latencies) if latencies else None),
        "avg_time_to_resolution_seconds": (
            sum(resolution_seconds) / len(resolution_seconds)
            if resolution_seconds else None),
    }
