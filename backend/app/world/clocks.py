"""Criteria-driven campaign clocks / pressures — issue #218.

A clock is durable directional state that advances only when committed
gameplay evidence satisfies its explicit criteria. Deterministic code owns
everything authoritative:

- the clock definition, current stage, allowed transitions, criteria,
  evidence candidates, visibility, revision, and mutation;
- the versioned decision frame offered to the semantic judge, containing
  only the legal next outcomes;
- revision revalidation and idempotent application of the outcome.

The decision model judges which legal outcome best matches the evidence for
``semantic`` criteria and never invents a transition amount, stage, or
effect outside the code-supplied candidates. Purely mechanical
(event-count) criteria resolve without any model call.

Failure posture (fail closed, never guessed advancement):

- invalid criteria / dangling source refs: rejected at creation;
- invalid evidence refs at apply time: rejected, range fails;
- decision-plane failure (provider/config/timeout/malformed): explicit
  DEFER outcome recorded with the failure kind — the range is consumed, the
  checkpoint still advances, and the deferral stays visible for later
  repair instead of wedging all post-turn consolidation;
- stale frame (clock revision moved before application): rejected via
  :class:`ClockStaleError`; the clock is skipped this pass with its
  watermark untouched so a later range re-evaluates the same evidence;
- any other exception during required processing: propagates so the
  post-turn run fails and the range is retried cumulatively.

The AI Dungeon Master is the only DM in this product; no clock copy may
imply a separate human DM or moderator. This module never advances state
from wall-clock time: progress moves only against committed domain-event
evidence, so real-world idle time can never tick a clock.

Conventions mirror ``app.world.npcs`` (writers run inside the caller's
revision transaction) and ``app.world.identity`` (bounded decision
integration + telemetry).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schema import coerce_uuid
from app.campaigns.events import commit_campaign_mutation
from app.campaigns.service import lock_campaign_row
from app.decisions import (
    ACTIVE,
    ESCALATE,
    CandidateRecord,
    ChoiceResult,
    DecisionClassPolicy,
    DecisionError,
    DecisionFrame,
    DecisionService,
    build_frame,
    build_record,
    evaluate_execution,
    is_escape_id,
    record_fail_soft,
    register_policy,
    revalidate_for_execution,
    shared_trace,
    to_decision_request,
)
from app.idempotency import compose_operation_id
from app.observability.tracing import structured_log
from app.visibility.access import is_campaign_participant, is_world_authority
from app.visibility.policy import RESTRICTED_VISIBILITIES, visible_to_viewer, world_event_visibility
from app.world._common import require_provenance
from models.campaigns import Campaign, CampaignDomainEvent
from models.world import (
    CLOCK_EVALUABLE_STATUSES,
    CLOCK_STATUSES,
    CLOCK_TERMINAL_STATUSES,
    CLOCK_VISIBILITIES,
    CampaignClock,
)

logger = logging.getLogger(__name__)

# ── Decision role ──────────────────────────────────────────────────────────

CLOCK_DECISION_CLASS = "campaign_clock"
CLOCK_QUESTION_ID = "evaluate_campaign_clock"
CLOCK_FRAME_INSTRUCTIONS = (
    "Select only the supplied legal outcome whose criteria best match the "
    "evidence. Advancement requires explicit evidence satisfying the stated "
    "criteria; when evidence is insufficient or ambiguous, defer. Never "
    "select a transition amount, stage, or effect outside the supplied set."
)
CLOCK_FRAME_SCHEMA_VERSION = 1

NO_CHANGE = "NO_CHANGE"
COMPLETE = "COMPLETE"
DEFER = "DEFER"

_ADVANCE_RE = re.compile(r"^ADVANCE_(\d+)$")

# Domain events recording clock lifecycle. No-change / deferral outcomes
# emit no domain event (they change no fictional state); they are recorded
# in decision telemetry plus the post-turn run result instead.
CLOCK_ADVANCED_EVENT = "clock.advanced"
CLOCK_COMPLETED_EVENT = "clock.completed"

# Bound on evidence refs serialized into one decision state payload.
MAX_EVIDENCE_REFS = 25

# Clock lifecycle bookkeeping shares the campaign sequence with gameplay
# but is never gameplay evidence (see collect_evidence).
CLOCK_LIFECYCLE_EVENT_TYPES = frozenset({
    CLOCK_ADVANCED_EVENT, CLOCK_COMPLETED_EVENT,
})

register_policy(DecisionClassPolicy(
    decision_class=CLOCK_DECISION_CLASS,
    # Conservative posture for story-consequential pressure calls: a
    # confident, high-margin selection executes; near-ties and weak
    # selections escalate to open AI-DM adjudication (recorded as deferral).
    min_probability_direct=0.85,
    min_confidence_direct=0.8,
    min_margin_direct=0.25,
    near_tie_margin=0.25,
    near_tie_behavior=ESCALATE,
    allow_direct_when_irreversible=False,
    max_risk_for_direct="standard",
    min_probability_primer=0.5,
    min_confidence_primer=0.45,
))


class ClockStaleError(DecisionError):
    """A clock decision frame moved behind the authoritative clock revision."""

    def __init__(self, clock_id: Any, frame_revision: Any, current_revision: Any) -> None:
        super().__init__(
            f"clock {clock_id} decision is stale: enumerated at revision "
            f"{frame_revision!r}, current is {current_revision!r}",
            kind="stale",
        )
        self.clock_id = clock_id
        self.frame_revision = frame_revision
        self.current_revision = current_revision


#: Caller-owned veto on revision-bumping clock commits: returns a deferral
#: reason, or ``None`` to allow. Called once unlocked before any model work
#: and again under the campaign row lock immediately before the commit.
ClockCommitGate = Callable[[Session, uuid.UUID], "str | None"]


class ClockCommitDeferred(Exception):
    """A clock commit must wait; nothing was applied and the range retries."""

    def __init__(self, campaign_id: Any, reason: str) -> None:
        super().__init__(f"clock commit deferred for campaign {campaign_id}: {reason}")
        self.campaign_id = campaign_id
        self.reason = reason


# ── Validation (fail closed at creation) ───────────────────────────────────

_CRITERION_KINDS = frozenset({"deterministic", "semantic"})


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _non_empty_str(value: Any, field: str, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer")
    return text


def _event_type_list(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty list of event types or omitted")
    cleaned: list[str] = []
    for entry in value:
        text = str(entry or "").strip()
        if not text or len(text) > 64:
            raise ValueError(f"{field} entries must be 1-64 character event type strings")
        cleaned.append(text)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(cleaned)


def _match_map(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} must be a non-empty object of payload paths or omitted")
    cleaned: dict[str, Any] = {}
    for path, expected in value.items():
        if not isinstance(path, str) or not path.strip() or len(path) > 128:
            raise ValueError(f"{field} keys must be 1-128 character dotted payload paths")
        if expected is None or isinstance(expected, (dict, list)):
            raise ValueError(f"{field}[{path!r}] must be a scalar, not a container")
        if isinstance(expected, bool):
            raise ValueError(f"{field}[{path!r}] must not be a boolean")
        cleaned[path.strip()] = expected
    return cleaned


def validate_advancement_criteria(value: Any) -> dict[str, Any]:
    """Validate the advancement criteria DSL; fail closed on anything else."""
    if not isinstance(value, dict):
        raise ValueError("advancement_criteria must be an object")
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in _CRITERION_KINDS:
        raise ValueError(f"advancement_criteria.kind must be one of {sorted(_CRITERION_KINDS)}")
    event_types = _event_type_list(value.get("event_types"), "advancement_criteria.event_types")
    match = _match_map(value.get("match"), "advancement_criteria.match")
    required_count = value.get("required_count", 1)
    max_advance = value.get("max_advance", 1)
    criteria = {
        "kind": kind,
        "event_types": list(event_types),
        "match": match,
        "required_count": _positive_int(required_count, "advancement_criteria.required_count"),
        "max_advance": _positive_int(max_advance, "advancement_criteria.max_advance"),
    }
    # Semantic advancement says what counts as the pressure gaining ground,
    # so the judge weighs the fiction rather than counting events.
    if value.get("description") is not None:
        criteria["description"] = _non_empty_str(value.get("description"), "advancement_criteria.description", limit=2000)
    return criteria


def validate_completion_criteria(value: Any) -> dict[str, Any] | None:
    """Validate optional semantic completion criteria.

    Mechanical completion (progress reaching threshold) needs no criteria;
    these declare the judged completion path that offers the COMPLETE
    candidate to the decision model.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("completion_criteria must be an object or omitted")
    kind = str(value.get("kind") or "").strip().lower()
    if kind != "semantic":
        raise ValueError("completion_criteria.kind must be 'semantic' (mechanical completion is progress >= threshold)")
    return {
        "kind": kind,
        "description": _non_empty_str(value.get("description"), "completion_criteria.description", limit=2000),
        "event_types": list(_event_type_list(value.get("event_types"), "completion_criteria.event_types")),
        "match": _match_map(value.get("match"), "completion_criteria.match"),
    }


