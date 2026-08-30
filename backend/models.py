"""App tables that live in Supabase Postgres.

- Supabase Auth owns `auth.users` (managed by GoTrue). Do NOT duplicate it.
- We keep a thin `public.profiles` mirror synced on login (id = auth.users.id UUID).
  This is where app-specific fields live and where foreign keys should point.

If you prefer to use `auth.users` directly without a mirror, point FKs there — but
`profiles` is the Supabase-recommended pattern (lets you add username, etc.).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
