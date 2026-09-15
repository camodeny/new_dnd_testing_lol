"""Campaigns and membership domain models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
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
    """Lobby membership — issue #241.

    One human member = at most one selected player character (owned by that
    member). Readiness is explicit and reversible before start; campaign start
    locks the launch assignment (status != lobby rejects selection/readiness
    changes). Readiness alone never freezes character edits.
    """

    __tablename__ = "campaign_members"
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="player")
    selected_character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, default=None
    )
    is_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CampaignInvite(Base):
    __tablename__ = "campaign_invites"
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Adventure(Base):
    """Lightweight adventure/arc record — issue #260.

    The campaign is the durable continuity boundary; an adventure is one
    meaningful arc within it. The AI DM declares completion via a validated
    effect (or the adventures API); the campaign stays active and later
    adventures can be created in the same world.

    Outcome is a DM narrative decision, not a checklist: non-victory
    outcomes (retreat, capture, death/TPK, villain victory) are valid
    completions. Campaign archival is a separate concern (own issue).
    """

    __tablename__ = "adventures"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_adventures_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('victory', 'failure', 'retreat', 'capture', 'death', 'tpk', 'villain_victory')",
            name="ck_adventures_outcome",
        ),
        CheckConstraint(
            "closing_status IN ('pending', 'succeeded', 'failed')",
            name="ck_adventures_closing_status",
        ),
        # Idempotency: one completion operation closes at most one adventure
        # record per campaign; retries hit the same row.
        UniqueConstraint("campaign_id", "operation_id", name="uq_adventures_campaign_operation"),
        # Lifecycle invariant: at most one active adventure per campaign,
        # enforced in PostgreSQL so concurrent starts cannot both insert.
        Index(
            "uq_adventures_one_active_per_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Player-visible summary; hidden world consequences stay scoped separately.
    public_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    adventure_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # Authoritative turn/event provenance for the completion decision.
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_turns.id", ondelete="SET NULL"), nullable=True
    )
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True
    )
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Downstream closing work (recap/rewards) — best-effort, never invalidates
    # an already committed narrative completion.
    closing_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    closing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    closing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "title": self.title,
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "public_summary": self.public_summary,
            "metadata": self.adventure_metadata,
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_event_id": str(self.source_event_id) if self.source_event_id else None,
            "operation_id": self.operation_id,
            "closing_status": self.closing_status,
            "closing_attempts": self.closing_attempts,
            "closing_error": self.closing_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_public_dict(self):
        """Player-safe projection — issue #260.

        Members see the arc identity, its outcome, and the player-visible
        summary. The DM's completion reason, arbitrary metadata, turn/event
        provenance ids, operation ids, and closing-work bookkeeping stay
        owner-visible only.
        """
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "title": self.title,
            "status": self.status,
            "outcome": self.outcome,
            "public_summary": self.public_summary,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class CampaignPcLifecycle(Base):
    """Replacement-character lifecycle — issue #266.

    One row per (campaign, player character). Launch PCs backfill to
    ``active`` on first touch; death/retirement flips the row to a terminal
    ``dead``/``retired`` state that is never deleted — the dead PC's sheet,
    inventory, and world relationships stay intact as historical canon.

    A replacement PC gets its own ``active`` row linked to the fallen PC via
    ``replacement_of_character_id`` (and the reverse ``replaced_by`` pointer
    on the dead row) plus an ``introduction_status`` so the AI DM introduces
    the newcomer through normal forward-DM play (domain events surface in
    RECENT_HISTORY) instead of silently teleporting them into the party.
    """

    __tablename__ = "campaign_pc_lifecycles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'dead', 'retired')",
            name="ck_pc_lifecycles_status",
        ),
        CheckConstraint(
            "introduction_status IN ('na', 'pending_introduction', 'introduced')",
            name="ck_pc_lifecycles_introduction_status",
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    character_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    died_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_tpk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    replaced_by_character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, default=None
    )
    replacement_of_character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, default=None
    )
    introduction_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="na", server_default="na"
    )
    introduced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "campaign_id": str(self.campaign_id),
            "character_id": str(self.character_id),
            "user_id": str(self.user_id),
            "status": self.status,
            "cause": self.cause,
            "died_at": self.died_at.isoformat() if self.died_at else None,
            "is_tpk": bool(self.is_tpk),
            "replaced_by_character_id": str(self.replaced_by_character_id) if self.replaced_by_character_id else None,
            "replacement_of_character_id": str(self.replacement_of_character_id) if self.replacement_of_character_id else None,
            "introduction_status": self.introduction_status,
            "introduced_at": self.introduced_at.isoformat() if self.introduced_at else None,
        }