def validate_stages(value: Any, threshold: int) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("stages must be a non-empty list or omitted")
    seen: set[int] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("stages entries must be objects with at/stage labels")
        at = _positive_int(entry.get("at"), "stages[].at")
        if at > threshold:
            raise ValueError(f"stages[].at ({at}) must not exceed the threshold ({threshold})")
        if at in seen:
            raise ValueError("stages[].at must be unique")
        seen.add(at)
        cleaned.append({"at": at, "label": _non_empty_str(entry.get("label"), "stages[].label", limit=160)})
    return sorted(cleaned, key=lambda stage: stage["at"])


def _check_source_refs(
    db: Session, campaign_id: uuid.UUID, *, turn_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None, event_id: uuid.UUID | None,
) -> None:
    """Fail closed on dangling source refs (existence + campaign match).

    Unlike NPC standalone writes there is no committed-status gate: the
    world-seed flow creates the initial pressure before any gameplay has
    committed, so existence-in-campaign is the authority check here.
    """
    from models.campaigns import CampaignDomainEvent as _Event
    from models.dm import DmTurn, DmTurnAttempt

    if event_id is not None:
        event = db.get(_Event, event_id)
        if event is None or event.campaign_id != campaign_id:
            raise ValueError(f"source_event {event_id} not found in campaign {campaign_id}")
    if turn_id is not None:
        turn = db.get(DmTurn, turn_id)
        if turn is None or turn.campaign_id != campaign_id:
            raise ValueError(f"source_turn {turn_id} not found in campaign {campaign_id}")
    if attempt_id is not None:
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is None or attempt.campaign_id != campaign_id:
            raise ValueError(f"source_attempt {attempt_id} not found in campaign {campaign_id}")
        if turn_id is not None and str(attempt.turn_id) != str(turn_id):
            raise ValueError(f"source_attempt {attempt_id} does not belong to source_turn {turn_id}")


# ── Creation ───────────────────────────────────────────────────────────────

def get_clock(db: Session, campaign_id: Any, clock_id: Any) -> CampaignClock | None:
    row = db.get(CampaignClock, coerce_uuid(clock_id, field="clock_id"))
    return row if row is not None and row.campaign_id == coerce_uuid(campaign_id, field="campaign_id") else None


def get_clock_strict(db: Session, campaign_id: uuid.UUID, clock_id: uuid.UUID) -> CampaignClock:
    row = get_clock(db, campaign_id, clock_id)
    if row is None:
        raise ValueError(f"clock {clock_id} not found in campaign {campaign_id}")
    return row


def _validate_new_clock(
    *, name: Any, status: Any, progress: Any, threshold: Any, stages: Any,
    advancement_criteria: Any, completion_criteria: Any, completion_effect: Any,
    visibility: Any,
) -> dict[str, Any]:
    cleaned_name = _non_empty_str(name, "name", limit=160)
    cleaned_status = str(status or "pending").strip().lower()
    if cleaned_status not in CLOCK_STATUSES:
        raise ValueError(f"status must be one of {sorted(CLOCK_STATUSES)}")
    cleaned_threshold = _positive_int(threshold, "threshold")
    cleaned_progress = progress if progress is not None else 0
    if isinstance(cleaned_progress, bool) or not isinstance(cleaned_progress, int) or cleaned_progress < 0:
        raise ValueError("progress must be a non-negative integer")
    if cleaned_progress > cleaned_threshold:
        raise ValueError("progress must not exceed the threshold")
    if cleaned_progress >= cleaned_threshold and cleaned_status != "completed":
        raise ValueError("a clock at its threshold must be created completed")
    if cleaned_status == "completed" and cleaned_progress < cleaned_threshold:
        raise ValueError("a completed clock must be at its threshold")
    cleaned_visibility = str(visibility or "dm_only").strip().lower()
    if cleaned_visibility not in CLOCK_VISIBILITIES:
        raise ValueError(f"visibility must be one of {sorted(CLOCK_VISIBILITIES)}")
    if completion_effect is not None and not isinstance(completion_effect, dict):
        raise ValueError("completion_effect must be an object or omitted")
    return {
        "name": cleaned_name,
        "status": cleaned_status,
        "progress": cleaned_progress,
        "threshold": cleaned_threshold,
        "stages": validate_stages(stages, cleaned_threshold),
        "advancement_criteria": validate_advancement_criteria(advancement_criteria),
        "completion_criteria": validate_completion_criteria(completion_criteria),
        "completion_effect": dict(completion_effect) if completion_effect is not None else None,
        "visibility": cleaned_visibility,
    }


