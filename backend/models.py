"""App tables that live in Supabase Postgres.

- Supabase Auth owns `auth.users` (managed by GoTrue). Do NOT duplicate it.
- We keep a thin `public.profiles` mirror synced on login (id = auth.users.id UUID).
  This is where app-specific fields live and where foreign keys should point.

If you prefer to use `auth.users` directly without a mirror, point FKs there — but
`profiles` is the Supabase-recommended pattern (lets you add username, etc.).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Profile(Base):
    __tablename__ = "profiles"

    # Matches auth.users.id (UUID v4 from Supabase)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # Supabase auth already guarantees email uniqueness; cache here for queries
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # App display name — set from user_metadata or onboarding
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "username": self.username or (self.email.split("@")[0] if self.email else "adventurer"),
            "email": self.email,
        }


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    random_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required_players: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    loot_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="frequent_gamble")
    # Monotonic fictional revision — incremented exactly once per authoritative fictional mutation.
    # See campaign_events.py commit_campaign_mutation().
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            # compat aliases
            "user_id": str(self.owner_id),
            "name": self.name,
            "description": self.description,
            "random_seed": self.random_seed,
            "required_players": self.required_players,
            "loot_mode": self.loot_mode,
            "loot_drop_rate": self.loot_mode,
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
    # Monotonically ordered within a campaign; 1-indexed (first event is 1).
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Source operation idempotency/observability hook (e.g. client-supplied op id or server-generated).
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Actor who caused the mutation (profile id) — provenance hook.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Targets / affected entities (free-form JSONB, e.g. ["entity_id", ...] or {"targets": [...]})
    targets: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Visibility hook for later projection policy (e.g. public/private/dm_only).
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public", server_default="public")
    # Free-form provenance (source, causal chain, etc.)
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


class IdempotentCommand(Base):
    """Durable acceptance record for an externally submitted command."""

    __tablename__ = "idempotent_commands"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "idempotency_key", "command_type", "scope_type", "scope_id",
            name="uq_idempotent_commands_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    result: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Outbox(Base):
    """Transactional outbox — issue #190.

    Authoritative state + domain event + outbox writes commit atomically.
    Relay publishes via stable id (outbox id) for idempotent downstream handling.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        # no hard uniqueness — duplicates are acceptable but operation_id aids dedupe
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False, default="campaign", server_default="campaign")
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id) if self.aggregate_id else None,
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "event_type": self.event_type,
            "operation_id": self.operation_id,
            "trace_id": self.trace_id,
            "payload": self.payload,
            "status": self.status,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "claimed_by": self.claimed_by,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class WorkerExecution(Base):
    """Durable worker execution / idempotent job ledger — issue #191.

    Keyed by logical job ID (stable UUID). At-least-once queue delivery must
    not duplicate side effects: a second delivery of the same job_id returns
    the existing successful result. Retries vs terminal failures are explicit,
    exhausted jobs stay durably inspectable, and manual replay reuses the same
    idempotency guarantee.
    """

    __tablename__ = "worker_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    aggregate_type: Mapped[str | None] = mapped_column(String(64), nullable=True, default="campaign")
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # pending | running | succeeded | failed | dead_letter
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # retriable | terminal | None
    error_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "job_type": self.job_type,
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id) if self.aggregate_id else None,
            "expected_revision": self.expected_revision,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "error_class": self.error_class,
            "result": self.result,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "processing_duration_ms": self.processing_duration_ms,
            "claim_token": self.claim_token,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class OperationTrace(Base):
    """Durable milestones for one logical operation; content-free by default."""

    __tablename__ = "operation_traces"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_visible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    narration_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted", server_default="submitted")
    telemetry_dropped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AIRun(Base):
    """Provider-independent AI attempt accounting, separate from game state."""

    __tablename__ = "ai_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    logical_operation: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)  # primary | recovery
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CampaignMember(Base):
    __tablename__ = "campaign_members"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="player")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CampaignThread(Base):
    """Durable thread identity for live-table messaging — issue #195.

    One shared ``campaign`` thread per campaign is the default audience;
    additional ``private`` threads hold explicit participant lists. Ownership
    never grants implicit private access — callers must be explicit members.
    """

    __tablename__ = "campaign_threads"
    __table_args__ = (
        CheckConstraint("thread_type IN ('campaign', 'private')", name="ck_campaign_threads_type"),
        Index(
            "uq_campaign_threads_one_campaign_per_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text("thread_type = 'campaign'"),
            sqlite_where=text("thread_type = 'campaign'"),
        ),
        Index(
            "uq_campaign_threads_private_key",
            "campaign_id",
            "private_key",
            unique=True,
            postgresql_where=text("private_key IS NOT NULL"),
            sqlite_where=text("private_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_type: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    private_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    private_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self, *, include_members: bool = False, members=None):
        result = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "thread_type": self.thread_type,
            "private_kind": self.private_kind,
            "title": self.title,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_members and members is not None:
            result["members"] = [m.to_dict() for m in members]
        return result


class CampaignThreadMember(Base):
    """Explicit audience membership for a thread — revocation is deletion."""

    __tablename__ = "campaign_thread_members"
    __table_args__ = (
        UniqueConstraint("thread_id", "user_id", name="uq_campaign_thread_members_identity"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_threads.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member", server_default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "thread_id": str(self.thread_id),
            "user_id": str(self.user_id),
            "role": self.role,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
        }


class PlayerSubmission(Base):
    """A durably accepted player contribution, before any AI interpretation."""

    __tablename__ = "player_submissions"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "thread_id", "sequence",
            name="uq_player_submissions_campaign_thread_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_player_submissions_positive_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, default="main", server_default="main")
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="accepted", server_default="accepted", index=True
    )
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, segments=None):
        result = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "user_id": str(self.user_id),
            "character_id": str(self.character_id) if self.character_id else None,
            "thread_id": self.thread_id,
            "audience": self.audience,
            "sequence": self.sequence,
            "raw_content": self.raw_content,
            "resolution_status": self.resolution_status,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
        if segments is not None:
            result["segments"] = [segment.to_dict() for segment in segments]
        return result


class PlayerSubmissionSegment(Base):
    """One ordered, semantically explicit IC/OOC part of a submission."""

    __tablename__ = "player_submission_segments"
    __table_args__ = (
        UniqueConstraint("submission_id", "position", name="uq_player_submission_segments_position"),
        CheckConstraint("position >= 0", name="ck_player_submission_segments_nonnegative_position"),
        CheckConstraint("segment_type IN ('ic', 'ooc')", name="ck_player_submission_segments_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("player_submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_type: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    def to_dict(self):
        return {"position": self.position, "type": self.segment_type, "text": self.text}


class DMStream(Base):
    """Durable DM stream — issue #197.

    One logical turn attempt maps to one stream. Chunks are appended
    idempotently in ordered sequence. A completed stream materializes a
    final display representation via concatenated chunk text; abandoned/failed
    streams remain auditable but are excluded from canonical completed history.
    """

    __tablename__ = "dm_streams"
    __table_args__ = (
        UniqueConstraint("turn_id", "attempt_id", name="uq_dm_streams_turn_attempt"),
        CheckConstraint("status IN ('streaming', 'completed', 'abandoned', 'failed')", name="ck_dm_streams_status"),
        Index("ix_dm_streams_campaign_thread", "campaign_id", "thread_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="streaming", server_default="streaming")
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    first_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    abandonment_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self, *, include_final: bool = True):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "thread_id": str(self.thread_id),
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "audience": self.audience,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "first_chunk_at": self.first_chunk_at.isoformat() if self.first_chunk_at else None,
            "last_chunk_at": self.last_chunk_at.isoformat() if self.last_chunk_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "abandoned_at": self.abandoned_at.isoformat() if self.abandoned_at else None,
            "chunk_count": self.chunk_count,
            "total_bytes": self.total_bytes,
            "last_sequence": self.last_sequence,
            "completion_reason": self.completion_reason,
            "abandonment_reason": self.abandonment_reason,
            "trace_id": self.trace_id,
            "operation_id": self.operation_id,
            "final_text": self.final_text if include_final else None,
        }


class DMStreamChunk(Base):
    """Ordered persisted chunk — one visible text fragment."""

    __tablename__ = "dm_stream_chunks"
    __table_args__ = (
        UniqueConstraint("stream_id", "sequence", name="uq_dm_stream_chunks_stream_sequence"),
        CheckConstraint("sequence >= 0", name="ck_dm_stream_chunks_nonnegative_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stream_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_streams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "stream_id": str(self.stream_id),
            "sequence": self.sequence,
            "text": self.text,
            "byte_length": self.byte_length,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DmTurn(Base):
    """Durable logical DM turn — issue #200.

    One logical turn may consume multiple player submissions that arrived
    within the same unresolved fictional moment (same campaign+thread/audience).
    The turn records the exact included submissions, the campaign revision
    observed at assembly, and the input-set revision for auditability.
    Streaming establishes a commitment boundary: once first visible chunk is
    committed, the input set cannot silently change. A campaign cannot advance
    to a conflicting later turn while a visible partial turn remains unresolved.
    """

    __tablename__ = "dm_turns"
    __table_args__ = (
        Index("ix_dm_turns_campaign_thread_status", "campaign_id", "thread_id", "status"),
        Index("ix_dm_turns_campaign_status", "campaign_id", "status"),
        # CAS: at most one active (pending/streaming/failed_visible) turn per thread
        Index(
            "uq_dm_turns_active_per_thread",
            "campaign_id",
            "thread_id",
            unique=True,
            postgresql_where=text("status IN ('pending','streaming','failed_visible')"),
            sqlite_where=text("status IN ('pending','streaming','failed_visible')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Durable thread id as stored in player_submissions.thread_id (UUID string).
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    # pending | streaming | succeeded | failed_visible | superseded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    # Campaign revision observed when the turn's input set was assembled (optimistic check at commit).
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Monotonic within this turn: increments each time included submissions expand pre-stream.
    input_set_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Canonical ordered list of submission UUID strings included in the current input set.
    submission_ids: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    streaming_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Observability: assembly window and timing.
    assembly_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assembly_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    streaming_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Time spent waiting for additional input before execution vs executing.
    time_waiting_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_executing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "thread_id": self.thread_id,
            "audience": self.audience,
            "status": self.status,
            "source_revision": self.source_revision,
            "input_set_revision": self.input_set_revision,
            "submission_ids": self.submission_ids or [],
            "current_attempt_id": str(self.current_attempt_id) if self.current_attempt_id else None,
            "streaming_attempt_id": str(self.streaming_attempt_id) if self.streaming_attempt_id else None,
            "assembly_window_start": self.assembly_window_start.isoformat() if self.assembly_window_start else None,
            "assembly_window_end": self.assembly_window_end.isoformat() if self.assembly_window_end else None,
            "streaming_started_at": self.streaming_started_at.isoformat() if self.streaming_started_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "time_waiting_ms": self.time_waiting_ms,
            "time_executing_ms": self.time_executing_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DmTurnAttempt(Base):
    """One attempt to resolve a logical DmTurn — issue #200.

    Attempts are lineage-linked: a new attempt supersedes a prior prepared/running
    attempt when additional eligible submissions arrive pre-stream. Once streaming
    starts, supersession is forbidden. Stale attempts that finish after supersession
    are discarded harmlessly (no campaign mutation). Worker crash leaves the attempt
    in running/prepared so recovery can retry.
    """

    __tablename__ = "dm_turn_attempts"
    __table_args__ = (
        UniqueConstraint("turn_id", "attempt_number", name="uq_dm_turn_attempts_turn_number"),
        Index("ix_dm_turn_attempts_turn_status", "turn_id", "status"),
        Index("ix_dm_turn_attempts_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_turns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # prepared | running | superseded | streaming | succeeded | failed | failed_visible | discarded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared", server_default="prepared", index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_set_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_ids: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    # Lineage: parent attempt that was superseded to create this one.
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_turn_attempts.id", ondelete="SET NULL"), nullable=True
    )
    worker_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("worker_executions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invalidation_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    streaming_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Observability: assembly window for this specific attempt.
    assembly_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assembly_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "turn_id": str(self.turn_id),
            "attempt_number": self.attempt_number,
            "status": self.status,
            "campaign_id": str(self.campaign_id),
            "thread_id": self.thread_id,
            "audience": self.audience,
            "source_revision": self.source_revision,
            "input_set_revision": self.input_set_revision,
            "submission_ids": self.submission_ids or [],
            "parent_attempt_id": str(self.parent_attempt_id) if self.parent_attempt_id else None,
            "worker_job_id": str(self.worker_job_id) if self.worker_job_id else None,
            "invalidation_reason": self.invalidation_reason,
            "invalidated_at": self.invalidated_at.isoformat() if self.invalidated_at else None,
            "streaming_started_at": self.streaming_started_at.isoformat() if self.streaming_started_at else None,
            "last_error": self.last_error,
            "error_class": self.error_class,
            "assembly_window_start": self.assembly_window_start.isoformat() if self.assembly_window_start else None,
            "assembly_window_end": self.assembly_window_end.isoformat() if self.assembly_window_end else None,
            "processing_duration_ms": self.processing_duration_ms,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignInvite(Base):
    __tablename__ = "campaign_invites"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Character(Base):
    """Generic character identity. FK target for all system-specific sheets.

    Lets the app refer to a 'character' without caring about ruleset.
    System-specific data lives in 1:1 tables like dnd5e_character_sheets.
    """

    __tablename__ = "characters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    system: Mapped[str] = mapped_column(String(32), nullable=False, default="dnd5e")  # dnd5e | pf2e | etc.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "system": self.system,
            "name": self.name,
        }


class CharacterChatMessage(Base):
    __tablename__ = "character_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "character_id": str(self.character_id) if self.character_id else None,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Dnd5eCharacterSheet(Base):
    """Full D&D 5e character sheet. Named explicitly for 5e to allow future systems.

    Canonical DTO matches frontend CharacterDraft shape where possible:
    - frontend sends `name`, `total_level`, `max_hp`/`current_hp`/`temp_hp`, `skills[]`,
      `saving_throws[]`, `weapons[]`, `resources[]`, `companions[]`, `conditions[]`,
      spell slots as `{max, used}`.
    - DB keeps `character_name`/`level`/`hit_points_*` etc. as storage columns;
      to_dict() emits both legacy and frontend aliases plus JSONB lists.
    - Derived values (level, proficiency_bonus, passive_perception, spell DC/attack)
      are stored as overrides; canonical values are computed from classes/abilities
      when not overridden.

    Covers the official 5e sheet sections:
    - Identity (name, race, class/level, background, alignment, XP)
    - Ability scores + modifiers (stored as base scores, mods computed)
    - Inspiration / proficiency bonus (override)
    - Saving throws & skills (both per-flag booleans and list JSONB)
    - Combat (AC, initiative, speed, HP, hit dice, death saves, exhaustion)
    - Attacks/weapons, equipment, currency
    - Spellcasting, features & traits, proficiencies/languages, resources, companions, conditions
    - Flavor (personality, ideals, bonds, flaws, appearance, backstory)
    - Extras JSONB for homebrew / future fields
    """

    __tablename__ = "dnd5e_character_sheets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Generic character FK for future system support; nullable for back-compat. New rows should set character_id.
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ── Identity ───────────────────────────────────────────────────────
    character_name: Mapped[str] = mapped_column(String(128), nullable=False)
    player_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    race: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subrace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    background: Mapped[str | None] = mapped_column(String(64), nullable=True)
    alignment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Multiclass support via JSONB + denormalized display fields
    char_class: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "Fighter 3 / Wizard 2"
    classes: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"class_name": "Fighter", "level": 3, "subclass": "Champion"}]
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    experience_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Flavor / descriptive
    age: Mapped[str | None] = mapped_column(String(32), nullable=True)
    height: Mapped[str | None] = mapped_column(String(32), nullable=True)
    weight: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eyes: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hair: Mapped[str | None] = mapped_column(String(32), nullable=True)
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    backstory: Mapped[str | None] = mapped_column(Text, nullable=True)
    allies_and_organizations: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality_traits: Mapped[str | None] = mapped_column(Text, nullable=True)
    ideals: Mapped[str | None] = mapped_column(Text, nullable=True)
    bonds: Mapped[str | None] = mapped_column(Text, nullable=True)
    flaws: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Ability Scores (3-30) ──────────────────────────────────────────
    strength: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    dexterity: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    constitution: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    intelligence: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    wisdom: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    charisma: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    # ── Inspiration & Proficiency ──────────────────────────────────────
    inspiration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proficiency_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    # Saving throw proficiencies
    str_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dex_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    con_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    int_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wis_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cha_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Skill proficiencies (18) + expertise flags stored in JSONB for flexibility
    acrobatics_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    animal_handling_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    arcana_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    athletics_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deception_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    insight_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    intimidation_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    investigation_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    medicine_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nature_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    perception_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    performance_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persuasion_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    religion_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sleight_of_hand_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stealth_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    survival_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # expertise / custom skill mods
    skill_expertise: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # e.g. {"stealth": true, "perception": true}

    # Frontend-aligned lists (canonical for draft round-trip; booleans above are denormalized overrides)
    skills: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"skill_name":"Stealth","is_proficient":true,"is_expertise":false}]
    saving_throws: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"ability":"dexterity","is_proficient":true}]

    passive_perception: Mapped[int | None] = mapped_column(Integer, nullable=True)  # override, else 10+WIS+prof

    # ── Combat ─────────────────────────────────────────────────────────
    armor_class: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    initiative_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speed: Mapped[int] = mapped_column(Integer, nullable=False, default=30)  # feet per round
    speed_details: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g. "fly 30, swim 20"

    hit_points_max: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_current: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_temp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_dice: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "1d8"
    hit_dice_total: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "3d8"
    hit_dice_remaining: Mapped[str | None] = mapped_column(String(32), nullable=True)
    death_save_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    death_save_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exhaustion_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encumbrance_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # frontend alias for encumbrance

    # ── Attacks & Spellcasting ─────────────────────────────────────────
    attacks: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Longsword","bonus":5,"damage":"1d8+3","type":"slashing"}]
    weapons: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # frontend alias: same shape as attacks, kept separate for round-trip
    spellcasting_ability: Mapped[str | None] = mapped_column(String(16), nullable=True)  # int/wis/cha
    spell_save_dc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_attack_bonus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_slots: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"1": {"max":4,"used":1}, ...} canonical frontend shape
    spells: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Fireball","level":3,"prepared":true}]
    cantrips: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # ── Equipment / Treasure ───────────────────────────────────────────
    equipment: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Rope","qty":1,"weight":5}]
    equipment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    treasure: Mapped[str | None] = mapped_column(Text, nullable=True)
    cp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ep: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encumbrance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # legacy alias kept; new code uses encumbrance_status

    # ── Features, Traits, Proficiencies ────────────────────────────────
    features_and_traits: Mapped[str | None] = mapped_column(Text, nullable=True)  # class/racial features
    features: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # frontend structured features[]
    proficiencies: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # frontend proficiencies[]
    other_proficiencies_languages: Mapped[str | None] = mapped_column(Text, nullable=True)
    proficiencies_languages_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    companions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    conditions: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # ── Extras ─────────────────────────────────────────────────────────
    extras: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # homebrew / future game fields
    portrait_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        base = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        # id as string for frontend UUID handling
        base["id"] = str(base["id"]) if base.get("id") else None
        base["character_id"] = str(base["character_id"]) if base.get("character_id") else None
        base["owner_id"] = str(base["owner_id"]) if base.get("owner_id") else None
        # frontend aliases for round-trip
        base["name"] = base.get("character_name")
        base["total_level"] = base.get("level")
        base["max_hp"] = base.get("hit_points_max")
        base["current_hp"] = base.get("hit_points_current")
        base["temp_hp"] = base.get("hit_points_temp")
        # encumbrance alias
        base["encumbrance_status"] = base.get("encumbrance_status") or base.get("encumbrance")
        # weapons alias
        if base.get("weapons") is None and base.get("attacks") is not None:
            base["weapons"] = base["attacks"]
        # keep skill_expertise as is; frontend can read either
        return base

    @classmethod
    def from_frontend(cls, data: dict, owner_id: uuid.UUID):
        """Create instance from frontend CharacterDraft shape."""
        # map frontend names to storage columns
        mapped = {}
        if "name" in data:
            mapped["character_name"] = data["name"]
        elif "character_name" in data:
            mapped["character_name"] = data["character_name"]
        else:
            mapped["character_name"] = "Unnamed Hero"
        for k in ("player_name", "race", "subrace", "background", "alignment", "experience_points"):
            if k in data:
                mapped[k] = data[k]
        if "total_level" in data:
            mapped["level"] = data["total_level"]
        elif "level" in data:
            mapped["level"] = data["level"]
        # combat
        if "max_hp" in data:
            mapped["hit_points_max"] = data["max_hp"]
        if "current_hp" in data:
            mapped["hit_points_current"] = data["current_hp"]
        if "temp_hp" in data:
            mapped["hit_points_temp"] = data["temp_hp"]
        for k in ("armor_class", "initiative_bonus", "speed", "death_save_successes", "death_save_failures", "exhaustion_level"):
            if k in data:
                mapped[k] = data[k]
        # compat: frontend sends combat.abilities etc flattened already via toCharacterPayload
        for k in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma",
                  "inspiration", "proficiency_bonus", "passive_perception",
                  "armor_class", "initiative_bonus", "speed"):
            if k in data:
                mapped[k] = data[k]
        # lists
        for k in ("classes", "skills", "saving_throws", "proficiencies", "features", "weapons", "equipment", "spells", "resources", "companions", "conditions"):
            if k in data:
                mapped[k] = data[k]
        # attacks alias
        if "attacks" in data and "weapons" not in mapped:
            mapped["weapons"] = data["attacks"]
        # spellcasting
        for k in ("spellcasting_ability", "spell_save_dc", "spell_attack_bonus", "spell_slots", "cantrips"):
            if k in data:
                mapped[k] = data[k]
        if "encumbrance_status" in data:
            mapped["encumbrance_status"] = data["encumbrance_status"]
            mapped["encumbrance"] = data["encumbrance_status"]
        mapped["owner_id"] = owner_id
        # per-skill booleans from skills[] if provided
        # (kept for queryability; not required for round-trip)
        return cls(**{k: v for k, v in mapped.items() if k in cls.__table__.columns})
