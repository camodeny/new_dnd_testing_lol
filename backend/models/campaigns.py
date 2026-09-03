"""Campaigns and membership domain models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('lobby', 'starting', 'active', 'archived')",
            name="ck_campaigns_lifecycle_status",
        ),
        CheckConstraint(
            "required_players BETWEEN 1 AND 6",
            name="ck_campaigns_required_players_launch_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    random_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="lobby", server_default="lobby")
    theme: Mapped[str | None] = mapped_column(String(128), nullable=True)
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", server_default="medium")
    required_players: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    content_boundaries: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    loot_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="frequent_gamble", server_default="frequent_gamble")
    # Monotonic fictional revision — incremented exactly once per authoritative fictional mutation.
    # See campaign_events.py commit_campaign_mutation().
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "name": self.name,
            "description": self.description,
            "random_seed": self.random_seed,
            "status": self.status,
            "theme": self.theme,
            "brief": self.brief,
            "difficulty": self.difficulty,
            "required_players": self.required_players,
            "content_boundaries": self.content_boundaries or {},
            "loot_mode": self.loot_mode,
            "revision": self.revision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignDomainEvent(Base):
    """Immutable domain-event persistence — issue #188.

    Every authoritative fictional mutation commits one row with a campaign-scoped
    monotonic sequence (== resulting Campaign.revision). Current-state tables remain
    authoritative for reads; events provide provenance/history, not full sourcing.

    Visibility/provenance fields are hooks for later enforcement; reads must
    eventually respect them but this issue only ensures the columns exist.
    """

    __tablename__ = "campaign_domain_events"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence", name="uq_campaign_domain_events_campaign_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    targets: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public", server_default="public")
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "sequence": self.sequence,
            "event_type": self.event_type,
            "operation_id": self.operation_id,
            "trace_id": self.trace_id,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "targets": self.targets,
            "payload": self.payload,
            "visibility": self.visibility,
            "provenance": self.provenance,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="player")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CampaignInvite(Base):
    __tablename__ = "campaign_invites"
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