def create_clock(
    db: Session, campaign: Campaign, *, name: Any, threshold: Any,
    advancement_criteria: Any, status: Any = "pending", progress: Any = None,
    stages: Any = None, completion_criteria: Any = None, completion_effect: Any = None,
    visibility: Any = "dm_only", provenance: dict | None = None,
    source_turn_id: Any = None, source_attempt_id: Any = None,
    source_event_id: Any = None, operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[CampaignClock, bool]:
    """Create a clock inside the caller's transaction (flushes, never commits).

    Returns ``(row, created)``: a duplicate ``idempotency_key`` reuses the
    existing row (after verifying it describes the same clock) instead of
    inserting. The world-seed flow composes this with campaign creation.
    """
    cid = coerce_uuid(campaign.id, field="campaign_id")
    turn_id = coerce_uuid(source_turn_id, field="source_turn_id") if source_turn_id else None
    attempt_id = coerce_uuid(source_attempt_id, field="source_attempt_id") if source_attempt_id else None
    event_id = coerce_uuid(source_event_id, field="source_event_id") if source_event_id else None
    prov = require_provenance(provenance, subject="clock")
    _check_source_refs(db, cid, turn_id=turn_id, attempt_id=attempt_id, event_id=event_id)
    cleaned = _validate_new_clock(
        name=name, status=status, progress=progress, threshold=threshold, stages=stages,
        advancement_criteria=advancement_criteria, completion_criteria=completion_criteria,
        completion_effect=completion_effect, visibility=visibility,
    )
    key = str(idempotency_key).strip() if idempotency_key else None
    if key:
        existing = db.execute(select(CampaignClock).where(
            CampaignClock.campaign_id == cid, CampaignClock.idempotency_key == key,
        )).scalars().first()
        if existing is not None:
            if existing.name != cleaned["name"] or int(existing.threshold) != cleaned["threshold"]:
                raise ValueError("idempotency key belongs to a different clock")
            return existing, False
    row = CampaignClock(
        campaign_id=cid, operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key, provenance=prov,
        source_turn_id=turn_id, source_attempt_id=attempt_id, source_event_id=event_id,
        **cleaned,
    )
    db.add(row)
    db.flush()
    return row, True


# ── Evidence matching (deterministic, authority lane) ──────────────────────

def _payload_lookup(payload: Any, path: str) -> tuple[bool, Any]:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def event_matches(event: CampaignDomainEvent, *, event_types: tuple[str, ...], match: dict) -> bool:
    """Whether one committed event counts as evidence for a criteria clause."""
    if event_types and str(event.event_type or "") not in event_types:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    for path, expected in match.items():
        found, actual = _payload_lookup(payload, path)
        if not found or actual != expected:
            return False
    return True


def collect_evidence(
    events: list[CampaignDomainEvent], criteria: dict,
) -> tuple[list[CampaignDomainEvent], int]:
    """Split range events into (matching, total-relevant) evidence.

    ``event_types`` filters candidacy; an empty filter admits every event
    type and the ``match`` clause alone decides. Clock lifecycle events
    (``clock.advanced/completed``) never qualify: they are this feature's
    own bookkeeping in the same campaign sequence, not committed gameplay,
    so counting them would let a clock tick on its own progress. Matching ignores event visibility: post-turn evaluation runs
    in the authority lane, so DM-private gameplay can advance hidden
    clocks without disclosure.
    """
    event_types = tuple(criteria.get("event_types") or ())
    match = dict(criteria.get("match") or {})
    matching = [
        e for e in events
        if str(e.event_type or "") not in CLOCK_LIFECYCLE_EVENT_TYPES
        and event_matches(e, event_types=event_types, match=match)
    ]
    return matching, len(matching)


def evidence_refs(matching: list[CampaignDomainEvent]) -> list[dict[str, Any]]:
    """Stable, bounded evidence refs for frames, telemetry, and events."""
    refs = [
        {"sequence": int(e.sequence), "event_id": str(e.id), "event_type": str(e.event_type)}
        for e in sorted(matching, key=lambda e: int(e.sequence))
    ]
    return refs[:MAX_EVIDENCE_REFS]


#: Bounds on the story text a semantic frame carries per evidence event and
#: in total, so long ranges stay a small decision input.
STORY_CHARS_PER_EVENT = 500
STORY_CHARS_TOTAL = 6000


def _turn_story(db: Session, payload: dict[str, Any]) -> str:
    """Public beat claims and player words for one ``dm.turn_resolved`` event."""
    from models.dm import DmTurnAttempt
    from models.threads import PlayerSubmission

    parts: list[str] = []
    for raw_id in payload.get("submission_ids") or []:
        sub = db.get(PlayerSubmission, coerce_uuid(raw_id, field="submission_id"))
        if sub is not None and sub.raw_content:
            parts.append(f"Player: {sub.raw_content}")
    attempt_id = payload.get("attempt_id")
    attempt = db.get(DmTurnAttempt, coerce_uuid(attempt_id, field="attempt_id")) if attempt_id else None
    for beat in ((attempt.contract_snapshot or {}).get("beats") if attempt is not None else None) or []:
        for claim in beat.get("claims") or []:
            if claim.get("visibility", "public") == "public" and claim.get("text"):
                parts.append(f"DM: {claim['text']}")
    return " | ".join(parts)


def story_evidence(db: Session, events: list[CampaignDomainEvent]) -> list[dict[str, Any]]:
    """Evidence refs plus what happened, for a semantic judge to weigh.

    Plain refs (sequence, id, type) say nothing about the fiction, so a
    criterion like "the party decisively ends the pressure" could never be
    judged. Each ref gains its event visibility and a bounded ``story``:
    public beat claims and player words for DM turns, or a payload summary
    for world events. Newest events keep their story when the total bound
    bites. Refs stay the identifiers; stories never become evidence ids.
    """
    refs = evidence_refs(events)
    by_id = {str(e.id): e for e in events}
    budget = STORY_CHARS_TOTAL
    for ref in reversed(refs):
        event = by_id[ref["event_id"]]
        ref["visibility"] = str(event.visibility or "public")
        payload = event.payload or {}
        try:
            story = _turn_story(db, payload) if payload.get("attempt_id") else str(payload.get("summary") or "")
        except Exception:  # story is context, never a reason to fail evaluation
            story = ""
        story = story[:min(STORY_CHARS_PER_EVENT, budget)]
        if story:
            ref["story"] = story
            budget -= len(story)
    return refs


def _validate_evidence_refs(
    db: Session, campaign_id: uuid.UUID, refs: list[dict[str, Any]],
    *, from_sequence: int, to_sequence: int,
) -> None:
    """Fail closed on evidence refs outside the evaluated source range."""
    if not isinstance(refs, list):
        raise ValueError("evidence refs must be a list")
    ids: list[uuid.UUID] = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("evidence refs must be objects")
        seq = ref.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError("evidence ref sequence must be an integer")
        if not (from_sequence <= seq <= to_sequence):
            raise ValueError(f"evidence ref sequence {seq} is outside the evaluated range")
        ids.append(coerce_uuid(ref.get("event_id"), field="evidence ref event_id"))
    if not ids:
        return
    rows = db.execute(select(CampaignDomainEvent.id, CampaignDomainEvent.campaign_id).where(
        CampaignDomainEvent.id.in_(ids),
    )).all()
    found = {row_id: row_campaign for row_id, row_campaign in rows}
    for event_id in ids:
        if found.get(event_id) != campaign_id:
            raise ValueError(f"evidence event {event_id} not found in campaign {campaign_id}")


# ── Legal outcomes + versioned frames ──────────────────────────────────────

def advance_amount(selected_id: str) -> int | None:
    """Parse an ADVANCE_<n> candidate; None for non-advance outcomes."""
    match = _ADVANCE_RE.fullmatch(str(selected_id or "").strip())
    if not match:
        return None
    return int(match.group(1))


def legal_outcome_ids(
    clock: CampaignClock, *, has_advancement_evidence: bool = True,
    has_completion_evidence: bool = False,
) -> list[str]:
    """Only the legal next outcomes for the clock's current stage.

    - ``NO_CHANGE`` and ``DEFER`` (explicit open-adjudication) always,
      except a clock already at its threshold must complete, not linger;
    - ``ADVANCE_1..ADVANCE_k`` capped by criteria ``max_advance`` and the
      remaining ticks to the threshold — only when advancement evidence
      satisfying the advancement criteria is present, so a confident model
      can never advance a clock over an empty evidence set;
    - ``COMPLETE`` when the threshold is already met (mechanical), when
      ticks could finish the clock this evaluation *and* advancement
      evidence is present, or when judged completion criteria exist *and*
      completion evidence satisfying the completion prefilters is present.
      Completion filters are never borrowed from the advancement criteria:
      unrelated advancement evidence alone cannot legalize COMPLETE.
    """
    if clock.status not in CLOCK_EVALUABLE_STATUSES:
        return [NO_CHANGE, DEFER]
    if int(clock.progress or 0) >= int(clock.threshold):
        return [COMPLETE, DEFER]
    criteria = clock.advancement_criteria or {}
    try:
        max_advance = max(1, int(criteria.get("max_advance", 1)))
    except (TypeError, ValueError):
        raise ValueError(f"clock {clock.id} has non-integer max_advance")
    remaining = int(clock.threshold) - int(clock.progress or 0)
    could_finish = int(clock.progress or 0) + min(max_advance, remaining) >= int(clock.threshold)
    ids = [NO_CHANGE]
    if has_advancement_evidence:
        for amount in range(1, min(max_advance, remaining) + 1):
            ids.append(f"ADVANCE_{amount}")
    judged_complete = bool(clock.completion_criteria) and has_completion_evidence
    if (could_finish and has_advancement_evidence) or judged_complete:
        ids.append(COMPLETE)
    ids.append(DEFER)
    return ids


def _candidate_label(outcome_id: str, clock_name: str, *, is_authority: bool) -> str:
    name = clock_name if is_authority else "the hidden pressure"
    amount = advance_amount(outcome_id)
    if amount is not None:
        return f"Advance '{name}' by {amount} tick(s): evidence satisfies its criteria"
    if outcome_id == COMPLETE:
        return f"Complete '{name}': its completion criteria are met"
    if outcome_id == NO_CHANGE:
        return f"Leave '{name}' unchanged: evidence does not satisfy its criteria"
    if outcome_id == DEFER:
        return f"Defer '{name}' to open AI-DM adjudication: evidence is insufficient or ambiguous"
    return outcome_id


def _candidate_risk(outcome_id: str) -> tuple[str, bool]:
    if outcome_id == NO_CHANGE or outcome_id == DEFER:
        return "low", True
    if outcome_id == COMPLETE:
        # Completion records an explicit terminal state that later world
        # consequences consume: never direct-executed. A confident selection
        # returns primer_advisory and is applied only after the mandatory
        # deterministic revalidation at apply time; anything weaker
        # escalates to open AI-DM adjudication (recorded as deferral).
        return "standard", False
    return "standard", True


def build_clock_frame(
    clock: CampaignClock, *, evidence: list[dict[str, Any]], evidence_total: int,
    from_sequence: int, to_sequence: int, is_authority: bool = True,
    has_advancement_evidence: bool | None = None,
    has_completion_evidence: bool = False,
) -> DecisionFrame:
    """Build the versioned decision frame for one clock evaluation.

    ``state_revision`` is the authoritative clock revision: any drift before
    application rejects the decision as stale. Non-authority frames redact
    the hidden name, criteria mechanics, and restricted evidence — hidden
    clock details never enter unauthorized decision inputs.

    The candidate set is evidence-gated: ``ADVANCE_*`` requires advancement
    evidence and judged ``COMPLETE`` requires completion evidence, so the
    judge can only select transitions the evidence actually supports. When
    ``has_advancement_evidence`` is omitted it is derived from
    ``evidence_total`` (the advancement match count).
    """
    if has_advancement_evidence is None:
        has_advancement_evidence = int(evidence_total or 0) > 0
    outcome_ids = legal_outcome_ids(
        clock, has_advancement_evidence=has_advancement_evidence,
        has_completion_evidence=has_completion_evidence,
    )
    records = []
    for outcome_id in outcome_ids:
        risk, reversible = _candidate_risk(outcome_id)
        records.append(CandidateRecord(
            id=outcome_id,
            label=_candidate_label(outcome_id, clock.name, is_authority=is_authority),
            source="clock:criteria_evaluation",
            source_ref=f"clock:{clock.id}:rev{int(clock.revision or 1)}",
            payload_ref=str(clock.id),
            risk=risk,
            reversible=reversible,
        ))
    if is_authority:
        state: Any = {
            "frame_schema": CLOCK_FRAME_SCHEMA_VERSION,
            "clock_ref": str(clock.id),
            "clock_name": clock.name,
            "status": clock.status,
            "progress": int(clock.progress or 0),
            "threshold": int(clock.threshold),
            "stages": list(clock.stages or []),
            "advancement_criteria": dict(clock.advancement_criteria or {}),
            "completion_criteria": dict(clock.completion_criteria or {}),
            "evidence": evidence,
            "evidence_total": evidence_total,
            "source_range": [from_sequence, to_sequence],
        }
    else:
        state = {
            "frame_schema": CLOCK_FRAME_SCHEMA_VERSION,
            "clock_ref": str(clock.id),
            "audience": "member",
            "evidence": [
                ref for ref in evidence
                if str((ref.get("visibility") or "public")) not in RESTRICTED_VISIBILITIES
            ],
            "evidence_total": evidence_total,
            "source_range": [from_sequence, to_sequence],
        }
    return build_frame(
        decision_class=CLOCK_DECISION_CLASS,
        question_id=CLOCK_QUESTION_ID,
        instructions=CLOCK_FRAME_INSTRUCTIONS,
        state=state,
        state_revision=int(clock.revision or 1),
        candidates=records,
        include_escapes=False,
    )


# ── Bounded decision ───────────────────────────────────────────────────────

class ClockDecision:
    """Outcome of one bounded clock judgment (never a mutation permit)."""

    def __init__(
        self, selected_id: str, directive: str, *, frame: DecisionFrame,
        failure: str | None = None, record: Any = None,
        provider: str | None = None, model: str | None = None,
    ) -> None:
        self.selected_id = selected_id
        self.directive = directive
        self.frame = frame
        self.failure = failure
        self.record = record
        self.provider = provider
        self.model = model


def decide_clock(
    clock: CampaignClock, frame: DecisionFrame, service: DecisionService,
    *, campaign_id: Any = None, session_factory: Any = None,
) -> ClockDecision:
    """Run, policy-check, and record one bounded clock judgment.

    Decision-plane failures (provider/config/timeout/malformed response)
    resolve to an explicit recorded DEFER — never guessed advancement.
    Revalidation against the live revision happens at apply time, not here.
    """
    try:
        response = service.decide(to_decision_request(frame))
    except Exception as exc:
        logger.warning("clock decision failed clock=%s error=%s", clock.id, exc)
        return ClockDecision(DEFER, ESCALATE, frame=frame, failure=f"{type(exc).__name__}: {exc}"[:500])
    result = response.results.get(frame.question_id)
    if not isinstance(result, ChoiceResult):
        logger.warning("clock decision missing result clock=%s", clock.id)
        return ClockDecision(DEFER, ESCALATE, frame=frame, failure="malformed: missing choice result",
                             provider=response.provider, model=response.model)
    if is_escape_id(result.selected_id) or result.selected_id == DEFER:
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True,
        )
        record = build_record(
            frame, result, verdict, provider=response.provider,
            model=response.model or "unknown", mode=ACTIVE,
            trace_id=response.trace_id, operation_id=response.operation_id,
            campaign_id=campaign_id, latency_ms=response.latency_ms, verified=True,
        )
        record_fail_soft(session_factory, record)
        return ClockDecision(DEFER, ESCALATE, frame=frame, record=record,
                             provider=response.provider, model=response.model)
    try:
        candidate_known = result.selected_id in {c.id for c in frame.candidates}
        if not candidate_known:
            raise DecisionError(
                f"unknown candidate {result.selected_id!r} for question {frame.question_id!r}",
                kind="malformed",
            )
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True,
        )
    except Exception as exc:
        logger.warning("clock policy failed clock=%s error=%s", clock.id, exc)
        return ClockDecision(DEFER, ESCALATE, frame=frame,
                             failure=f"{type(exc).__name__}: {exc}"[:500],
                             provider=response.provider, model=response.model)
    record = build_record(
        frame, result, verdict, provider=response.provider,
        model=response.model or "unknown", mode=ACTIVE,
        trace_id=response.trace_id, operation_id=response.operation_id,
        campaign_id=campaign_id, latency_ms=response.latency_ms, verified=True,
    )
    record_fail_soft(session_factory, record)
    if verdict.directive == ESCALATE:
        # Below class thresholds / near-tie / over-risk: open adjudication.
        return ClockDecision(DEFER, verdict.directive, frame=frame, record=record,
                             provider=response.provider, model=response.model)
    # direct_execute, or primer_advisory confirmed by the mandatory
    # deterministic revalidation at apply time (revision + legality +
    # mechanical guardrails), which is the policy's confirmation step.
    return ClockDecision(result.selected_id, verdict.directive, frame=frame,
                         record=record, provider=response.provider,
                         model=response.model)


