"""Authoritative world retrieval — issue #212.

Controlled structured queries over canonical campaign records (entities,
relations, facts, domain events, source turns/submissions, current scene,
character/NPC knowledge) that normalize results into typed evidence packets
with stable source IDs, epistemic state, visibility, version/revision, and
code-owned provenance.

Design rules (pre-alpha, single canonical implementation):

- No arbitrary SQL or table names: every query is a fixed select with
  validated scalar parameters (UUID ids, bounded ints, known enums).
- Campaign scoping is mandatory on every query.
- Audience authorization happens BEFORE revealable evidence is returned
  (player-facing mode filters hidden rows, counting denials without leaking
  ids/content). DM-internal mode preserves visibility metadata on every
  packet for later projection instead of erasing it.
- Provenance is code-owned: built from persisted record fields plus a
  ``retrieved_by`` marker. Callers can never supply provenance.
- The AI is the only DM; no copy here implies a human DM/moderator.

#203 integration: ``TOOL_HANDLERS`` maps the world evidence tools onto
``(request, audience, db)`` callables executable through
``app.dm.evidence.execute_evidence_round`` without provider-specific
knowledge.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import or_ as sqlalchemy_or, select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.visibility.policy import most_restrictive, visibility_or_dm_only
from app.world._common import clamp_limit, coerce_optional_uuid, coerce_uuid
from app.world.evidence_packets import (
    EvidencePacket,
    audience_viewers,
    authorize_world_record,
    entity_packet,
    event_packet,
    event_visible_player_facing,
    fact_packet,
    knowledge_packet,
    packet_source_ids,
    relation_packet,
    resolve_campaign,
    resolve_viewers,
    submission_gate,
    tool_result,
    turn_gate,
    turn_packet,
)
from models.campaigns import Campaign, CampaignDomainEvent
from models.dm import DmTurn
from models.threads import PlayerSubmission

logger = logging.getLogger(__name__)

# ── Bounds (observable; every outcome reports the applied values) ────────────

RETRIEVAL_DEFAULT_LIMIT = 20
RETRIEVAL_MAX_LIMIT = 50
RETRIEVAL_DEFAULT_DEPTH = 1
RETRIEVAL_MAX_DEPTH = 3
TIMELINE_DEFAULT_LIMIT = 20

# Outcome statuses. ``not_found`` / ``defer`` / ``no_match`` are explicit
# evidence states — never fabricated substitutes.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_DEFER = "defer"
STATUS_NO_MATCH = "no_match"
# ── Typed outcome ────────────────────────────────────────────────────────────

@dataclass
class RetrievalOutcome:
    """Envelope for one retrieval query: packets plus observable bounds."""

    status: str = STATUS_OK
    packets: list[EvidencePacket] = field(default_factory=list)
    total: int = 0
    visible: int = 0
    denied: int = 0
    denied_reasons: dict[str, int] = field(default_factory=dict)
    depth_applied: int = 0
    limit_applied: int = 0
    truncated: bool = False
    latency_ms: float = 0.0
    source_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "packets": [p.to_dict() for p in self.packets],
            "total": self.total,
            "visible": self.visible,
            "denied": self.denied,
            "denied_reasons": dict(self.denied_reasons),
            "depth_applied": self.depth_applied,
            "limit_applied": self.limit_applied,
            "truncated": self.truncated,
            "latency_ms": self.latency_ms,
            "source_ids": list(self.source_ids),
            "error": self.error,
        }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _clamp_depth(depth: Any) -> int:
    try:
        value = int(depth if depth is not None else RETRIEVAL_DEFAULT_DEPTH)
    except (TypeError, ValueError):
        value = RETRIEVAL_DEFAULT_DEPTH
    return max(0, min(value, RETRIEVAL_MAX_DEPTH))


def _log_query(
    query_type: str,
    campaign: Campaign,
    *,
    depth: int,
    limit: int,
    outcome: RetrievalOutcome,
    dm_internal: bool,
) -> None:
    structured_log(
        logger, logging.INFO, "world_retrieval_query",
        query_type=query_type,
        campaign_id=str(campaign.id),
        dm_internal=dm_internal,
        depth_applied=depth,
        limit_applied=limit,
        result_count=len(outcome.packets),
        total=outcome.total,
        denied=outcome.denied,
        denied_reasons=dict(outcome.denied_reasons),
        truncated=outcome.truncated,
        status=outcome.status,
        source_ids=outcome.source_ids,
        latency_ms=round(outcome.latency_ms, 3),
    )


def _not_found(query_type: str, campaign: Campaign, *, depth: int, limit: int,
               detail: str) -> RetrievalOutcome:
    outcome = RetrievalOutcome(
        status=STATUS_NOT_FOUND, depth_applied=depth, limit_applied=limit, error=detail,
    )
    _log_query(query_type, campaign, depth=depth, limit=limit,
               outcome=outcome, dm_internal=False)
    return outcome


# ── Entity + graph traversal ─────────────────────────────────────────────────

def retrieve_entity(
    db: Session,
    campaign_id: Any,
    entity_id: Any,
    viewer_user_id: Any = None,
    *,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Retrieve one canonical entity by stable ID with provenance metadata."""
    from app.world.service import get_entity_strict

    started = time.monotonic()
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    try:
        eid = coerce_uuid(entity_id, field="entity_id")
        entity = get_entity_strict(db, campaign.id, eid)
    except ValueError as exc:
        return _not_found("lookup_world_entity", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = authorize_world_record(
        db, campaign, "entity", entity.id, viewers, dm_internal=dm_internal)
    outcome = RetrievalOutcome(depth_applied=0, limit_applied=1)
    outcome.total = 1
    if not allowed:
        outcome.denied = 1
        outcome.denied_reasons[reason or "denied"] = 1
        outcome.status = STATUS_NOT_FOUND if reason == "record_not_found" else STATUS_OK
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("lookup_world_entity", campaign, depth=0, limit=1,
                   outcome=outcome, dm_internal=dm_internal)
        return outcome
    outcome.packets = [entity_packet(
        entity, campaign.id, 0,
        revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_world_entity", campaign, depth=0, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


def traverse_relations(
    db: Session,
    campaign_id: Any,
    root_entity_id: Any,
    viewer_user_id: Any = None,
    *,
    depth: Any = RETRIEVAL_DEFAULT_DEPTH,
    limit: Any = RETRIEVAL_DEFAULT_LIMIT,
    include_history: bool = False,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Bounded BFS graph traversal from one root entity.

    Collects active (or historical, on request) relation packets plus the
    neighboring entity packets they touch. Depth and limit are clamped to
    ``RETRIEVAL_MAX_DEPTH`` / ``RETRIEVAL_MAX_LIMIT``; the applied values and
    any truncation are reported on the outcome and in observability.
    Authorization is enforced per record during collection so hidden rows
    never consume the result window for player-facing callers.
    """
    from app.world.service import get_entity_strict

    started = time.monotonic()
    depth_applied = _clamp_depth(depth)
    limit_applied = clamp_limit(limit, default=RETRIEVAL_DEFAULT_LIMIT, maximum=RETRIEVAL_MAX_LIMIT)
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    try:
        root_id = coerce_uuid(root_entity_id, field="entity_id")
        root = get_entity_strict(db, campaign.id, root_id)
    except ValueError as exc:
        return _not_found("traverse_world_relations", campaign, depth=depth_applied,
                          limit=limit_applied, detail=str(exc))

    outcome = RetrievalOutcome(depth_applied=depth_applied, limit_applied=limit_applied)
    allowed, reason = authorize_world_record(
        db, campaign, "entity", root.id, viewers, dm_internal=dm_internal)
    if not allowed:
        outcome.total = 1
        outcome.denied = 1
        outcome.denied_reasons[reason or "denied"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("traverse_world_relations", campaign, depth=depth_applied,
                   limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
        return outcome

    packets: list[EvidencePacket] = [None]  # type: ignore[list-item]  # placeholder for root
    denied_reasons: dict[str, int] = {}
    total_seen = 1  # root entity
    visited_entities: set[str] = {str(root.id)}
    frontier: list[Any] = [root]
    relation_ids: set[str] = set()
    truncated = False

    root_packet = entity_packet(root, campaign.id, 0,
                                 revealable=None if dm_internal else True)
    packets[0] = root_packet

    from app.world.facts import list_relations
    from models.world import WorldEntity

    for _level in range(depth_applied):
        next_frontier: list[Any] = []
        for node in frontier:
            batch = list_relations(
                db, campaign.id, entity_id=node.id,
                include_history=bool(include_history), limit=RETRIEVAL_MAX_LIMIT,
                exclude_restricted=False,
            )
            for relation in batch:
                if str(relation.id) in relation_ids:
                    continue
                total_seen += 1
                allowed_rel, reason_rel = authorize_world_record(
                    db, campaign, "relation", relation.id, viewers,
                    dm_internal=dm_internal)
                if not allowed_rel:
                    denied_reasons[reason_rel or "denied"] = denied_reasons.get(reason_rel or "denied", 0) + 1
                    continue
                relation_ids.add(str(relation.id))
                packets.append(relation_packet(
                    relation, campaign.id, len(packets),
                    revealable=None if dm_internal else True))
                for neighbor_id in (relation.subject_entity_id, relation.object_entity_id):
                    if neighbor_id is None or str(neighbor_id) in visited_entities:
                        continue
                    neighbor = db.get(WorldEntity, neighbor_id)
                    if neighbor is None or neighbor.campaign_id != campaign.id:
                        continue
                    total_seen += 1
                    allowed_ent, reason_ent = authorize_world_record(
                        db, campaign, "entity", neighbor.id, viewers,
                        dm_internal=dm_internal)
                    if not allowed_ent:
                        denied_reasons[reason_ent or "denied"] = denied_reasons.get(reason_ent or "denied", 0) + 1
                        continue
                    visited_entities.add(str(neighbor.id))
                    packets.append(entity_packet(
                        neighbor, campaign.id, len(packets),
                        revealable=None if dm_internal else True))
                    next_frontier.append(neighbor)
                if len(packets) - 1 >= limit_applied:
                    truncated = True
                    break
            if len(packets) - 1 >= limit_applied:
                truncated = True
                break
        frontier = next_frontier
        if not frontier or len(packets) - 1 >= limit_applied:
            if frontier and len(packets) - 1 >= limit_applied:
                truncated = True
            break

    if len(packets) - 1 > limit_applied:
        packets = packets[: limit_applied + 1]
        truncated = True

    # Renumber retrieval ranks after bounding.
    for rank, packet in enumerate(packets):
        packet.retrieval_rank = rank
        packet.retrieval_score = float(1000 - rank)

    outcome.packets = packets
    outcome.total = total_seen
    outcome.visible = len(packets)
    outcome.denied = total_seen - len(packets)
    outcome.denied_reasons = denied_reasons
    outcome.truncated = truncated
    outcome.source_ids = packet_source_ids(packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("traverse_world_relations", campaign, depth=depth_applied,
               limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Fact lookup ──────────────────────────────────────────────────────────────

def lookup_fact(
    db: Session,
    campaign_id: Any,
    fact_id: Any,
    viewer_user_id: Any = None,
    *,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Retrieve one canonical fact by stable ID with provenance metadata."""
    from app.world.facts import get_fact_strict

    started = time.monotonic()
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    try:
        fid = coerce_uuid(fact_id, field="fact_id")
        fact = get_fact_strict(db, campaign.id, fid)
    except ValueError as exc:
        return _not_found("lookup_world_fact", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = authorize_world_record(
        db, campaign, "fact", fact.id, viewers, dm_internal=dm_internal)
    outcome = RetrievalOutcome(depth_applied=0, limit_applied=1)
    outcome.total = 1
    if not allowed:
        outcome.denied = 1
        outcome.denied_reasons[reason or "denied"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("lookup_world_fact", campaign, depth=0, limit=1,
                   outcome=outcome, dm_internal=dm_internal)
        return outcome
    outcome.packets = [fact_packet(
        fact, campaign.id, 0, revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_world_fact", campaign, depth=0, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


def fact_source_evidence(
    db: Session,
    campaign_id: Any,
    fact_id: Any,
    viewer_user_id: Any = None,
    *,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """The fact plus the historical events / source turn that established it.

    Follows the fact's own ``source_event_id`` / ``source_turn_id``
    provenance links (code-owned record fields, never caller input) so
    downstream execution can decide whether an answer is actually supported.
    """
    started = time.monotonic()
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    fact_outcome = lookup_fact(db, campaign.id, fact_id, viewers,
                               dm_internal=dm_internal)
    if fact_outcome.status == STATUS_NOT_FOUND or not fact_outcome.packets:
        return fact_outcome
    packets = list(fact_outcome.packets)
    denied_reasons = dict(fact_outcome.denied_reasons)
    denied = int(fact_outcome.denied)
    total = int(fact_outcome.total)
    content = packets[0].content
    event_ref = content.get("source_event_id")
    turn_ref = content.get("source_turn_id")
    if event_ref:
        total += 1
        event = db.get(CampaignDomainEvent, coerce_optional_uuid(event_ref))
        if event is None or event.campaign_id != campaign.id:
            denied += 1
            denied_reasons["source_event_not_found"] = denied_reasons.get("source_event_not_found", 0) + 1
        else:
            gate = (True, None) if dm_internal else event_visible_player_facing(db, campaign, event, viewers)
            if not gate[0]:
                denied += 1
                denied_reasons[gate[1] or "denied"] = denied_reasons.get(gate[1] or "denied", 0) + 1
            else:
                packets.append(event_packet(event, len(packets),
                                             revealable=None if dm_internal else True))
    if turn_ref:
        total += 1
        turn_outcome = lookup_source_turn(
            db, campaign.id, turn_ref, viewers,
            include_submissions=False, include_established_records=True,
            dm_internal=dm_internal)
        if turn_outcome.status == STATUS_NOT_FOUND or not turn_outcome.packets:
            denied += 1
            reason_key = (turn_outcome.denied_reasons or {}).keys()
            reason = next(iter(reason_key), None) or "source_turn_not_found"
            denied_reasons[reason] = denied_reasons.get(reason, 0) + 1
        else:
            turn_packet = turn_outcome.packets[0]
            turn_packet.retrieval_rank = len(packets)
            turn_packet.retrieval_score = float(1000 - len(packets))
            packets.append(turn_packet)
    for rank, packet in enumerate(packets):
        packet.retrieval_rank = rank
        packet.retrieval_score = float(1000 - rank)
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=total, visible=len(packets),
        denied=denied, denied_reasons=denied_reasons,
        depth_applied=1, limit_applied=len(packets),
        source_ids=packet_source_ids(packets),
        latency_ms=(time.monotonic() - started) * 1000,
    )
    _log_query("lookup_world_fact", campaign, depth=1, limit=len(packets),
               outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Timeline (domain events) ─────────────────────────────────────────────────

def query_timeline(
    db: Session,
    campaign_id: Any,
    viewer_user_id: Any = None,
    *,
    event_types: list[str] | str | None = None,
    from_sequence: Any = None,
    to_sequence: Any = None,
    limit: Any = TIMELINE_DEFAULT_LIMIT,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Bounded historical event range over authoritative domain events.

    ``event_types`` is matched against a fixed allowlist of known world /
    campaign event prefixes — never free-form SQL. Player-facing callers see
    the member feed (public events plus their own actor events);
    DM-internal retrieval preserves every event's visibility metadata.
    """
    started = time.monotonic()
    limit_applied = clamp_limit(limit, default=RETRIEVAL_DEFAULT_LIMIT, maximum=RETRIEVAL_MAX_LIMIT)
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    wanted: list[str] | None = None
    if event_types is not None:
        raw = [event_types] if isinstance(event_types, str) else list(event_types)
        wanted = []
        for entry in raw[:16]:
            text = str(entry or "").strip().lower()
            if not text or len(text) > 64:
                raise ValueError("event type filters must be 1-64 chars")
            wanted.append(text)
    try:
        from_seq = int(from_sequence) if from_sequence is not None else None
        to_seq = int(to_sequence) if to_sequence is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("sequence bounds must be integers") from exc
    if from_seq is not None and from_seq < 0:
        raise ValueError("from_sequence must be non-negative")
    if to_seq is not None and to_seq < 0:
        raise ValueError("to_sequence must be non-negative")
    if from_seq is not None and to_seq is not None and from_seq > to_seq:
        raise ValueError("from_sequence must not exceed to_sequence")

    query = select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == campaign.id)
    if from_seq is not None:
        query = query.where(CampaignDomainEvent.sequence >= from_seq)
    if to_seq is not None:
        query = query.where(CampaignDomainEvent.sequence <= to_seq)
    if wanted:
        query = query.where(sqlalchemy_or(*[
            CampaignDomainEvent.event_type.ilike(f"{part}%") for part in wanted
        ]))
    rows = list(db.execute(
        query.order_by(CampaignDomainEvent.sequence.asc()).limit(limit_applied + 1)
    ).scalars().all())

    outcome = RetrievalOutcome(depth_applied=0, limit_applied=limit_applied)
    outcome.total = len(rows)
    packets: list[EvidencePacket] = []
    for event in rows:
        gate = (True, None) if dm_internal else event_visible_player_facing(db, campaign, event, viewers)
        if not gate[0]:
            outcome.denied += 1
            outcome.denied_reasons[gate[1] or "denied"] = outcome.denied_reasons.get(gate[1] or "denied", 0) + 1
            continue
        packets.append(event_packet(event, len(packets),
                                     revealable=None if dm_internal else True))
    if len(packets) > limit_applied:
        packets = packets[:limit_applied]
        outcome.truncated = True
    for rank, packet in enumerate(packets):
        packet.retrieval_rank = rank
        packet.retrieval_score = float(1000 - rank)
    outcome.packets = packets
    outcome.visible = len(packets)
    outcome.source_ids = packet_source_ids(packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("query_world_timeline", campaign, depth=0, limit=limit_applied,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Source turns + submissions ───────────────────────────────────────────────

def lookup_source_turn(
    db: Session,
    campaign_id: Any,
    turn_id: Any,
    viewer_user_id: Any = None,
    *,
    include_submissions: bool = True,
    include_established_records: bool = True,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Retrieve the source turn that established world state, with its player
    submissions and the relation/fact rows that cite it as provenance."""
    from app.world.facts import list_records_for_source_turn

    started = time.monotonic()
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    try:
        tid = coerce_uuid(turn_id, field="turn_id")
        turn = db.get(DmTurn, tid)
        if turn is None or turn.campaign_id != campaign.id:
            raise ValueError(f"Source turn {tid} not found in campaign {campaign.id}")
    except ValueError as exc:
        return _not_found("lookup_source_turn", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = turn_gate(db, campaign, turn, viewers, dm_internal=dm_internal)
    outcome = RetrievalOutcome(depth_applied=1, limit_applied=1)
    outcome.total = 1
    if not allowed:
        outcome.denied = 1
        outcome.denied_reasons[reason or "denied"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("lookup_source_turn", campaign, depth=1, limit=1,
                   outcome=outcome, dm_internal=dm_internal)
        return outcome

    submission_dicts: list[dict[str, Any]] = []
    if include_submissions:
        for raw_sid in list(getattr(turn, "submission_ids", None) or []):
            try:
                sid = coerce_uuid(raw_sid, field="submission_id")
            except ValueError:
                continue
            submission = db.get(PlayerSubmission, sid)
            if submission is None or submission.campaign_id != campaign.id:
                continue
            gate_ok, _ = submission_gate(db, campaign, submission, viewers,
                                          dm_internal=dm_internal)
            if not gate_ok:
                outcome.denied += 1
                outcome.denied_reasons["submission_not_visible"] = outcome.denied_reasons.get(
                    "submission_not_visible", 0) + 1
                continue
            submission_dicts.append(submission.to_dict())
            outcome.total += 1
    records: dict[str, list[str]] = {"relation_ids": [], "fact_ids": []}
    if include_established_records:
        grouped = list_records_for_source_turn(db, campaign.id, turn.id)
        for relation in grouped.get("relations", []):
            gate_ok, _ = authorize_world_record(
                db, campaign, "relation", relation.id, viewers, dm_internal=dm_internal)
            if not gate_ok:
                outcome.denied += 1
                outcome.denied_reasons["record_not_visible"] = outcome.denied_reasons.get(
                    "record_not_visible", 0) + 1
                continue
            records["relation_ids"].append(str(relation.id))
            outcome.total += 1
        for fact in grouped.get("facts", []):
            gate_ok, _ = authorize_world_record(
                db, campaign, "fact", fact.id, viewers, dm_internal=dm_internal)
            if not gate_ok:
                outcome.denied += 1
                outcome.denied_reasons["record_not_visible"] = outcome.denied_reasons.get(
                    "record_not_visible", 0) + 1
                continue
            records["fact_ids"].append(str(fact.id))
            outcome.total += 1
    outcome.packets = [turn_packet(
        turn, 0, revealable=None if dm_internal else True,
        submissions=submission_dicts, records=records)]
    outcome.visible = 1
    outcome.source_ids = packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_source_turn", campaign, depth=1, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Character / NPC knowledge ────────────────────────────────────────────────

def _knowledge_target_id_of_row(row: Any) -> tuple[str, str]:
    kind = str(getattr(row, "target_kind", ""))
    if kind == "fact" and getattr(row, "target_fact_id", None):
        return kind, str(row.target_fact_id)
    if kind == "relation" and getattr(row, "target_relation_id", None):
        return kind, str(row.target_relation_id)
    if kind == "entity" and getattr(row, "target_entity_id", None):
        return kind, str(row.target_entity_id)
    return kind, ""


def _knowledge_entry_from_row(db: Session, row: Any, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Campaign-scoped internal entry: most-restrictive visibility, target snapshot.

    The packet visibility is at least as restrictive as BOTH the knowledge
    row and the embedded truth-target record, so a ``campaign``-visible
    knowledge row pointing at a ``dm_only`` fact stays ``dm_only`` and can
    never become narration-eligible through evidence mediation.
    """
    from models.world import WorldEntity, WorldFact, WorldRelation

    kind, target_id = _knowledge_target_id_of_row(row)
    target: dict[str, Any] | None = None
    target_visibility: Any = None
    try:
        record = None
        if kind == "fact" and getattr(row, "target_fact_id", None):
            record = db.get(WorldFact, row.target_fact_id)
        elif kind == "relation" and getattr(row, "target_relation_id", None):
            record = db.get(WorldRelation, row.target_relation_id)
        elif kind == "entity" and getattr(row, "target_entity_id", None):
            record = db.get(WorldEntity, row.target_entity_id)
        if record is not None and getattr(record, "campaign_id", None) == campaign_id:
            target = record.to_dict()
            target_visibility = getattr(record, "visibility", None)
    except Exception:
        target = None
        target_visibility = None
    row_visibility = getattr(row, "visibility", "dm_only")
    visibility = most_restrictive(row_visibility, target_visibility or row_visibility)
    return {
        "knowledge_id": str(row.id),
        "subject_kind": getattr(row, "subject_kind", None),
        "subject_entity_id": str(getattr(row, "subject_entity_id", "")),
        "target_kind": kind,
        "target_id": target_id,
        "knowledge_state": getattr(row, "knowledge_state", "believes"),
        "acquisition_source": getattr(row, "acquisition_source", None),
        "visibility": visibility,
        "target_visibility": visibility_or_dm_only(target_visibility or row_visibility),
        "target": target,
        "campaign_id": str(campaign_id),
    }


def _enrich_entries_with_row_visibility(
    db: Session, campaign_id: uuid.UUID, entries: list[dict[str, Any]]
) -> None:
    """Attach the most-restrictive real visibility to authorized entries.

    Only called for entries a human projection already authorized, so no
    hidden row is introduced — this preserves the stricter of the knowledge
    row's and the embedded target's visibility instead of erasing it to a
    hardcoded default.
    """
    from models.world import WorldKnowledge

    for entry in entries:
        try:
            row = db.get(WorldKnowledge, coerce_optional_uuid(entry.get("knowledge_id")))
        except Exception:
            row = None
        if row is not None and getattr(row, "campaign_id", None) == campaign_id:
            row_vis = getattr(row, "visibility", "dm_only")
            target = entry.get("target") if isinstance(entry.get("target"), dict) else None
            target_vis = (target or {}).get("visibility", row_vis)
            entry["visibility"] = most_restrictive(row_vis, target_vis)


def query_character_knowledge(
    db: Session,
    campaign_id: Any,
    subject_entity_id: Any,
    viewer_user_id: Any = None,
    *,
    knowledge_state: str | None = None,
    limit: Any = RETRIEVAL_DEFAULT_LIMIT,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """What may the viewer see of one subject's fictional knowledge?

    Player-facing retrieval wraps the #211 ``what_does_subject_know``
    projection (subject, knowledge row, and truth target authorized
    independently). DM-internal retrieval bypasses human disclosure
    filtering while remaining campaign-scoped, returning every knowledge
    row for the subject with its real visibility metadata preserved.
    """
    from app.world.knowledge import validate_knowledge_state, what_does_subject_know

    started = time.monotonic()
    limit_applied = clamp_limit(limit, default=RETRIEVAL_DEFAULT_LIMIT, maximum=RETRIEVAL_MAX_LIMIT)
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    if knowledge_state is not None:
        validate_knowledge_state(knowledge_state)
    try:
        subject_id = coerce_uuid(subject_entity_id, field="subject_entity_id")
    except ValueError as exc:
        return _not_found("query_character_knowledge", campaign, depth=0,
                          limit=limit_applied, detail=str(exc))
    if dm_internal:
        from app.world.knowledge import list_knowledge_for_subject
        from models.world import WorldEntity

        subject = db.get(WorldEntity, subject_id)
        if subject is None or subject.campaign_id != campaign.id:
            return _not_found("query_character_knowledge", campaign, depth=0,
                              limit=limit_applied,
                              detail=f"Subject entity {subject_id} not found")
        rows = list_knowledge_for_subject(
            db, campaign.id, subject_id,
            knowledge_state=knowledge_state, limit=limit_applied + 1)
        total_rows = len(rows)
        truncated = total_rows > limit_applied
        rows = rows[:limit_applied]
        entries = [_knowledge_entry_from_row(db, row, campaign.id) for row in rows]
        packets = [
            knowledge_packet(entry, rank, revealable=None)
            for rank, entry in enumerate(entries)
        ]
        outcome = RetrievalOutcome(
            status=STATUS_OK, packets=packets, total=total_rows,
            visible=len(packets), denied=0, denied_reasons={},
            depth_applied=0, limit_applied=limit_applied, truncated=truncated,
            source_ids=packet_source_ids(packets),
            latency_ms=(time.monotonic() - started) * 1000,
        )
        _log_query("query_character_knowledge", campaign, depth=0,
                   limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
        return outcome
    if len(viewers) == 1:
        effective_viewer: Any = viewers[0]
    elif not viewers:
        outcome = RetrievalOutcome(depth_applied=0, limit_applied=limit_applied)
        outcome.denied = 1
        outcome.denied_reasons["viewer_required"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("query_character_knowledge", campaign, depth=0,
                   limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
        return outcome
    else:
        # Multi-viewer player-facing projection: intersect by running the
        # single-viewer projection per viewer and keeping entries visible to
        # all of them (fail closed, no id/content leak for denied rows).
        return _query_character_knowledge_multi(
            db, campaign, subject_id, viewers, started, limit_applied,
            knowledge_state=knowledge_state)

    projection = what_does_subject_know(
        db, campaign, subject_id, effective_viewer,
        knowledge_state=knowledge_state, include_target=True, limit=limit_applied + 1)
    entries = list(projection.get("entries", []))
    truncated = len(entries) > limit_applied
    entries = entries[:limit_applied]
    _enrich_entries_with_row_visibility(db, campaign.id, entries)
    packets = [
        knowledge_packet({**entry, "campaign_id": str(campaign.id)}, rank,
                          revealable=True)
        for rank, entry in enumerate(entries)
    ]
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=int(projection.get("total", 0)),
        visible=len(packets), denied=int(projection.get("denied", 0)),
        denied_reasons=dict(projection.get("denied_reasons", {})),
        depth_applied=0, limit_applied=limit_applied, truncated=truncated,
        source_ids=packet_source_ids(packets),
        latency_ms=(time.monotonic() - started) * 1000,
    )
    _log_query("query_character_knowledge", campaign, depth=0,
               limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
    return outcome


def _query_character_knowledge_multi(
    db: Session, campaign: Campaign, subject_id: uuid.UUID,
    viewers: list[uuid.UUID], started: float, limit_applied: int,
    *, knowledge_state: str | None,
) -> RetrievalOutcome:
    from app.world.knowledge import what_does_subject_know

    per_viewer = [
        what_does_subject_know(
            db, campaign, subject_id, viewer, knowledge_state=knowledge_state,
            include_target=True, limit=limit_applied + 1)
        for viewer in viewers
    ]
    if any(p.get("denied_reasons", {}).get("subject_not_visible") for p in per_viewer):
        outcome = RetrievalOutcome(depth_applied=0, limit_applied=limit_applied)
        outcome.denied = 1
        outcome.denied_reasons["subject_not_visible"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("query_character_knowledge", campaign, depth=0,
                   limit=limit_applied, outcome=outcome, dm_internal=False)
        return outcome
    common = {e["knowledge_id"] for e in per_viewer[0].get("entries", [])}
    for projection in per_viewer[1:]:
        common &= {e["knowledge_id"] for e in projection.get("entries", [])}
    entries = [e for e in per_viewer[0].get("entries", []) if e["knowledge_id"] in common]
    truncated = len(entries) > limit_applied
    entries = entries[:limit_applied]
    denied_total = max((int(p.get("total", 0)) - len(entries) for p in per_viewer), default=0)
    _enrich_entries_with_row_visibility(db, campaign.id, entries)
    packets = [
        knowledge_packet({**entry, "campaign_id": str(campaign.id)}, rank,
                          revealable=True)
        for rank, entry in enumerate(entries)
    ]
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=int(per_viewer[0].get("total", 0)),
        visible=len(packets), denied=max(0, denied_total),
        denied_reasons={"target_not_visible": max(0, denied_total)} if denied_total else {},
        depth_applied=0, limit_applied=limit_applied, truncated=truncated,
        source_ids=packet_source_ids(packets),
        latency_ms=(time.monotonic() - started) * 1000,
    )
    _log_query("query_character_knowledge", campaign, depth=0,
               limit=limit_applied, outcome=outcome, dm_internal=False)
    return outcome


# ── #203 evidence-tool handlers ──────────────────────────────────────────────

def _require_db(db: Any) -> Session | dict[str, Any]:
    if db is None:
        return {
            "status": "unknown",
            "sources": [],
            "visibility": "campaign",
            "authorization": {"campaign_id": "unknown", "thread_ids": []},
            "payload": {"retrieval_status": STATUS_DEFER,
                        "error": "world retrieval requires a database session"},
            "result_count": 0,
        }
    return db


def _bundle_result(audience: Any, outcome: RetrievalOutcome, *, dm_internal: bool) -> dict[str, Any]:
    packets = outcome.packets
    if outcome.status == STATUS_NOT_FOUND and not packets:
        status = "missing"
    elif outcome.status in {STATUS_DEFER, STATUS_NO_MATCH}:
        status = "unknown"
    else:
        status = "ok" if packets else "unknown"
    payload = {
        "retrieval_status": outcome.status,
        "packets": [p.to_dict() for p in packets],
        "total": outcome.total,
        "visible": outcome.visible,
        "denied": outcome.denied,
        "denied_reasons": dict(outcome.denied_reasons),
        "depth_applied": outcome.depth_applied,
        "limit_applied": outcome.limit_applied,
        "truncated": outcome.truncated,
        "latency_ms": outcome.latency_ms,
    }
    return tool_result(audience, status=status, packets=packets, payload=payload,
                       dm_internal=dm_internal)


def handle_lookup_world_entity(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_world_entity requires query (entity id)")
    outcome = retrieve_entity(
        session, getattr(audience, "campaign_id", None), query,
        audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


def handle_traverse_world_relations(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("traverse_world_relations requires query (entity id)")
    outcome = traverse_relations(
        session, getattr(audience, "campaign_id", None), query,
        audience_viewers(audience), limit=getattr(req, "limit", None),
        dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


def handle_lookup_world_fact(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_world_fact requires query (fact id)")
    outcome = fact_source_evidence(
        session, getattr(audience, "campaign_id", None), query,
        audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


def handle_query_world_timeline(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip() or None
    outcome = query_timeline(
        session, getattr(audience, "campaign_id", None),
        audience_viewers(audience), event_types=query,
        limit=getattr(req, "limit", None), dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


def handle_lookup_source_turn(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_source_turn requires query (turn id)")
    outcome = lookup_source_turn(
        session, getattr(audience, "campaign_id", None), query,
        audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


def handle_query_character_knowledge(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    character_id = getattr(req, "character_id", None)
    subject = query or (str(character_id).strip() if character_id else "")
    if not subject:
        raise ValueError("query_character_knowledge requires query (subject entity id)")
    outcome = query_character_knowledge(
        session, getattr(audience, "campaign_id", None), subject,
        audience_viewers(audience), limit=getattr(req, "limit", None),
        dm_internal=dm_internal)
    return _bundle_result(audience, outcome, dm_internal=dm_internal)


TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "lookup_world_entity": handle_lookup_world_entity,
    "traverse_world_relations": handle_traverse_world_relations,
    "lookup_world_fact": handle_lookup_world_fact,
    "query_world_timeline": handle_query_world_timeline,
    "lookup_source_turn": handle_lookup_source_turn,
    "query_character_knowledge": handle_query_character_knowledge,
}
