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


class CampaignCharacterLore(Base):
    """Private character setup lore — issue #244.

    One row per (campaign, character): the controlling player's private
    backstory/secrets shared with the DM (the AI runtime) during setup.
    Visibility follows #211 ``private`` semantics, enforced centrally in
    ``app.campaigns.party_lore.assert_lore_readable`` (owning player only,
    plus DM-internal seed consumption via ``get_seed_lore_bundle``); the
    campaign owner gets nothing implicit — owner reads of another player's
    row fail closed (404, no existence leak). Public party-composition
    projections must never include content.

    A dedicated table (rather than WorldFact rows) keeps pre-start setup
    secrets isolated from world-truth/retrieval paths until #245 explicitly
    consumes them as restricted seed input; authorization matches the shared
    private rule exactly.
    """

    __tablename__ = "campaign_character_lore"
    __table_args__ = (
        UniqueConstraint("campaign_id", "character_id", name="uq_character_lore_campaign_character"),
        Index("ix_character_lore_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # #211 visibility tier for this row — always ``private``. Stored
    # explicitly so the authorization rule keys off data, not table identity.
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private", server_default="private")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self, *, include_content: bool = False):
        d = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "character_id": str(self.character_id),
            "user_id": str(self.user_id),
            "version": self.version,
            "has_content": bool(self.content),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_content:
            d["content"] = self.content
        return d


class CampaignInvite(Base):
    """Shareable lobby invitation — issue #242.

    One row per invite (a campaign may have many outstanding invites). Each
    row carries its own code/link, optional intended recipient metadata,
    creator, lifecycle status, and optional expiry. Revocation is a status
    flip (row preserved for observability), never a delete.

    Status is ``active`` or ``revoked``; expiry is derived from
    ``expires_at`` (no background sweeper — reads treat a past ``expires_at``
    as expired). ``accepted_count`` counts successful membership creations
    through this invite for observability; membership rows stay idempotent
    per (campaign, user) regardless of retries.
    """

    __tablename__ = "campaign_invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    intended_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    recipient_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_delivery_status: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    last_delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Adventure(Base):
    """Lightweight adventure/arc record — issue #260.

    The campaign is the durable continuity boundary; an adventure is one
    meaningful arc within it. The AI DM declares completion via a validated
    effect (or the adventures API); the campaign stays active and later
    adventures can be created in the same world.

    Outcome is a DM narrative decision, not a checklist: non-victory
    outcomes (retreat, capture, death/TPK, villain victory) are valid
    completions. Campaign archival is a separate concern (own issue).

    ``start_sequence`` / ``end_sequence`` / ``end_revision`` bound the
    authoritative domain-event range of the arc (issue #263). They feed the
    derived AdventureSummary below; events/facts/world stay authoritative.
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
    # Source-range boundary for derived summaries (issue #263): the first
    # domain-event sequence belonging to this arc. Defaults to the post-cursor
    # boundary at open time; explicit values stay inclusive.
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    end_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "end_revision": self.end_revision,
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


class AdventureSummary(Base):
    """Derived summary/recap artifact — explicitly NOT authoritative (issue #263).

    ONE row per adventure. ``is_derived`` is always true: consumers must treat
    domain events, facts, and world state as authoritative over this prose on
    any conflict. Carries both a durable historical summary (owner/DM-only
    retrieval context, may compress hidden sources) and a player-facing recap
    baseline that is always re-projected per viewer at read time.

    Lifecycle: ``pending`` → ``current``; repair/retcon marks ``stale``;
    generation failure goes to ``failed`` with the error recorded (the
    adventure's completed status is untouched). Regeneration bumps version.
    """

    __tablename__ = "adventure_summaries"
    __table_args__ = (
        UniqueConstraint("adventure_id", name="uq_adventure_summaries_adventure"),
        Index("ix_adventure_summaries_campaign", "campaign_id"),
        CheckConstraint(
            "status IN ('pending', 'current', 'stale', 'failed')",
            name="ck_adventure_summaries_status",
        ),
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