# ── Application (deterministic, idempotent, revision-guarded) ──────────────

def apply_clock_outcome(
    db: Session, campaign_id: Any, clock_id: Any, *, frame: DecisionFrame,
    selected_id: str, evidence_refs: list[dict[str, Any]],
    from_sequence: int, to_sequence: int, operation_id: str | None = None,
    commit_gate: ClockCommitGate | None = None,
) -> dict[str, Any]:
    """Apply one decided outcome after revision + evidence revalidation.

    Raises :class:`ClockStaleError` when the clock revision moved since the
    frame was enumerated, and ``ValueError`` (fail closed) on illegal
    outcomes, invalid evidence refs, or advancement/completion selected
    with no evidence satisfying the relevant criteria. NO_CHANGE advances
    only the idempotency watermark; advancement/completion commit a domain
    event under campaign revision ordering. ``commit_gate`` is re-checked
    under the campaign row lock right before that commit and raises
    :class:`ClockCommitDeferred` (nothing applied) when it vetoes.
    """
    cid = coerce_uuid(campaign_id, field="campaign_id")
    clock = get_clock_strict(db, cid, coerce_uuid(clock_id, field="clock_id"))
    if clock.status not in CLOCK_EVALUABLE_STATUSES:
        raise ClockStaleError(clock.id, frame.state_revision, clock.revision)
    if int(clock.revision or 1) != int(frame.state_revision):
        raise ClockStaleError(clock.id, frame.state_revision, clock.revision)
    _validate_evidence_refs(db, cid, list(evidence_refs or []),
                            from_sequence=from_sequence, to_sequence=to_sequence)
    has_evidence = bool(evidence_refs)
    amount = advance_amount(selected_id)
    completing = selected_id == COMPLETE
    if (not completing and amount is None and selected_id not in (NO_CHANGE, DEFER)
            and not is_escape_id(selected_id)):
        raise ValueError(f"clock {clock.id} outcome {selected_id!r} is not a legal transition")
    if amount is not None and amount <= 0:
        raise ValueError(f"clock {clock.id} advancement amount must be positive")
    if amount is not None and not has_evidence:
        raise ValueError(
            f"clock {clock.id} advancement requires evidence satisfying its criteria"
        )
    if completing and not has_evidence and int(clock.progress or 0) < int(clock.threshold):
        raise ValueError(
            f"clock {clock.id} completion requires evidence satisfying its criteria"
        )
    legal_ids = legal_outcome_ids(
        clock, has_advancement_evidence=has_evidence,
        has_completion_evidence=has_evidence,
    )
    if (
        selected_id == COMPLETE
        and COMPLETE not in legal_ids
        and int(clock.progress or 0) >= int(clock.threshold)
    ):
        # Mechanical completion is always legal once the threshold is met,
        # even when the frame was enumerated before the final tick landed.
        legal_ids = [*legal_ids, COMPLETE]
    revalidate_for_execution(frame, selected_id, int(clock.revision or 1), legal_ids=legal_ids)

    if selected_id == NO_CHANGE:
        clock.evaluated_through_sequence = max(int(clock.evaluated_through_sequence or 0), to_sequence)
        db.flush()
        return {
            "clock_id": str(clock.id), "outcome": "no_change",
            "progress": int(clock.progress or 0), "status": clock.status,
            "revision": int(clock.revision or 1),
            "evaluated_through": int(clock.evaluated_through_sequence or 0),
        }

    if selected_id == DEFER:
        clock.evaluated_through_sequence = max(int(clock.evaluated_through_sequence or 0), to_sequence)
        db.flush()
        return {
            "clock_id": str(clock.id), "outcome": "deferred",
            "progress": int(clock.progress or 0), "status": clock.status,
            "revision": int(clock.revision or 1),
            "evaluated_through": int(clock.evaluated_through_sequence or 0),
        }

    amount = advance_amount(selected_id)
    completing = selected_id == COMPLETE
    if (not completing and amount is None and selected_id not in (NO_CHANGE, DEFER)
            and not is_escape_id(selected_id)):
        raise ValueError(f"clock {clock.id} outcome {selected_id!r} is not a legal transition")

    expected_revision = int(clock.revision or 1)
    holder: dict[str, Any] = {}

    def mutate(campaign: Campaign) -> None:
        # Atomic re-check inside the revision transaction: a concurrent
        # applier that committed between frame build and now is rejected
        # instead of double-applying.
        fresh = db.execute(select(CampaignClock).where(
            CampaignClock.id == clock.id,
        ).with_for_update()).scalars().first()
        if fresh is None or fresh.campaign_id != cid:
            raise ValueError(f"clock {clock_id} vanished during application")
        if int(fresh.revision or 1) != expected_revision:
            raise ClockStaleError(fresh.id, expected_revision, fresh.revision)
        if fresh.status not in CLOCK_EVALUABLE_STATUSES:
            raise ClockStaleError(fresh.id, expected_revision, fresh.revision)
        prior_progress = int(fresh.progress or 0)
        new_progress = int(fresh.threshold) if completing else prior_progress + int(amount or 0)
        if new_progress > int(fresh.threshold):
            raise ValueError(f"clock {fresh.id} advancement overshoots its threshold")
        fresh.progress = new_progress
        fresh.revision = expected_revision + 1
        fresh.evaluated_through_sequence = max(int(fresh.evaluated_through_sequence or 0), to_sequence)
        holder["completed"] = new_progress >= int(fresh.threshold)
        # "threshold_reached": the pressure filled (advanced to, or already
        # sat at, its threshold). "criteria_met": judged completion ended it
        # early. The forward-DM pressure lane tells these apart.
        holder["reason"] = "threshold_reached" if (
            (not completing and holder["completed"]) or (completing and prior_progress >= int(fresh.threshold))
        ) else "criteria_met"
        if holder["completed"]:
            fresh.status = "completed"
            fresh.completed_at = datetime.now(timezone.utc)
            fresh.resolution = {
                "outcome": "completed",
                "reason": holder["reason"],
                "via_outcome": selected_id,
                "source_range": [from_sequence, to_sequence],
                "evidence": list(evidence_refs or []),
                "completion_effect": dict(fresh.completion_effect or {}),
            }
        holder["progress"] = new_progress
        holder["revision"] = fresh.revision
        holder["status"] = fresh.status

    if commit_gate is not None:
        # Same lock commit_campaign_mutation takes below (re-entrant within
        # this transaction), held through the commit so the gate's verdict
        # cannot go stale before the revision bump lands.
        lock_campaign_row(db, cid)
        reason = commit_gate(db, cid)
        if reason:
            raise ClockCommitDeferred(cid, reason)
    event_type = CLOCK_COMPLETED_EVENT if (
        completing or int(clock.progress or 0) + int(amount or 0) >= int(clock.threshold)
    ) else CLOCK_ADVANCED_EVENT
    _campaign, event = commit_campaign_mutation(
        db, cid, int(db.get(Campaign, cid).revision or 0),
        event_type=event_type, operation_id=operation_id,
        mutate=mutate,
        payload_builder=lambda: {
            "clock_id": str(clock.id),
            "outcome": selected_id,
            "progress": holder["progress"],
            "threshold": int(clock.threshold),
            "reason": holder["reason"],
            "source_range": [from_sequence, to_sequence],
            "evidence": list(evidence_refs or []),
            **({"completion_effect": dict(clock.completion_effect or {})} if holder["completed"] else {}),
        },
        targets_builder=lambda: {"clock_id": str(clock.id)},
        visibility_builder=lambda: world_event_visibility(clock.visibility),
        provenance={"source": "post_turn_clock_evaluation",
                    "frame_state_revision": expected_revision},
    )
    structured_log(
        logger, logging.INFO, "clock_applied", campaign_id=str(cid), clock_id=str(clock.id),
        outcome=selected_id, progress=holder["progress"], status=holder["status"],
    )
    return {
        "clock_id": str(clock.id),
        "outcome": "completed" if holder["completed"] else "advanced",
        "progress": holder["progress"], "status": holder["status"],
        "revision": holder["revision"], "event_id": str(event.id),
        "evaluated_through": to_sequence,
    }


