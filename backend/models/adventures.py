"""Adventure lifecycle + derived summary/recap records — issue #263.

Single canonical path (no legacy shims):

- Adventure: DM-declared story-arc record scoped to a campaign. Completion is
  an authoritative fictional mutation (committed via
  ``commit_campaign_mutation`` as ``adventure.completed``); the campaign stays
  continuable independently.
- AdventureSummary: ONE derived row per adventure (``is_derived`` is always
  true). It identifies its exact source adventure/event range + version and
  carries both a durable historical summary (DM/context retrieval) and a
  separate player-facing recap projection. Derived prose NEVER overrides
  event/fact/world authority — readers must prefer events/facts on conflict.

Visibility: recap text is built only from viewer-appropriate source events
(public, or actor-visible). Private/dm_only/dm_private sources are omitted;
a leak validator rejects recap text containing private-only tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

ADVENTURE_STATUSES = frozenset({"open", "completed"})

# DM-declared completion outcomes — victory is only one valid completion.
ADVENTURE_OUTCOMES = frozenset({
    "victory",
    "defeat",
    "retreat",
    "capture",
    "tpk",
    "villain_victory",
    "draw",
    "pyrrhic_victory",
})

# Summary lifecycle. ``failed`` keeps the prior artifact (if any) but flags it
# stale rather than presenting it as current; ``stale`` requires rebuild.
SUMMARY_STATUSES = frozenset({"pending", "current", "stale", "failed"})

# Event visibilities treated as hidden from the player-facing recap.
HIDDEN_VISIBILITIES = frozenset({"private", "dm_only", "dm_private", "gm_only"})


class Adventure(Base):
    __tablename__ = "adventures"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "idempotency_key",
            name="uq_adventures_campaign_idempotency",
        ),
        Index("ix_adventures_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="Untitled Adventure")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    end_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "title": self.title,
            "status": self.status,
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "end_revision": self.end_revision,
            "operation_id": self.operation_id,
            "extra": self.extra or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class AdventureSummary(Base):
    """Derived summary/recap artifact — explicitly NOT authoritative.

    ``is_derived`` is always True. Consumers must treat domain events, facts,
    and world state as authoritative over this prose on any conflict.
    """

    __tablename__ = "adventure_summaries"
    __table_args__ = (
        UniqueConstraint("adventure_id", name="uq_adventure_summaries_adventure"),
        Index("ix_adventure_summaries_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adventure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("adventures.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source_event_from: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    source_event_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_derived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    historical_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    recap_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stale_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rebuild_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    validation_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    leak_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self, *, include_text: bool = True):
        d = {
            "id": str(self.id),
            "adventure_id": str(self.adventure_id),
            "campaign_id": str(self.campaign_id),
            "version": self.version,
            "source_event_from": self.source_event_from,
            "source_event_to": self.source_event_to,
            "source_revision": self.source_revision,
            "is_derived": True,
            "authority": "derived: events/facts/world outrank this prose on conflict",
            "status": self.status,
            "attempts": self.attempts,
            "stale_count": self.stale_count,
            "rebuild_count": self.rebuild_count,
            "validation_failures": self.validation_failures,
            "leak_failures": self.leak_failures,
            "views": self.views,
            "provider": self.provider,
            "model": self.model,
            "cost_cents": self.cost_cents,
            "error": self.error,
            "metadata": self.summary_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_text:
            d["historical_text"] = self.historical_text
            d["recap_text"] = self.recap_text
        return d
