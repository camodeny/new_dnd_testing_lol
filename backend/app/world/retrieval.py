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
- Semantic reranking (#380/#381) is an optional post-filter hook: it
  receives only the already-authorized bounded candidate set and may return
  only those candidate IDs (or an explicit defer). It can reorder
  presentation but never changes the authoritative source record, version,
  or provenance attached to a packet. Reranker failure falls back to
  deterministic retrieval order — authoritative evidence is never lost.
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
from models.campaigns import Campaign, CampaignDomainEvent
from models.dm import DmTurn, DmTurnAttempt
from models.threads import PlayerSubmission

logger = logging.getLogger(__name__)

RETRIEVAL_SOURCE = "world_retrieval_212"

# ── Bounds (observable; every outcome reports the applied values) ────────────

RETRIEVAL_DEFAULT_LIMIT = 20
RETRIEVAL_MAX_LIMIT = 50
RETRIEVAL_DEFAULT_DEPTH = 1
RETRIEVAL_MAX_DEPTH = 3
TIMELINE_DEFAULT_LIMIT = 20

# Evidence tools exposed through the #203 mediation interface.
WORLD_TOOL_NAMES = frozenset({
    "lookup_world_entity",
    "traverse_world_relations",
    "lookup_world_fact",
    "query_world_timeline",
    "lookup_source_turn",
    "query_character_knowledge",
})

# Outcome statuses. ``not_found`` / ``defer`` / ``no_match`` are explicit
# evidence states — never fabricated substitutes.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_DEFER = "defer"
STATUS_NO_MATCH = "no_match"
STATUS_PARTIAL = "partial"

# Reranker escape: mirrors the decisions DEFER candidate without importing
# the decisions package at module load (core retrieval must not block on
# #380/#381 availability).
RERANK_DEFER_ID = "DEFER"
RERANK_NO_MATCH_ID = "NO_MATCH"


# ── Typed evidence packet ────────────────────────────────────────────────────

@dataclass
class EvidencePacket:
    """One normalized authoritative evidence item.

    ``source_type``/``source_id``/``source_version`` form the stable source
    identity used by validators and audit tooling. ``retrieval_rank`` is the
    deterministic retrieval order; ``presentation_rank`` is set only by the
    optional rerank hook (``None`` means retrieval order stands).
    ``revealable`` is meaningful in player-facing mode (True = authorized
    for the viewer); in DM-internal mode it is None (deferred to later
    projection) while ``visibility`` is always preserved.
    """

    source_type: str
    source_id: str
    source_version: str
    content: dict[str, Any]
    epistemic_state: str | None
    visibility: str
    campaign_id: str
    revision_or_sequence: int | None
    provenance: dict[str, Any]
    retrieval_rank: int = 0
    retrieval_score: float = 0.0
    presentation_rank: int | None = None
    revealable: bool | None = None
    denial_reason: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content": self.content,
            "epistemic_state": self.epistemic_state,
            "visibility": self.visibility,
            "campaign_id": self.campaign_id,
            "revision_or_sequence": self.revision_or_sequence,
            "provenance": self.provenance,
            "retrieval_rank": self.retrieval_rank,
            "retrieval_score": self.retrieval_score,
            "presentation_rank": self.presentation_rank,
            "revealable": self.revealable,
            "denial_reason": self.denial_reason,
            "created_at": self.created_at,
        }

    def to_source_ref(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "campaign_revision": self.revision_or_sequence,
            "provenance": dict(self.provenance),
        }


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


@dataclass
class RerankedOutcome:
    """Result of the optional semantic rerank hook.

    ``packets`` always carries the full authorized set in presentation order
    (empty only for defer/no-match); every packet keeps its original
    ``retrieval_rank``/source identity/provenance — reranking only sets
    ``presentation_rank``. ``fallback`` is True when the reranker failed or
    returned invented IDs and deterministic order was kept instead.
    """

    status: str = STATUS_OK
    packets: list[EvidencePacket] = field(default_factory=list)
    presented_ids: list[str] = field(default_factory=list)
    reordered_ids: list[str] = field(default_factory=list)
    reranked: bool = False
    fallback: bool = False
    reranker_model: str | None = None
    reranker_policy: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "packets": [p.to_dict() for p in self.packets],
            "presented_ids": list(self.presented_ids),
            "reordered_ids": list(self.reordered_ids),
            "reranked": self.reranked,
            "fallback": self.fallback,
            "reranker_model": self.reranker_model,
            "reranker_policy": self.reranker_policy,
            "error": self.error,
        }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit if limit is not None else RETRIEVAL_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = RETRIEVAL_DEFAULT_LIMIT
    return max(1, min(value, RETRIEVAL_MAX_LIMIT))


def _clamp_depth(depth: Any) -> int:
    try:
        value = int(depth if depth is not None else RETRIEVAL_DEFAULT_DEPTH)
    except (TypeError, ValueError):
        value = RETRIEVAL_DEFAULT_DEPTH
    return max(0, min(value, RETRIEVAL_MAX_DEPTH))


def _coerce_uuid(value: Any, *, field_name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} {value!r}") from exc


def _coerce_optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _coerce_uuid(value, field_name="id")


def _packet_source_ids(packets: list[EvidencePacket]) -> list[str]:
    return sorted({f"{p.source_type}:{p.source_id}@{p.source_version}" for p in packets})