# ── Per-clock evaluation over one source range ─────────────────────────────

def evaluate_clock_for_range(
    db: Session, campaign: Campaign, clock: CampaignClock,
    events: list[CampaignDomainEvent], *, from_sequence: int, to_sequence: int,
    decision_service: DecisionService | None = None,
    session_factory: Any = None, operation_id: str | None = None,
    is_authority: bool = True, commit_gate: ClockCommitGate | None = None,
) -> dict[str, Any]:
    """Evaluate one clock against the unevaluated suffix of a source range.

    Returns a per-clock result dict (never raises for decision-plane
    outcomes; stale frames skip with the watermark untouched; required
    processing errors propagate to fail the post-turn range).
    """
    base = {"clock_id": str(clock.id), "status": clock.status,
            "revision": int(clock.revision or 1)}
    if clock.status not in CLOCK_EVALUABLE_STATUSES:
        return {**base, "evaluated": False, "reason": f"status_{clock.status}_not_evaluable"}
    if clock.status in CLOCK_TERMINAL_STATUSES:  # pragma: no cover - guarded above
        return {**base, "evaluated": False, "reason": "terminal"}
    lo = max(from_sequence, int(clock.evaluated_through_sequence or 0) + 1)
    if lo > to_sequence:
        return {**base, "evaluated": False, "reason": "already_evaluated",
                "evaluated_through": int(clock.evaluated_through_sequence or 0)}
    window = [e for e in events if lo <= int(e.sequence) <= to_sequence]
    if not window:
        raise ValueError(
            f"clock {clock.id} evaluation range {lo}-{to_sequence} has no committed events; "
            "refusing to advance the watermark past unvalidated history"
        )
    criteria = dict(clock.advancement_criteria or {})
    kind = str(criteria.get("kind") or "").strip().lower()
    if kind not in ("deterministic", "semantic"):
        raise ValueError(f"clock {clock.id} has unknown criteria kind {kind!r}")

    matching, total = collect_evidence(window, criteria)
    refs = evidence_refs(matching)

    if kind == "deterministic":
        required = max(1, int(criteria.get("required_count", 1)))
        max_advance = max(1, int(criteria.get("max_advance", 1)))
        remaining = int(clock.threshold) - int(clock.progress or 0)
        if remaining <= 0:
            return _complete_threshold(db, campaign, clock, refs, from_sequence, to_sequence,
                                       operation_id, commit_gate)
        available = int(clock.progress_carry or 0) + total
        ticks = min(available // required, max_advance, remaining)
        if ticks <= 0:
            clock.progress_carry = available
            clock.evaluated_through_sequence = max(int(clock.evaluated_through_sequence or 0), to_sequence)
            db.flush()
            return {**base, "evaluated": True, "outcome": "no_change",
                    "progress": int(clock.progress or 0), "carry": available,
                    "evidence_count": total, "evaluated_through": to_sequence,
                    "path": "deterministic"}
        carry_before = int(clock.progress_carry or 0)
        clock.progress_carry = available - ticks * required
        outcome_id = f"ADVANCE_{ticks}"
        try:
            applied = apply_clock_outcome(
                db, campaign.id, clock.id, frame=build_clock_frame(
                    clock, evidence=refs, evidence_total=total,
                    from_sequence=from_sequence, to_sequence=to_sequence,
                    is_authority=is_authority,
                ),
                selected_id=outcome_id, evidence_refs=refs,
                from_sequence=from_sequence, to_sequence=to_sequence,
                operation_id=operation_id, commit_gate=commit_gate,
            )
        except ClockStaleError:
            # No rollback here: a stale clock wrote nothing of its own, and
            # isolation belongs to the caller's savepoint (consolidation
            # evaluates each clock inside one) so sibling clocks' flushed
            # watermarks survive the skip. The carry deduction above must be
            # undone first: the rejected tick consumed no evidence, so the
            # accumulated carry survives for the next evaluation.
            clock.progress_carry = carry_before
            fresh = get_clock_strict(db, campaign.id, clock.id)
            return {**base, "evaluated": False, "reason": "stale_revision_skipped",
                    "revision": int(fresh.revision or 1)}
        applied["evidence_count"] = total
        applied["path"] = "deterministic"
        applied["evaluated"] = True
        return applied

    # Semantic path: the model judges only among the legal outcomes.
    # Completion evidence is collected against the completion criteria
    # alone — never borrowed from the advancement prefilter — and the
    # frame offers ADVANCE_* / judged COMPLETE only when the matching
    # evidence set is non-empty. Completion-only events join the frame so
    # the judge can verify the completion it is asked to consider.
    completion_matching: list[CampaignDomainEvent] = []
    completion_total = 0
    completion_criteria = clock.completion_criteria or None
    if completion_criteria:
        completion_matching, completion_total = collect_evidence(
            window, dict(completion_criteria))
    seen_ids = {str(e.id) for e in matching}
    frame_events = list(matching) + [e for e in completion_matching if str(e.id) not in seen_ids]
    frame_refs = story_evidence(db, frame_events)
    frame_total = total + sum(1 for e in completion_matching if str(e.id) not in seen_ids)
    completion_refs = evidence_refs(completion_matching)
    frame = build_clock_frame(
        clock, evidence=frame_refs, evidence_total=frame_total,
        from_sequence=from_sequence, to_sequence=to_sequence,
        is_authority=is_authority,
        has_advancement_evidence=total > 0,
        has_completion_evidence=completion_total > 0,
    )
    service = decision_service or DecisionService()
    decision = decide_clock(clock, frame, service, campaign_id=campaign.id,
                            session_factory=session_factory)
    trace = shared_trace(decision.record) if decision.record is not None else None
    if decision.selected_id == DEFER:
        clock.evaluated_through_sequence = max(int(clock.evaluated_through_sequence or 0), to_sequence)
        db.flush()
        return {**base, "evaluated": True, "outcome": "deferred",
                "progress": int(clock.progress or 0),
                "evidence_count": total, "evaluated_through": to_sequence,
                "path": "decision", "directive": decision.directive,
                "failure": decision.failure, "telemetry": trace}
    try:
        judged_complete = decision.selected_id == COMPLETE and completion_total > 0
        applied = apply_clock_outcome(
            db, campaign.id, clock.id, frame=frame,
            selected_id=decision.selected_id,
            evidence_refs=completion_refs if judged_complete else refs,
            from_sequence=from_sequence, to_sequence=to_sequence,
            operation_id=operation_id, commit_gate=commit_gate,
        )
    except ClockStaleError:
        # Same savepoint-owned isolation as the deterministic path above:
        # never a full-session rollback here.
        fresh = get_clock_strict(db, campaign.id, clock.id)
        return {**base, "evaluated": False, "reason": "stale_revision_skipped",
                "revision": int(fresh.revision or 1),
                "evidence_count": total, "path": "decision", "telemetry": trace}
    applied["evidence_count"] = completion_total if judged_complete else total
    applied["path"] = "decision"
    applied["directive"] = decision.directive
    applied["evaluated"] = True
    applied["telemetry"] = trace
    return applied


def _complete_threshold(
    db: Session, campaign: Campaign, clock: CampaignClock,
    refs: list[dict[str, Any]], from_sequence: int, to_sequence: int,
    operation_id: str | None, commit_gate: ClockCommitGate | None,
) -> dict[str, Any]:
    """Defensive completion when progress already covers the threshold."""
    frame = build_clock_frame(
        clock, evidence=refs, evidence_total=len(refs),
        from_sequence=from_sequence, to_sequence=to_sequence,
    )
    applied = apply_clock_outcome(
        db, campaign.id, clock.id, frame=frame, selected_id=COMPLETE,
        evidence_refs=refs, from_sequence=from_sequence,
        to_sequence=to_sequence, operation_id=operation_id, commit_gate=commit_gate,
    )
    applied["evidence_count"] = len(refs)
    applied["path"] = "deterministic"
    applied["evaluated"] = True
    return applied


# ── Range consolidation (post-turn content phase) ──────────────────────────

def consolidate_clocks_for_range(
    db: Session, campaign_id: Any, from_sequence: int, to_sequence: int,
    events: list[CampaignDomainEvent], *, decision_service: DecisionService | None = None,
    session_factory: Any = None, operation_id: str | None = None,
    commit_gate: ClockCommitGate | None = None,
) -> dict[str, Any]:
    """Evaluate every evaluable clock against one committed source range.

    Deterministic clocks resolve without a model call; semantic clocks go
    through the bounded decision runtime with full #383 telemetry. One
    clock's stale skip never blocks the others; any other required
    processing error propagates so the post-turn run fails and the range
    is retried cumulatively. Emits no wall-clock reads: an empty evidence
    window is an explicit no-op.

    ``commit_gate`` vetoes revision-bumping commits: checked once up front
    (before any decision-model call) and again under the campaign row lock
    per commit. A veto raises :class:`ClockCommitDeferred` so the caller
    retries the whole range later; clocks already committed this pass keep
    their watermarks and are not re-applied.
    """
    cid = coerce_uuid(campaign_id, field="campaign_id")
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found")
    if commit_gate is not None:
        reason = commit_gate(db, cid)
        if reason:
            raise ClockCommitDeferred(cid, reason)
    # All non-terminal clocks load so dormant/pending skips are explicit
    # per-clock results (a clock may remain dormant without advancing);
    # terminal clocks are never re-evaluated.
    clocks = list(db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == cid,
        ~CampaignClock.status.in_(("completed", "retired")),
    ).order_by(CampaignClock.created_at.asc())).scalars().all())
    results: list[dict[str, Any]] = []
    for clock in clocks:
        op_id = compose_operation_id(operation_id, "clock", clock.id, f"{from_sequence}-{to_sequence}")
        # One savepoint per clock: a stale skip rolls back only that clock's
        # pending work, never sibling clocks' flushed watermarks or (already
        # committed) advancements. The guards below handle writers that
        # already closed the transaction themselves — a successful apply
        # commits, and the shared mutation helper fully rolls back on a
        # mid-mutation race — so the savepoint is closed only while active.
        # Any other required processing error propagates (no catch) so the
        # post-turn run fails and the range is retried cumulatively.
        savepoint = db.begin_nested()
        try:
            outcome = evaluate_clock_for_range(
                db, campaign, clock, events,
                from_sequence=from_sequence, to_sequence=to_sequence,
                decision_service=decision_service, session_factory=session_factory,
                operation_id=op_id, commit_gate=commit_gate,
            )
        except ClockCommitDeferred:
            if savepoint.is_active:
                savepoint.rollback()
            raise
        except ClockStaleError:
            if savepoint.is_active:
                savepoint.rollback()
            fresh = get_clock_strict(db, cid, clock.id)
            results.append({
                "clock_id": str(clock.id), "status": fresh.status,
                "revision": int(fresh.revision or 1),
                "evaluated": False, "reason": "stale_revision_skipped",
            })
        else:
            if savepoint.is_active:
                savepoint.commit()
            results.append(outcome)
    advanced = sum(1 for r in results if r.get("outcome") in ("advanced", "completed"))
    return {
        "clocks_evaluated": len(results),
        "advanced": advanced,
        "results": results,
    }


