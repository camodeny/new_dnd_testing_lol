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

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
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
