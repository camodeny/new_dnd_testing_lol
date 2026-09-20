"""World entities + authoritative current scene — issue #209.

Two tables:
- WorldEntity: durable canonical identity (NPC/location/faction/object/...).
  Stable UUID canonical IDs reusable across turns/events/relations.
  Transient scene state never deletes these rows.
- CampaignCurrentScene: one authoritative transient row per campaign
  (location, fictional time, present actors, environment) with
  revision/source metadata and campaign ownership.

Visibility columns are hooks for later restricted/hidden enforcement;
reads do not assume public.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class WorldEntity(Base):
    __tablename__ = "world_entities"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_world_entities_campaign_idempotency",
        ),
        Index("ix_world_entities_campaign_id", "campaign_id"),
        Index("ix_world_entities_campaign_type", "campaign_id", "entity_type"),
        Index("ix_world_entities_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "entity_type": self.entity_type,
            "name": self.name,
            "summary": self.summary,
            "status": self.status,
            "visibility": self.visibility,
            "details": self.details or {},
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "revision": self.revision,
            "superseded_by_id": str(self.superseded_by_id) if self.superseded_by_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldEntityAlias(Base):
    """Durable, normalized name/reference pointing at stable canonical identity."""

    __tablename__ = "world_entity_aliases"
    __table_args__ = (
        UniqueConstraint("campaign_id", "normalized_alias", name="uq_world_entity_alias_campaign_alias"),
        Index("ix_world_entity_alias_entity", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(160), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NPCState(Base):
    """Optional progressive state for a canonical NPC identity.

    The canonical name/status remains on :class:`WorldEntity`.  Incidental
    NPCs need no row here; enrichment creates one without replacing identity.
    Knowledge and relationships deliberately remain in their shared world
    tables and are joined by services when a bounded projection requests them.
    """

    __tablename__ = "npc_states"
    __table_args__ = (
        Index("ix_npc_states_campaign_importance", "campaign_id", "importance"),
        Index("ix_npc_states_campaign_location", "campaign_id", "location_entity_id"),
        CheckConstraint("importance IN ('incidental','supporting','major')", name="ck_npc_states_importance"),
        CheckConstraint("depth >= 0", name="ck_npc_states_depth_nonnegative"),
        CheckConstraint("state_revision >= 1", name="ck_npc_states_revision_positive"),
        CheckConstraint("campaign_revision >= 1", name="ck_npc_states_campaign_revision_positive"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="CASCADE"), primary_key=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str | None] = mapped_column(String(160), nullable=True)
    goals: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))
    disposition: Mapped[dict | None] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    resources: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))
    current_activity: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True,
    )
    location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    importance: Mapped[str] = mapped_column(String(16), nullable=False, default="incidental", server_default="incidental")
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    field_visibility: Mapped[dict | None] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    state_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    campaign_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=False)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "entity_id": str(self.entity_id), "campaign_id": str(self.campaign_id),
            "role": self.role, "goals": self.goals or [],
            "disposition": self.disposition or {}, "resources": self.resources or [],
            "current_activity": self.current_activity,
            "location_entity_id": str(self.location_entity_id) if self.location_entity_id else None,
            "location_name": self.location_name, "importance": self.importance,
            "depth": self.depth, "field_visibility": self.field_visibility or {},
            "state_revision": self.state_revision, "campaign_revision": self.campaign_revision,
            "provenance": self.provenance or {},
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "operation_id": self.operation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignCurrentScene(Base):
    """Single authoritative transient scene row per campaign.

    Location may reference a durable WorldEntity (location_entity_id) plus a
    transient display name; fictional time / present actors / environment are
    transient and change without deleting entity history. revision/source
    columns tie the row to campaign revision/event ordering.
    """

    __tablename__ = "campaign_current_scenes"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    location_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fictional_time: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fictional_time_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    present_actors: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))
    environment: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "campaign_id": str(self.campaign_id),
            "location_entity_id": str(self.location_entity_id) if self.location_entity_id else None,
            "location_name": self.location_name,
            "fictional_time": self.fictional_time,
            "fictional_time_details": self.fictional_time_details or {},
            "present_actors": self.present_actors or [],
            "environment": self.environment or {},
            "visibility": self.visibility,
            "revision": self.revision,
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "operation_id": self.operation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ── Issue #210: durable relations + epistemic facts ──────────────────────────

# Explicit truth/epistemic vocabulary. A player/NPC claim is stored as
# ``claimed``/``suspected``/etc. and never becomes ``confirmed`` objective
# truth unless a later supersession says so.
EPISTEMIC_STATES = frozenset({
    "confirmed", "false", "believed", "suspected", "claimed", "unknown", "retconned",
})

# Effective lifecycle. Only ``active`` rows are current truth; superseded /
# retracted rows are preserved history, never destructively overwritten.
RECORD_STATUSES = frozenset({"active", "superseded", "retracted"})


class WorldRelation(Base):
    """Durable graph-shaped world knowledge: subject —relation→ object.

    Only ``status == "active"`` rows are current truth (indexed for direct
    reads, no history replay). Changes create a new version row linked via
    ``supersedes_id``/``superseded_by_id``; the prior row is flipped to
    ``superseded`` in the same transaction so history is preserved.
    """

    __tablename__ = "world_relations"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_world_relations_campaign_idempotency",
        ),
        Index("ix_world_relations_campaign_status", "campaign_id", "status"),
        Index("ix_world_relations_campaign_subject", "campaign_id", "subject_entity_id"),
        Index("ix_world_relations_campaign_object", "campaign_id", "object_entity_id"),
        Index("ix_world_relations_campaign_type", "campaign_id", "relation_type"),
        Index("ix_world_relations_source_turn", "campaign_id", "source_turn_id"),
        Index("ix_world_relations_source_event", "campaign_id", "source_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id"), nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id"), nullable=True,
    )
    object_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    epistemic_state: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed", server_default="claimed")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("world_relations.id", ondelete="SET NULL"), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("world_relations.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="dm_only", server_default="dm_only")
    grants: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "subject_entity_id": str(self.subject_entity_id),
            "relation_type": self.relation_type,
            "object_entity_id": str(self.object_entity_id) if self.object_entity_id else None,
            "object_label": self.object_label,
            "epistemic_state": self.epistemic_state,
            "status": self.status,
            "version": self.version,
            "supersedes_id": str(self.supersedes_id) if self.supersedes_id else None,
            "superseded_by_id": str(self.superseded_by_id) if self.superseded_by_id else None,
            "visibility": self.visibility,
            "grants": self.grants or {},
            "provenance": self.provenance or {},
            "details": self.details or {},
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldFact(Base):
    """Durable propositional world knowledge ("the bridge collapsed").

    Same versioning/lifecycle contract as WorldRelation. Referenced canonical
    entities live in the ``world_fact_entity_refs`` join table for indexed
    lookup; ``entity_refs`` JSONB mirrors them for display.
    """

    __tablename__ = "world_facts"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_world_facts_campaign_idempotency",
        ),
        Index("ix_world_facts_campaign_status", "campaign_id", "status"),
        Index("ix_world_facts_campaign_epistemic", "campaign_id", "epistemic_state"),
        Index("ix_world_facts_source_turn", "campaign_id", "source_turn_id"),
        Index("ix_world_facts_source_event", "campaign_id", "source_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    epistemic_state: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed", server_default="claimed")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("world_facts.id", ondelete="SET NULL"), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("world_facts.id", ondelete="SET NULL"), nullable=True)
    entity_refs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="dm_only", server_default="dm_only")
    grants: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "content": self.content,
            "entity_refs": list(self.entity_refs or []),
            "epistemic_state": self.epistemic_state,
            "status": self.status,
            "version": self.version,
            "supersedes_id": str(self.supersedes_id) if self.supersedes_id else None,
            "superseded_by_id": str(self.superseded_by_id) if self.superseded_by_id else None,
            "visibility": self.visibility,
            "grants": self.grants or {},
            "provenance": self.provenance or {},
            "details": self.details or {},
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ── Issue #211: epistemic knowledge + visibility grants ─────────────────────

# Fictional epistemic stance of one knower toward one truth record. This never
# changes objective truth (WorldFact.epistemic_state / WorldRelation state);
# it only records what a character/NPC/party fictionally holds.
KNOWLEDGE_STATES = frozenset({
    "knows", "believes", "suspects", "claims", "does_not_know",
})

# Fictional knower kinds. Subjects are always canonical WorldEntity rows
# (PCs included via entity_type="character"); human users never appear here.
# Human access is governed separately by visibility + WorldVisibilityGrant.
KNOWER_KINDS = frozenset({"character", "npc", "party", "group"})

# Knowledge target kinds: objective truth records a knower can hold a stance
# toward. Exactly one target FK is set per row.
KNOWLEDGE_TARGET_KINDS = frozenset({"fact", "relation", "entity"})

# Visibility-grant target kinds: any record whose human disclosure can be
# scoped to an arbitrary authorized subset (includes knowledge rows, whose
# disclosure is independent of the underlying truth record's disclosure).
GRANT_TARGET_KINDS = frozenset({"fact", "relation", "entity", "knowledge"})

# Denied-access reason codes (observability without leaking hidden content).
ACCESS_DENIED_REASONS = frozenset({
    "not_campaign_member",
    "dm_only_requires_authority",
    "private_requires_grant",
    "record_not_found",
    "ambiguous_visibility",
    "knowledge_not_visible",
    "target_not_visible",
    "subject_not_visible",
})


class WorldKnowledge(Base):
    """Per-knower epistemic record — issue #211.

    Links one fictional subject (character/NPC/party/group as a canonical
    WorldEntity) to one truth record (fact/relation/entity) with a fictional
    stance (knows/believes/suspects/claims/does_not_know).

    - Never mutates objective truth: writers only touch this table (+ events).
    - Human disclosure of THIS row is governed by its own ``visibility`` plus
      WorldVisibilityGrant rows; disclosure of the underlying truth record is
      checked independently (never inferred from this row or vice versa).
    - One current row per (subject, target): re-assertion updates the row in
      place inside the caller's transaction; history lives in domain events.
    - Fail-closed visibility: default ``dm_only``.
    """

    __tablename__ = "world_knowledge"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_world_knowledge_campaign_idempotency",
        ),
        Index("ix_world_knowledge_campaign_subject", "campaign_id", "subject_entity_id"),
        Index("ix_world_knowledge_campaign_fact", "campaign_id", "target_fact_id"),
        Index("ix_world_knowledge_campaign_relation", "campaign_id", "target_relation_id"),
        Index("ix_world_knowledge_campaign_tentity", "campaign_id", "target_entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="CASCADE"), nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_facts.id", ondelete="CASCADE"), nullable=True,
    )
    target_relation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_relations.id", ondelete="CASCADE"), nullable=True,
    )
    target_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="CASCADE"), nullable=True,
    )
    knowledge_state: Mapped[str] = mapped_column(String(16), nullable=False, default="believes", server_default="believes")
    acquisition_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="dm_only", server_default="dm_only")
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "subject_kind": self.subject_kind,
            "subject_entity_id": str(self.subject_entity_id),
            "target_kind": self.target_kind,
            "target_fact_id": str(self.target_fact_id) if self.target_fact_id else None,
            "target_relation_id": str(self.target_relation_id) if self.target_relation_id else None,
            "target_entity_id": str(self.target_entity_id) if self.target_entity_id else None,
            "knowledge_state": self.knowledge_state,
            "acquisition_source": self.acquisition_source,
            "visibility": self.visibility,
            "provenance": self.provenance or {},
            "details": self.details or {},
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldVisibilityGrant(Base):
    """Arbitrary-subset human disclosure grant — issue #211.

    ``visibility="private"`` records disclose to exactly the grantee set with
    an active (unrevoked) row here — never to the campaign at large and never
    to the owner implicitly. Revocation sets ``revoked_at`` (soft): future
    reads deny while the row + its history stay durable provenance.
    """

    __tablename__ = "world_visibility_grants"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_world_visibility_grants_campaign_idempotency",
        ),
        Index("ix_world_visibility_grants_target", "campaign_id", "target_kind", "target_id"),
        Index("ix_world_visibility_grants_grantee", "campaign_id", "grantee_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False,
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "target_kind": self.target_kind,
            "target_id": str(self.target_id),
            "grantee_user_id": str(self.grantee_user_id),
            "granted_by": str(self.granted_by) if self.granted_by else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldFactEntityRef(Base):
    """Indexed join: which canonical entities a fact references.

    Authoritative for lookup-by-entity; kept in sync with
    ``WorldFact.entity_refs`` by the service layer.
    """

    __tablename__ = "world_fact_entity_refs"
    __table_args__ = (
        Index("ix_world_fact_entity_refs_campaign_entity", "campaign_id", "entity_id"),
        Index("ix_world_fact_entity_refs_fact", "fact_id"),
    )

    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_facts.id", ondelete="CASCADE"), primary_key=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id"), nullable=False, primary_key=True,
    )


# ── Issue #213: rebuildable semantic index over authoritative sources ─────────

# Source types eligible for async semantic indexing. Each row points at one
# canonical record; embeddings are derived and never truth.
SEMANTIC_SOURCE_TYPES = frozenset({
    "world_entity",
    "world_relation",
    "world_fact",
    "domain_event",
    "source_turn",
    "scene",
})

# Lifecycle of one embedding row. Only ``active`` rows participate in search;
# ``stale`` rows await rebuild, ``superseded`` rows point at replaced sources.
SEMANTIC_INDEX_STATUSES = frozenset({"active", "stale", "superseded", "failed"})


class WorldEmbedding(Base):
    """Rebuildable pgvector-backed semantic index — issue #213.

    One row per (campaign, source record, embedding model/version). The
    ``embedding`` column holds the vector natively (``vector(1536)``) on
    Postgres hosts with the pgvector extension and JSON text otherwise (same
    branch pattern as ``rules_embeddings``); ``embedding_text`` always holds
    the portable JSON snapshot used by the graceful-degradation search path.
    Similarity scores are derived ranking metadata — the authoritative record
    (``source_type``/``source_id``/``source_version``) remains the evidence.
    """

    __tablename__ = "world_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "source_type", "source_id",
            "embedding_model", "embedding_version",
            name="uq_world_embeddings_source_model",
        ),
        Index("ix_world_embeddings_campaign_status", "campaign_id", "status"),
        Index("ix_world_embeddings_campaign_source", "campaign_id", "source_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1", server_default="1")
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "source_type": self.source_type,
            "source_id": str(self.source_id),
            "source_version": self.source_version,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