# ── Viewer-aware projection (secrecy first) ────────────────────────────────

def project_clocks_for_viewer(
    db: Session, campaign: Campaign, viewer_user_id: Any,
) -> dict[str, Any]:
    """List clocks visible to a human viewer; hidden clocks are filtered.

    Non-members see nothing. Ordinary members see only ``public``/``campaign``
    clocks, and even then without DM-side provenance, completion effects, or
    resolution internals. The campaign owner (AI-DM authority lane) sees
    every clock in full.
    """
    viewer = coerce_uuid(viewer_user_id, field="viewer_user_id")
    if not is_campaign_participant(db, campaign, viewer):
        return {"clocks": [], "count": 0}
    authority = is_world_authority(campaign, viewer)
    rows = list(db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == campaign.id,
    ).order_by(CampaignClock.created_at.asc())).scalars().all())
    visible: list[dict[str, Any]] = []
    hidden = 0
    for row in rows:
        if visible_to_viewer(row.visibility, authority):
            data = row.to_dict()
            if not authority:
                for key in ("provenance", "completion_effect", "resolution",
                            "progress_carry", "evaluated_through_sequence",
                            "operation_id", "idempotency_key",
                            "source_turn_id", "source_attempt_id", "source_event_id"):
                    data.pop(key, None)
                completion = data.get("completion_criteria") or {}
                if isinstance(completion, dict):
                    completion.pop("description", None)
            visible.append(data)
        else:
            hidden += 1
    result: dict[str, Any] = {"clocks": visible, "count": len(visible)}
    if authority:
        result["hidden_count"] = hidden
    return result