def _provenance(
    record: Any, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Code-owned provenance built only from persisted record fields."""
    prov: dict[str, Any] = {"retrieved_by": RETRIEVAL_SOURCE}
    for attr in ("source_turn_id", "source_attempt_id", "source_event_id",
                 "actor_id", "operation_id", "idempotency_key"):
        value = getattr(record, attr, None)
        if value is not None:
            prov[attr] = str(value)
    stored = getattr(record, "provenance", None)
    if isinstance(stored, dict):
        for key, value in stored.items():
            prov.setdefault(f"record_{key}", value)
    if extra:
        for key, value in extra.items():
            prov.setdefault(key, value)
    return prov


def _version_of(record: Any, *, fallback: str = "1") -> str:
    version = getattr(record, "version", None)
    if version is not None:
        return f"v{int(version)}"
    updated = getattr(record, "updated_at", None)
    if updated is not None:
        try:
            return updated.isoformat()
        except Exception:
            pass
    return fallback


def _created_iso(record: Any) -> str | None:
    created = getattr(record, "created_at", None)
    try:
        return created.isoformat() if created else None
    except Exception:
        return None


def _resolve_campaign(db: Session, campaign_id: Any) -> Campaign:
    cid = _coerce_uuid(campaign_id, field_name="campaign_id")
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise ValueError(f"Campaign {cid} not found")
    return campaign


def _resolve_viewers(viewer_user_id: Any) -> list[uuid.UUID]:
    """Normalize one viewer id or a list of them (fail-closed on garbage)."""
    if viewer_user_id is None:
        return []
    raw = viewer_user_id if isinstance(viewer_user_id, (list, tuple)) else [viewer_user_id]
    viewers: list[uuid.UUID] = []
    for value in raw:
        try:
            viewers.append(_coerce_uuid(value, field_name="viewer_user_id"))
        except ValueError:
            continue
    seen: set[str] = set()
    unique: list[uuid.UUID] = []
    for viewer in viewers:
        if str(viewer) not in seen:
            seen.add(str(viewer))
            unique.append(viewer)
    return unique


def _membership(db: Session, campaign: Campaign, viewer: uuid.UUID) -> bool:
    from app.campaigns.service import is_campaign_member

    return campaign.owner_id == viewer or is_campaign_member(db, campaign.id, viewer)


def _log_query(
    query_type: str,
    campaign: Campaign,
    *,
    depth: int,
    limit: int,
    outcome: RetrievalOutcome,
    dm_internal: bool,
    rerank: dict[str, Any] | None = None,
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
        **(rerank or {}),
    )


def _not_found(query_type: str, campaign: Campaign, *, depth: int, limit: int,
               detail: str) -> RetrievalOutcome:
    outcome = RetrievalOutcome(
        status=STATUS_NOT_FOUND, depth_applied=depth, limit_applied=limit, error=detail,
    )
    _log_query(query_type, campaign, depth=depth, limit=limit,
               outcome=outcome, dm_internal=False)
    return outcome


# ── Packet builders (one per record kind; visibility always preserved) ───────

def _entity_packet(entity: Any, campaign_id: uuid.UUID, rank: int,
                   *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_entity",
        source_id=str(entity.id),
        source_version=_version_of(entity),
        content=entity.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(entity, "visibility", "campaign")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=_provenance(entity),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(entity),
    )


def _relation_packet(relation: Any, campaign_id: uuid.UUID, rank: int,
                     *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_relation",
        source_id=str(relation.id),
        source_version=_version_of(relation),
        content=relation.to_dict(),
        epistemic_state=str(getattr(relation, "epistemic_state", "claimed")),
        visibility=str(getattr(relation, "visibility", "dm_only")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=_provenance(relation),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(relation),
    )


def _fact_packet(fact: Any, campaign_id: uuid.UUID, rank: int,
                 *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_fact",
        source_id=str(fact.id),
        source_version=_version_of(fact),
        content=fact.to_dict(),
        epistemic_state=str(getattr(fact, "epistemic_state", "claimed")),
        visibility=str(getattr(fact, "visibility", "dm_only")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=_provenance(fact),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(fact),
    )


def _event_packet(event: CampaignDomainEvent, rank: int,
                  *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="domain_event",
        source_id=str(event.id),
        source_version=f"seq{int(event.sequence)}",
        content=event.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(event, "visibility", "public")),
        campaign_id=str(event.campaign_id),
        revision_or_sequence=int(event.sequence),
        provenance=_provenance(event, extra={"event_type": event.event_type}),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(event),
    )


def _turn_packet(turn: DmTurn, rank: int, *,
                 revealable: bool | None,
                 submissions: list[dict[str, Any]] | None = None,
                 records: dict[str, list[str]] | None = None) -> EvidencePacket:
    audience = str(getattr(turn, "audience", "campaign") or "campaign")
    visibility = audience if audience in {"public", "campaign", "private", "dm_only"} else "campaign"
    content = turn.to_dict()
    if submissions is not None:
        content = {**content, "submissions": submissions}
    if records is not None:
        content = {**content, "established_records": records}
    return EvidencePacket(
        source_type="source_turn",
        source_id=str(turn.id),
        source_version=_version_of(turn),
        content=content,
        epistemic_state=None,
        visibility=visibility,
        campaign_id=str(turn.campaign_id),
        revision_or_sequence=int(getattr(turn, "source_revision", 0) or 0),
        provenance=_provenance(turn, extra={"thread_id": str(getattr(turn, "thread_id", ""))}),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(turn),
    )


def _submission_packet(submission: PlayerSubmission, rank: int,
                       *, revealable: bool | None) -> EvidencePacket:
    audience = str(getattr(submission, "audience", "campaign") or "campaign")
    visibility = audience if audience in {"public", "campaign", "private", "dm_only"} else "campaign"
    return EvidencePacket(
        source_type="submission",
        source_id=str(submission.id),
        source_version=f"seq{int(submission.sequence)}",
        content=submission.to_dict(),
        epistemic_state=None,
        visibility=visibility,
        campaign_id=str(submission.campaign_id),
        revision_or_sequence=int(submission.sequence),
        provenance=_provenance(submission, extra={
            "author_user_id": str(submission.user_id),
            "thread_id": str(submission.thread_id),
        }),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(submission),
    )


def _scene_packet(scene: Any, rank: int, *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="scene",
        source_id=str(scene.campaign_id),
        source_version=f"r{int(getattr(scene, 'revision', 0) or 0)}",
        content=scene.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(scene, "visibility", "campaign")),
        campaign_id=str(scene.campaign_id),
        revision_or_sequence=int(getattr(scene, "revision", 0) or 0),
        provenance=_provenance(scene),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(scene),
    )


def _knowledge_packet(entry: dict[str, Any], rank: int,
                      *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="knowledge",
        source_id=str(entry.get("knowledge_id", f"knowledge:{rank}")),
        source_version="1",
        content=dict(entry),
        epistemic_state=str(entry.get("knowledge_state", "believes")),
        visibility=str(entry.get("visibility", "dm_only") or "dm_only"),
        campaign_id=str(entry.get("campaign_id", "")),
        revision_or_sequence=None,
        provenance={"retrieved_by": RETRIEVAL_SOURCE,
                    "subject_entity_id": str(entry.get("subject_entity_id", "")),
                    "target_kind": str(entry.get("target_kind", "")),
                    "target_id": str(entry.get("target_id", ""))},
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=None,
    )


# ── Authorization gates ──────────────────────────────────────────────────────

def _authorize_world_record(
    db: Session, campaign: Campaign, kind: str, record_id: uuid.UUID,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    """Return (allowed, denial_reason) for one world record.

    DM-internal mode never denies here — visibility metadata travels on the
    packet for later projection. Player-facing mode requires membership plus
    an explicit ``may_user_receive`` allow for EVERY listed viewer
    (intersection: the most restrictive viewer wins, fail closed).
    """
    from app.world.epistemics import may_user_receive

    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not _membership(db, campaign, viewer):
            return False, "not_campaign_member"
        verdict = may_user_receive(db, campaign, kind, record_id, viewer)
        if not verdict.get("allowed"):
            return False, str(verdict.get("reason", "denied"))
    return True, None


def _event_visible_player_facing(db: Session, campaign: Campaign,
                                  event: CampaignDomainEvent,
                                  viewers: list[uuid.UUID]) -> tuple[bool, str | None]:
    """Member feed rule mirroring ``list_campaign_events``: public events,
    plus a viewer's own actor events. Anything else stays hidden.

    Every player-facing viewer must first pass campaign membership — an
    arbitrary user UUID plus a known campaign ID must not read that
    campaign's public timeline.
    """
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not _membership(db, campaign, viewer):
            return False, "not_campaign_member"
    if str(getattr(event, "visibility", "public")) == "public":
        return True, None
    actor = getattr(event, "actor_id", None)
    if actor is not None and any(str(actor) == str(viewer) for viewer in viewers):
        return True, None
    return False, "event_not_visible"


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
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        eid = _coerce_uuid(entity_id, field_name="entity_id")
        entity = get_entity_strict(db, campaign.id, eid)
    except ValueError as exc:
        return _not_found("lookup_world_entity", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = _authorize_world_record(
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
    outcome.packets = [_entity_packet(
        entity, campaign.id, 0,
        revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = _packet_source_ids(outcome.packets)
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
    limit_applied = _clamp_limit(limit)
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        root_id = _coerce_uuid(root_entity_id, field_name="entity_id")
        root = get_entity_strict(db, campaign.id, root_id)
    except ValueError as exc:
        return _not_found("traverse_world_relations", campaign, depth=depth_applied,
                          limit=limit_applied, detail=str(exc))

    outcome = RetrievalOutcome(depth_applied=depth_applied, limit_applied=limit_applied)
    allowed, reason = _authorize_world_record(
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

    root_packet = _entity_packet(root, campaign.id, 0,
                                 revealable=None if dm_internal else True)
    packets[0] = root_packet

    from app.world.knowledge import list_relations
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
                allowed_rel, reason_rel = _authorize_world_record(
                    db, campaign, "relation", relation.id, viewers,
                    dm_internal=dm_internal)
                if not allowed_rel:
                    denied_reasons[reason_rel or "denied"] = denied_reasons.get(reason_rel or "denied", 0) + 1
                    continue
                relation_ids.add(str(relation.id))
                packets.append(_relation_packet(
                    relation, campaign.id, len(packets),
                    revealable=None if dm_internal else True))
                for neighbor_id in (relation.subject_entity_id, relation.object_entity_id):
                    if neighbor_id is None or str(neighbor_id) in visited_entities:
                        continue
                    neighbor = db.get(WorldEntity, neighbor_id)
                    if neighbor is None or neighbor.campaign_id != campaign.id:
                        continue
                    total_seen += 1
                    allowed_ent, reason_ent = _authorize_world_record(
                        db, campaign, "entity", neighbor.id, viewers,
                        dm_internal=dm_internal)
                    if not allowed_ent:
                        denied_reasons[reason_ent or "denied"] = denied_reasons.get(reason_ent or "denied", 0) + 1
                        continue
                    visited_entities.add(str(neighbor.id))
                    packets.append(_entity_packet(
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
    outcome.source_ids = _packet_source_ids(packets)
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
    from app.world.knowledge import get_fact_strict

    started = time.monotonic()
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        fid = _coerce_uuid(fact_id, field_name="fact_id")
        fact = get_fact_strict(db, campaign.id, fid)
    except ValueError as exc:
        return _not_found("lookup_world_fact", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = _authorize_world_record(
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
    outcome.packets = [_fact_packet(
        fact, campaign.id, 0, revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = _packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_world_fact", campaign, depth=0, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


def facts_for_entity(
    db: Session,
    campaign_id: Any,
    entity_id: Any,
    viewer_user_id: Any = None,
    *,
    limit: Any = RETRIEVAL_DEFAULT_LIMIT,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """All active facts referencing one canonical entity (bounded)."""
    from app.world.knowledge import list_facts
    from app.world.service import get_entity_strict

    started = time.monotonic()
    limit_applied = _clamp_limit(limit)
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        eid = _coerce_uuid(entity_id, field_name="entity_id")
        get_entity_strict(db, campaign.id, eid)
    except ValueError as exc:
        return _not_found("lookup_world_fact", campaign, depth=0,
                          limit=limit_applied, detail=str(exc))
    rows = list_facts(db, campaign.id, entity_id=eid, limit=limit_applied + 1)
    outcome = RetrievalOutcome(depth_applied=0, limit_applied=limit_applied)
    outcome.total = len(rows)
    packets: list[EvidencePacket] = []
    for row in rows:
        allowed, reason = _authorize_world_record(
            db, campaign, "fact", row.id, viewers, dm_internal=dm_internal)
        if not allowed:
            outcome.denied += 1
            outcome.denied_reasons[reason or "denied"] = outcome.denied_reasons.get(reason or "denied", 0) + 1
            continue
        packets.append(_fact_packet(row, campaign.id, len(packets),
                                    revealable=None if dm_internal else True))
    if len(packets) > limit_applied:
        packets = packets[:limit_applied]
        outcome.truncated = True
    for rank, packet in enumerate(packets):
        packet.retrieval_rank = rank
        packet.retrieval_score = float(1000 - rank)
    outcome.packets = packets
    outcome.visible = len(packets)
    outcome.source_ids = _packet_source_ids(packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_world_fact", campaign, depth=0, limit=limit_applied,
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
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
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
        event = db.get(CampaignDomainEvent, _coerce_optional_uuid(event_ref))
        if event is None or event.campaign_id != campaign.id:
            denied += 1
            denied_reasons["source_event_not_found"] = denied_reasons.get("source_event_not_found", 0) + 1
        else:
            gate = (True, None) if dm_internal else _event_visible_player_facing(db, campaign, event, viewers)
            if not gate[0]:
                denied += 1
                denied_reasons[gate[1] or "denied"] = denied_reasons.get(gate[1] or "denied", 0) + 1
            else:
                packets.append(_event_packet(event, len(packets),
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
        source_ids=_packet_source_ids(packets),
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
    limit_applied = _clamp_limit(limit)
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
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
        gate = (True, None) if dm_internal else _event_visible_player_facing(db, campaign, event, viewers)
        if not gate[0]:
            outcome.denied += 1
            outcome.denied_reasons[gate[1] or "denied"] = outcome.denied_reasons.get(gate[1] or "denied", 0) + 1
            continue
        packets.append(_event_packet(event, len(packets),
                                     revealable=None if dm_internal else True))
    if len(packets) > limit_applied:
        packets = packets[:limit_applied]
        outcome.truncated = True
    for rank, packet in enumerate(packets):
        packet.retrieval_rank = rank
        packet.retrieval_score = float(1000 - rank)
    outcome.packets = packets
    outcome.visible = len(packets)
    outcome.source_ids = _packet_source_ids(packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("query_world_timeline", campaign, depth=0, limit=limit_applied,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Source turns + submissions ───────────────────────────────────────────────

def _thread_readable_for_viewer(
    db: Session, campaign: Campaign, raw_thread_id: Any, viewer: uuid.UUID,
) -> bool:
    """Thread-ACL check for source-turn/submission evidence (read-only).

    Uses the centralized ``can_read_thread`` primitive: shared campaign
    threads require campaign membership; private threads require explicit
    thread membership (owner status alone never grants private access).
    Private content must live on a resolvable thread row — an unresolvable
    thread reference denies fail-closed. (This helper is only invoked for
    private-audience records; shared-audience legacy ``"main"`` turns
    without a durable thread row never reach it.)
    """
    from app.runtime.threads import can_read_thread, parse_thread_id
    from models.threads import CampaignThread
    from sqlalchemy import select as _select

    raw = "" if raw_thread_id is None else str(raw_thread_id)
    if not raw or raw == "main":
        thread = db.execute(
            _select(CampaignThread).where(
                CampaignThread.campaign_id == campaign.id,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        if thread is None:
            return False
        try:
            return bool(can_read_thread(db, campaign.id, thread.id, viewer))
        except Exception:
            return False
    try:
        tid = parse_thread_id(raw)
    except Exception:
        return False
    try:
        return bool(can_read_thread(db, campaign.id, tid, viewer))
    except Exception:
        return False


def _turn_gate(
    db: Session, campaign: Campaign, turn: DmTurn,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not _membership(db, campaign, viewer):
            return False, "not_campaign_member"
    audience = str(getattr(turn, "audience", "campaign") or "campaign")
    if audience == "private":
        for viewer in viewers:
            if not _thread_readable_for_viewer(
                db, campaign, getattr(turn, "thread_id", None), viewer
            ):
                return False, "turn_not_visible"
        return True, None
    return True, None


def _submission_gate(
    db: Session, campaign: Campaign, submission: PlayerSubmission,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not _membership(db, campaign, viewer):
            return False, "not_campaign_member"
    audience = str(getattr(submission, "audience", "campaign") or "campaign")
    if audience == "private":
        for viewer in viewers:
            if not _thread_readable_for_viewer(
                db, campaign, getattr(submission, "thread_id", None), viewer
            ):
                return False, "submission_not_visible"
    return True, None


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
    from app.world.knowledge import list_records_for_source_turn

    started = time.monotonic()
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        tid = _coerce_uuid(turn_id, field_name="turn_id")
        turn = db.get(DmTurn, tid)
        if turn is None or turn.campaign_id != campaign.id:
            raise ValueError(f"Source turn {tid} not found in campaign {campaign.id}")
    except ValueError as exc:
        return _not_found("lookup_source_turn", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = _turn_gate(db, campaign, turn, viewers, dm_internal=dm_internal)
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
                sid = _coerce_uuid(raw_sid, field_name="submission_id")
            except ValueError:
                continue
            submission = db.get(PlayerSubmission, sid)
            if submission is None or submission.campaign_id != campaign.id:
                continue
            gate_ok, _ = _submission_gate(db, campaign, submission, viewers,
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
            gate_ok, _ = _authorize_world_record(
                db, campaign, "relation", relation.id, viewers, dm_internal=dm_internal)
            if not gate_ok:
                outcome.denied += 1
                outcome.denied_reasons["record_not_visible"] = outcome.denied_reasons.get(
                    "record_not_visible", 0) + 1
                continue
            records["relation_ids"].append(str(relation.id))
            outcome.total += 1
        for fact in grouped.get("facts", []):
            gate_ok, _ = _authorize_world_record(
                db, campaign, "fact", fact.id, viewers, dm_internal=dm_internal)
            if not gate_ok:
                outcome.denied += 1
                outcome.denied_reasons["record_not_visible"] = outcome.denied_reasons.get(
                    "record_not_visible", 0) + 1
                continue
            records["fact_ids"].append(str(fact.id))
            outcome.total += 1
    outcome.packets = [_turn_packet(
        turn, 0, revealable=None if dm_internal else True,
        submissions=submission_dicts, records=records)]
    outcome.visible = 1
    outcome.source_ids = _packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_source_turn", campaign, depth=1, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


def lookup_submission(
    db: Session,
    campaign_id: Any,
    submission_id: Any,
    viewer_user_id: Any = None,
    *,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Retrieve one player submission as source evidence (bounded, scoped)."""
    started = time.monotonic()
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    try:
        sid = _coerce_uuid(submission_id, field_name="submission_id")
        submission = db.get(PlayerSubmission, sid)
        if submission is None or submission.campaign_id != campaign.id:
            raise ValueError(f"Submission {sid} not found in campaign {campaign.id}")
    except ValueError as exc:
        return _not_found("lookup_source_turn", campaign, depth=0,
                          limit=1, detail=str(exc))
    allowed, reason = _submission_gate(db, campaign, submission, viewers,
                                       dm_internal=dm_internal)
    outcome = RetrievalOutcome(depth_applied=0, limit_applied=1)
    outcome.total = 1
    if not allowed:
        outcome.denied = 1
        outcome.denied_reasons[reason or "denied"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        _log_query("lookup_source_turn", campaign, depth=0, limit=1,
                   outcome=outcome, dm_internal=dm_internal)
        return outcome
    outcome.packets = [_submission_packet(
        submission, 0, revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = _packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("lookup_source_turn", campaign, depth=0, limit=1,
               outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Current scene ────────────────────────────────────────────────────────────

def retrieve_current_scene(
    db: Session,
    campaign_id: Any,
    viewer_user_id: Any = None,
    *,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Authoritative current-scene value as an evidence packet (None-safe:
    no scene established returns an explicit not-found outcome)."""
    from app.world.service import (
        get_current_scene,
        is_world_authority,
        scene_visible_to_viewer,
    )

    started = time.monotonic()
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    scene = get_current_scene(db, campaign.id)
    if scene is None:
        return _not_found("get_current_scene", campaign, depth=0, limit=1,
                          detail="No current scene established for campaign")
    outcome = RetrievalOutcome(depth_applied=0, limit_applied=1)
    outcome.total = 1
    if not dm_internal:
        if not viewers:
            outcome.denied = 1
            outcome.denied_reasons["viewer_required"] = 1
            outcome.latency_ms = (time.monotonic() - started) * 1000
            _log_query("get_current_scene", campaign, depth=0, limit=1,
                       outcome=outcome, dm_internal=dm_internal)
            return outcome
        authority = any(is_world_authority(campaign, viewer) for viewer in viewers)
        member = all(_membership(db, campaign, viewer) for viewer in viewers)
        if not member or not scene_visible_to_viewer(scene, authority):
            outcome.denied = 1
            outcome.denied_reasons["scene_not_visible"] = 1
            outcome.latency_ms = (time.monotonic() - started) * 1000
            _log_query("get_current_scene", campaign, depth=0, limit=1,
                       outcome=outcome, dm_internal=dm_internal)
            return outcome
    outcome.packets = [_scene_packet(
        scene, 0, revealable=None if dm_internal else True)]
    outcome.visible = 1
    outcome.source_ids = _packet_source_ids(outcome.packets)
    outcome.latency_ms = (time.monotonic() - started) * 1000
    _log_query("get_current_scene", campaign, depth=0, limit=1,
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


def _canonical_knowledge_visibility(value: Any) -> str:
    """Canonical packet visibility: public/campaign/dm_only/private.

    Unknown spellings fail closed to ``dm_only`` (adjudication-only, never
    narration-eligible) rather than guessing broader disclosure.
    """
    s = str(value or "dm_only").strip()
    aliases = {"party": "campaign", "party_known": "campaign", "dm_private": "dm_only"}
    s = aliases.get(s, s)
    if s in {"public", "campaign", "dm_only", "private"}:
        return s
    return "dm_only"


_VISIBILITY_RESTRICTIVENESS = {"public": 0, "campaign": 1, "dm_only": 2, "private": 3}


def _most_restrictive_visibility(first: Any, second: Any) -> str:
    """Most restrictive of two visibility labels (fail closed on unknown)."""
    a = _canonical_knowledge_visibility(first)
    b = _canonical_knowledge_visibility(second)
    if _VISIBILITY_RESTRICTIVENESS.get(b, 2) > _VISIBILITY_RESTRICTIVENESS.get(a, 2):
        return b
    return a


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
    visibility = _most_restrictive_visibility(row_visibility, target_visibility or row_visibility)
    return {
        "knowledge_id": str(row.id),
        "subject_kind": getattr(row, "subject_kind", None),
        "subject_entity_id": str(getattr(row, "subject_entity_id", "")),
        "target_kind": kind,
        "target_id": target_id,
        "knowledge_state": getattr(row, "knowledge_state", "believes"),
        "acquisition_source": getattr(row, "acquisition_source", None),
        "visibility": visibility,
        "target_visibility": _canonical_knowledge_visibility(target_visibility or row_visibility),
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
            row = db.get(WorldKnowledge, _coerce_optional_uuid(entry.get("knowledge_id")))
        except Exception:
            row = None
        if row is not None and getattr(row, "campaign_id", None) == campaign_id:
            row_vis = getattr(row, "visibility", "dm_only")
            target = entry.get("target") if isinstance(entry.get("target"), dict) else None
            target_vis = (target or {}).get("visibility", row_vis)
            entry["visibility"] = _most_restrictive_visibility(row_vis, target_vis)


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
    from app.world.epistemics import validate_knowledge_state, what_does_subject_know

    started = time.monotonic()
    limit_applied = _clamp_limit(limit)
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    if knowledge_state is not None:
        validate_knowledge_state(knowledge_state)
    try:
        subject_id = _coerce_uuid(subject_entity_id, field_name="subject_entity_id")
    except ValueError as exc:
        return _not_found("query_character_knowledge", campaign, depth=0,
                          limit=limit_applied, detail=str(exc))
    if dm_internal:
        from app.world.epistemics import list_knowledge_for_subject
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
            _knowledge_packet(entry, rank, revealable=None)
            for rank, entry in enumerate(entries)
        ]
        outcome = RetrievalOutcome(
            status=STATUS_OK, packets=packets, total=total_rows,
            visible=len(packets), denied=0, denied_reasons={},
            depth_applied=0, limit_applied=limit_applied, truncated=truncated,
            source_ids=_packet_source_ids(packets),
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
        _knowledge_packet({**entry, "campaign_id": str(campaign.id)}, rank,
                          revealable=True)
        for rank, entry in enumerate(entries)
    ]
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=int(projection.get("total", 0)),
        visible=len(packets), denied=int(projection.get("denied", 0)),
        denied_reasons=dict(projection.get("denied_reasons", {})),
        depth_applied=0, limit_applied=limit_applied, truncated=truncated,
        source_ids=_packet_source_ids(packets),
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
    from app.world.epistemics import what_does_subject_know

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
        _knowledge_packet({**entry, "campaign_id": str(campaign.id)}, rank,
                          revealable=True)
        for rank, entry in enumerate(entries)
    ]
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=int(per_viewer[0].get("total", 0)),
        visible=len(packets), denied=max(0, denied_total),
        denied_reasons={"target_not_visible": max(0, denied_total)} if denied_total else {},
        depth_applied=0, limit_applied=limit_applied, truncated=truncated,
        source_ids=_packet_source_ids(packets),
        latency_ms=(time.monotonic() - started) * 1000,
    )
    _log_query("query_character_knowledge", campaign, depth=0,
               limit=limit_applied, outcome=outcome, dm_internal=False)
    return outcome


def query_who_knows(
    db: Session,
    campaign_id: Any,
    target_kind: str,
    target_id: Any,
    viewer_user_id: Any = None,
    *,
    knowledge_state: str | None = None,
    limit: Any = RETRIEVAL_DEFAULT_LIMIT,
    dm_internal: bool = False,
) -> RetrievalOutcome:
    """Which subjects may the viewer see as holding one truth target?

    Player-facing retrieval wraps the #211 ``who_knows_target`` projection.
    DM-internal retrieval bypasses human disclosure filtering while
    remaining campaign-scoped, returning every knower row with its real
    visibility metadata preserved.
    """
    from app.world.epistemics import (
        validate_knowledge_state,
        validate_knowledge_target_kind,
        who_knows_target,
    )

    started = time.monotonic()
    limit_applied = _clamp_limit(limit)
    campaign = _resolve_campaign(db, campaign_id)
    viewers = _resolve_viewers(viewer_user_id)
    kind = validate_knowledge_target_kind(target_kind)
    if knowledge_state is not None:
        validate_knowledge_state(knowledge_state)
    try:
        tid = _coerce_uuid(target_id, field_name="target_id")
    except ValueError as exc:
        return _not_found("query_character_knowledge", campaign, depth=0,
                          limit=limit_applied, detail=str(exc))
    if dm_internal:
        from app.world.epistemics import list_knowledge_for_target
        from models.world import WorldEntity, WorldFact, WorldRelation

        target_record = None
        try:
            if kind == "fact":
                target_record = db.get(WorldFact, tid)
            elif kind == "relation":
                target_record = db.get(WorldRelation, tid)
            else:
                target_record = db.get(WorldEntity, tid)
        except Exception:
            target_record = None
        if target_record is None or getattr(target_record, "campaign_id", None) != campaign.id:
            return _not_found("query_character_knowledge", campaign, depth=0,
                              limit=limit_applied,
                              detail=f"Target {kind} {tid} not found")
        rows = list_knowledge_for_target(
            db, campaign.id, kind, tid,
            knowledge_state=knowledge_state, limit=limit_applied + 1)
        total_rows = len(rows)
        truncated = total_rows > limit_applied
        rows = rows[:limit_applied]
        packets = [
            _knowledge_packet(_knowledge_entry_from_row(db, row, campaign.id), rank,
                              revealable=None)
            for rank, row in enumerate(rows)
        ]
        outcome = RetrievalOutcome(
            status=STATUS_OK, packets=packets, total=total_rows,
            visible=len(packets), denied=0, denied_reasons={},
            depth_applied=0, limit_applied=limit_applied,
            truncated=truncated,
            source_ids=_packet_source_ids(packets),
            latency_ms=(time.monotonic() - started) * 1000,
        )
        _log_query("query_character_knowledge", campaign, depth=0,
                   limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
        return outcome
    if len(viewers) >= 1:
        effective_viewer = viewers[0]
    else:
        outcome = RetrievalOutcome(depth_applied=0, limit_applied=limit_applied)
        outcome.denied = 1
        outcome.denied_reasons["viewer_required"] = 1
        outcome.latency_ms = (time.monotonic() - started) * 1000
        return outcome
    projection = who_knows_target(
        db, campaign, kind, tid, effective_viewer,
        knowledge_state=knowledge_state, limit=limit_applied + 1)
    knowers = list(projection.get("knowers", []))[:limit_applied]
    _enrich_entries_with_row_visibility(db, campaign.id, knowers)
    from models.world import WorldEntity as _WhoKnowsEntity
    from models.world import WorldFact as _WhoKnowsFact
    from models.world import WorldRelation as _WhoKnowsRelation

    _target_record = None
    try:
        if kind == "fact":
            _target_record = db.get(_WhoKnowsFact, tid)
        elif kind == "relation":
            _target_record = db.get(_WhoKnowsRelation, tid)
        else:
            _target_record = db.get(_WhoKnowsEntity, tid)
    except Exception:
        _target_record = None
    _target_vis = (
        getattr(_target_record, "visibility", "dm_only")
        if _target_record is not None
        and getattr(_target_record, "campaign_id", None) == campaign.id
        else "dm_only"
    )
    for _knower in knowers:
        _knower["visibility"] = _most_restrictive_visibility(
            _knower.get("visibility", "dm_only"), _target_vis)
    packets = [
        _knowledge_packet(
            {"knowledge_id": k["knowledge_id"],
             "subject_kind": k.get("subject_kind"),
             "subject_entity_id": k.get("subject_entity_id"),
             "target_kind": kind, "target_id": str(tid),
             "knowledge_state": k.get("knowledge_state"),
             "acquisition_source": k.get("acquisition_source"),
             "visibility": k.get("visibility", "dm_only"),
             "campaign_id": str(campaign.id)}, rank,
            revealable=True)
        for rank, k in enumerate(knowers)
    ]
    outcome = RetrievalOutcome(
        status=STATUS_OK, packets=packets, total=int(projection.get("total", 0)),
        visible=len(packets), denied=int(projection.get("denied", 0)),
        denied_reasons=dict(projection.get("denied_reasons", {})),
        depth_applied=0, limit_applied=limit_applied,
        truncated=len(projection.get("knowers", [])) > limit_applied,
        source_ids=_packet_source_ids(packets),
        latency_ms=(time.monotonic() - started) * 1000,
    )
    _log_query("query_character_knowledge", campaign, depth=0,
               limit=limit_applied, outcome=outcome, dm_internal=dm_internal)
    return outcome


# ── Optional semantic rerank hook (#380/#381, never blocking) ────────────────

def build_rerank_candidates(packets: list[EvidencePacket],
                            *, max_candidates: int = RETRIEVAL_MAX_LIMIT
                            ) -> list[dict[str, str]]:
    """Bounded candidate descriptors for a decision-model reranker.

    Only stable candidate IDs plus a short human-readable description leave
    this boundary — never hidden content beyond what is already authorized.
    """
    candidates: list[dict[str, str]] = []
    for packet in packets[:max(1, min(int(max_candidates), RETRIEVAL_MAX_LIMIT))]:
        summary = packet.source_id
        content = packet.content if isinstance(packet.content, dict) else {}
        for key in ("name", "content", "relation_type", "location_name"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                summary = value.strip()[:160]
                break
        candidates.append({
            "id": f"{packet.source_type}:{packet.source_id}",
            "description": f"{packet.source_type} {summary} "
                           f"(epistemic={packet.epistemic_state or 'n/a'} "
                           f"visibility={packet.visibility} "
                           f"version={packet.source_version})",
        })
    return candidates


def apply_rerank(
    packets: list[EvidencePacket],
    *,
    order: list[str] | None = None,
    defer: bool = False,
    reranker: Callable[[list[dict[str, str]]], dict[str, Any] | list[str] | str | None] | None = None,
    reranker_model: str | None = None,
    reranker_policy: str | None = None,
) -> RerankedOutcome:
    """Apply an optional semantic rerank over an authorized packet set.

    Either ``order`` (explicit candidate-ID list, e.g. from a test or a
    #380/#381 decision answer) or ``reranker`` (a callable receiving
    ``build_rerank_candidates`` output) drives presentation order. The
    callable may return an ID list, ``{"order": [...]}``, ``{"defer": True}``,
    or the ``DEFER``/``NO_MATCH`` escape IDs for the no-relevant-evidence
    outcome. Unknown IDs and reranker exceptions fall back to deterministic
    retrieval order so authoritative evidence is never lost; the original
    retrieval rank, score, source identity, and provenance on every packet
    are preserved either way.
    """
    candidates = build_rerank_candidates(packets)
    candidate_ids = [c["id"] for c in candidates]
    allowed = set(candidate_ids)

    resolved_order: list[str] | None = list(order) if order is not None else None
    resolved_defer = bool(defer)
    model = reranker_model
    policy = reranker_policy
    fallback = False
    error: str | None = None

    if reranker is not None and resolved_order is None and not resolved_defer:
        try:
            raw = reranker(candidates)
            if isinstance(raw, str):
                if raw.strip().upper() in {RERANK_DEFER_ID, RERANK_NO_MATCH_ID}:
                    resolved_defer = True
                else:
                    resolved_order = [raw]
            elif isinstance(raw, list):
                resolved_order = [str(item) for item in raw]
            elif isinstance(raw, dict):
                if raw.get("defer") or raw.get("no_match"):
                    resolved_defer = True
                else:
                    inner = raw.get("order", [])
                    resolved_order = [str(item) for item in (inner or [])]
                model = model or (str(raw.get("model")) if raw.get("model") else None)
                policy = policy or (str(raw.get("policy")) if raw.get("policy") else None)
            elif raw is None:
                resolved_order = None
            else:
                raise ValueError(f"reranker returned {type(raw).__name__!r}")
        except Exception as exc:
            fallback = True
            error = str(exc)[:300]
            resolved_order = None
            resolved_defer = False

    by_id = {f"{p.source_type}:{p.source_id}": p for p in packets}

    if resolved_defer:
        for packet in packets:
            packet.presentation_rank = None
        structured_log(
            logger, logging.INFO, "world_retrieval_reranked",
            reranked=False, fallback=False, outcome=STATUS_DEFER,
            candidate_count=len(packets), reordered_ids=[],
            reranker_model=model, reranker_policy=policy,
        )
        return RerankedOutcome(
            status=STATUS_DEFER, packets=list(packets), presented_ids=[],
            reordered_ids=[], reranked=False, fallback=False,
            reranker_model=model, reranker_policy=policy)

    if resolved_order is not None:
        unknown = [cid for cid in resolved_order if cid not in allowed]
        if unknown:
            # A decision model must never invent a source ID: reject the
            # whole order and keep deterministic evidence handling.
            structured_log(
                logger, logging.WARNING, "world_retrieval_rerank_rejected",
                reason="unknown_candidate_ids", unknown_ids=unknown[:8],
                candidate_count=len(packets),
            )
            fallback = True
            error = (error + "; " if error else "") + f"unknown candidate IDs: {unknown[:4]}"
            resolved_order = None

    if resolved_order is None:
        for packet in packets:
            packet.presentation_rank = None
        structured_log(
            logger, logging.INFO, "world_retrieval_reranked",
            reranked=False, fallback=fallback, outcome=STATUS_OK,
            candidate_count=len(packets), reordered_ids=[],
            reranker_model=model, reranker_policy=policy, error=error,
        )
        return RerankedOutcome(
            status=STATUS_OK, packets=list(packets),
            presented_ids=list(candidate_ids),
            reordered_ids=[], reranked=False, fallback=fallback,
            reranker_model=model, reranker_policy=policy, error=error)

    seen: set[str] = set()
    ordered: list[EvidencePacket] = []
    for cid in resolved_order:
        if cid in seen:
            continue
        seen.add(cid)
        ordered.append(by_id[cid])
    # Authorized candidates the reranker omitted stay available in retrieval
    # order after the ranked ones — ranking changes presentation, never
    # access to authoritative evidence.
    for packet in packets:
        key = f"{packet.source_type}:{packet.source_id}"
        if key not in seen:
            ordered.append(packet)
    for rank, packet in enumerate(ordered):
        packet.presentation_rank = rank
    reordered = [f"{p.source_type}:{p.source_id}" for p in ordered]
    structured_log(
        logger, logging.INFO, "world_retrieval_reranked",
        reranked=True, fallback=False, outcome=STATUS_OK,
        candidate_count=len(packets), reordered_ids=reordered,
        reranker_model=model, reranker_policy=policy,
    )
    return RerankedOutcome(
        status=STATUS_OK, packets=ordered, presented_ids=list(reordered),
        reordered_ids=list(reordered), reranked=True, fallback=False,
        reranker_model=model, reranker_policy=policy)


def decision_service_reranker(
    service: Any,
    *,
    question_id: str = "world_evidence_rank",
    instructions: str = "Select the most relevant evidence candidate.",
    model: str | None = None,
    policy: str | None = None,
) -> Callable[[list[dict[str, str]]], dict[str, Any]]:
    """Adapt a #380 ``DecisionService`` into an ``apply_rerank`` callable.

    Issues one bounded choice question over the supplied candidate IDs plus
    the standard DEFER escape; the selected ID must be a supplied candidate
    (DecisionService rejects invented IDs) or DEFER for no-match. Provider
    failures propagate to ``apply_rerank``'s fallback path.
    """
    def _rerank(candidates: list[dict[str, str]]) -> dict[str, Any]:
        from app.decisions.contracts import (
            ChoiceQuestion,
            DecisionCandidate,
            DecisionRequest,
        )

        request = DecisionRequest(
            questions=(ChoiceQuestion(
                question_id=question_id,
                instructions=instructions,
                candidates=tuple(
                    [DecisionCandidate(id=c["id"], description=c.get("description"))
                     for c in candidates]
                    + [DecisionCandidate(id=RERANK_DEFER_ID,
                                         description="No candidate is relevant; defer.")]
                ),
            ),),
            state={"rerank_candidate_count": len(candidates)},
            **({"model": model} if model else {}),
        )
        response = service.decide(request)
        result = response.results[question_id]
        selected = getattr(result, "selected_id", None)
        if selected == RERANK_DEFER_ID:
            return {"defer": True, "model": getattr(response, "model", model),
                    "policy": policy}
        # Choice selects the single most relevant candidate; keep every other
        # authorized candidate available behind it in retrieval order.
        rest = [c["id"] for c in candidates if c["id"] != selected]
        return {"order": [selected, *rest],
                "model": getattr(response, "model", model), "policy": policy}

    return _rerank


# ── #203 evidence-tool handlers ──────────────────────────────────────────────

def _audience_viewers(audience: Any) -> list[str]:
    try:
        return list(getattr(audience, "user_ids", None) or [])
    except Exception:
        return []


def _resolve_tool_campaign(db: Session, audience: Any) -> Campaign | None:
    try:
        cid = _coerce_uuid(getattr(audience, "campaign_id", None), field_name="campaign_id")
    except ValueError:
        return None
    try:
        return db.get(Campaign, cid)
    except Exception:
        return None


def _bundle_result(
    request_id: str, tool: str, audience: Any, outcome: RetrievalOutcome,
    *, dm_internal: bool, rerank: RerankedOutcome | None = None,
) -> dict[str, Any]:
    packets = rerank.packets if rerank is not None else outcome.packets
    sources = [p.to_source_ref() for p in packets]
    if outcome.status == STATUS_NOT_FOUND and not packets:
        status = "missing"
    elif outcome.status in {STATUS_DEFER, STATUS_NO_MATCH}:
        status = "unknown"
    elif rerank is not None and rerank.status in {STATUS_DEFER, STATUS_NO_MATCH}:
        status = "unknown"
        packets = []
        sources = []
    else:
        status = "ok" if packets else "unknown"
    if dm_internal:
        visibility = "dm_only" if any(
            p.visibility in {"private", "dm_only"} for p in packets) else "campaign"
    else:
        rank = {"public": 0, "campaign": 1, "dm_only": 2, "private": 3}
        visibility = "campaign"
        for packet in packets:
            if rank.get(packet.visibility, 1) > rank.get(visibility, 1):
                visibility = packet.visibility
    try:
        thread_id = str(getattr(audience, "thread_id", ""))
    except Exception:
        thread_id = ""
    try:
        campaign_id = str(getattr(audience, "campaign_id", ""))
    except Exception:
        campaign_id = ""
    authorization: dict[str, Any] = {"campaign_id": campaign_id, "thread_ids": []}
    if thread_id:
        authorization["thread_ids"] = [thread_id]
    if visibility == "private":
        authorization["user_ids"] = _audience_viewers(audience)
    payload = {
        "retrieval_status": rerank.status if rerank is not None else outcome.status,
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
    if rerank is not None:
        payload["rerank"] = {
            "reranked": rerank.reranked,
            "fallback": rerank.fallback,
            "presented_ids": list(rerank.presented_ids),
            "reordered_ids": list(rerank.reordered_ids),
            "model": rerank.reranker_model,
            "policy": rerank.reranker_policy,
        }
    return {
        "status": status,
        "sources": sources,
        "visibility": visibility,
        "authorization": authorization,
        "payload": payload,
        "result_count": len(packets),
    }


def _require_db(db: Any, request_id: str, tool: str) -> Session | dict[str, Any]:
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


def handle_lookup_world_entity(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_world_entity requires query (entity id)")
    outcome = retrieve_entity(
        session, getattr(audience, "campaign_id", None), query,
        _audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


def handle_traverse_world_relations(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("traverse_world_relations requires query (entity id)")
    outcome = traverse_relations(
        session, getattr(audience, "campaign_id", None), query,
        _audience_viewers(audience), limit=getattr(req, "limit", None),
        dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


def handle_lookup_world_fact(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_world_fact requires query (fact id)")
    outcome = fact_source_evidence(
        session, getattr(audience, "campaign_id", None), query,
        _audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


def handle_query_world_timeline(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip() or None
    outcome = query_timeline(
        session, getattr(audience, "campaign_id", None),
        _audience_viewers(audience), event_types=query,
        limit=getattr(req, "limit", None), dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


def handle_lookup_source_turn(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
    if isinstance(session, dict):
        return session
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("lookup_source_turn requires query (turn id)")
    outcome = lookup_source_turn(
        session, getattr(audience, "campaign_id", None), query,
        _audience_viewers(audience), dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


def handle_query_character_knowledge(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    session = _require_db(db, req.id, req.tool)
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
        _audience_viewers(audience), limit=getattr(req, "limit", None),
        dm_internal=dm_internal)
    return _bundle_result(req.id, req.tool, audience, outcome, dm_internal=dm_internal)


TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "lookup_world_entity": handle_lookup_world_entity,
    "traverse_world_relations": handle_traverse_world_relations,
    "lookup_world_fact": handle_lookup_world_fact,
    "query_world_timeline": handle_query_world_timeline,
    "lookup_source_turn": handle_lookup_source_turn,
    "query_character_knowledge": handle_query_character_knowledge,
}