# ── Forward-DM pressure view (post-turn writes, the DM reacts) ────────────


def _stage_index(stages: list[dict[str, Any]], progress: int) -> int:
    """How many stages ``progress`` has reached (0 = none)."""
    return sum(1 for stage in stages if int(stage["at"]) <= progress)


def _clock_directive(clock: CampaignClock, event: CampaignDomainEvent, crossed: dict[str, Any] | None) -> str:
    name = clock.name
    payload = event.payload or {}
    if event.event_type == CLOCK_COMPLETED_EVENT:
        effect = payload.get("completion_effect") or {}
        effect_text = f" Consequence on record: {effect.get('description') or effect}." if effect else ""
        if payload.get("reason") == "threshold_reached":
            return (f"'{name}' has filled ({clock.threshold}/{clock.threshold}): this pressure comes to a head now. "
                    f"Show its consequence in the world this turn.{effect_text}")
        ended = (clock.completion_criteria or {}).get("description") or "its completion criteria were met"
        return (f"'{name}' has ended ({ended}). Show the aftermath and how the world responds to it "
                f"this turn.{effect_text}")
    return (f"'{name}' reached its '{crossed['label']}' stage ({payload.get('progress')}/{clock.threshold}). "
            "Show this escalation in the world this turn, through NPC action, a new threat, or a visible "
            "change, consistent with the scene.")


def dm_pressure_view(
    db: Session, campaign_id: Any, *, through_sequence: int, turn_event_type: str,
) -> list[dict[str, Any]]:
    """Clock state the forward DM should play to, plus any owed directive.

    Post-turn evaluation stays the only writer of clock state; this is a
    read-only projection as of ``through_sequence``. A stage crossing or a
    completion owes the DM one directive, derived from the clock's own
    lifecycle events: it stays pending until a DM turn (``turn_event_type``)
    has committed after it, so it needs no extra bookkeeping. Active clocks
    are always listed; a finished clock is listed only while its directive
    is still owed.
    """
    cid = coerce_uuid(campaign_id, field="campaign_id")
    clocks = list(db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == cid,
    ).order_by(CampaignClock.created_at.asc())).scalars().all())
    if not clocks:
        return []
    events = list(db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == cid,
        CampaignDomainEvent.sequence <= int(through_sequence),
        CampaignDomainEvent.event_type.in_([CLOCK_ADVANCED_EVENT, CLOCK_COMPLETED_EVENT, turn_event_type]),
    ).order_by(CampaignDomainEvent.sequence.asc())).scalars().all())
    last_turn_sequence = max((int(e.sequence) for e in events if e.event_type == turn_event_type), default=-1)

    view: list[dict[str, Any]] = []
    for clock in clocks:
        stages = list(clock.stages or [])
        latest: tuple[CampaignDomainEvent, dict[str, Any] | None] | None = None
        for event in events:
            if event.event_type == turn_event_type or str((event.payload or {}).get("clock_id")) != str(clock.id):
                continue
            after = int((event.payload or {}).get("progress") or 0)
            if event.event_type == CLOCK_COMPLETED_EVENT:
                latest = (event, None)
                continue
            before = after - (advance_amount(str((event.payload or {}).get("outcome"))) or 0)
            if _stage_index(stages, after) > _stage_index(stages, before):
                latest = (event, stages[_stage_index(stages, after) - 1])
        owed = latest is not None and int(latest[0].sequence) > last_turn_sequence
        if clock.status not in CLOCK_EVALUABLE_STATUSES and not owed:
            continue
        progress = int(clock.progress or 0)
        reached = _stage_index(stages, progress)
        view.append({
            "clock_id": str(clock.id),
            "name": clock.name,
            "status": clock.status,
            "progress": progress,
            "threshold": int(clock.threshold),
            "current_stage": stages[reached - 1]["label"] if reached else None,
            "next_stage": stages[reached] if reached < len(stages) else {"at": int(clock.threshold), "label": "fills"},
            "ends_when": (clock.completion_criteria or {}).get("description"),
            "evaluated_through_sequence": int(clock.evaluated_through_sequence or 0),
            "visibility": clock.visibility,
            "directive": _clock_directive(clock, latest[0], latest[1]) if owed else None,
        })
    return view

