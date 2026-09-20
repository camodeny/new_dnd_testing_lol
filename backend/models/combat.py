"""Authoritative encounter lifecycle models — issue #230.

Two tables:

- Encounter: one authoritative combat lifecycle row per started encounter.
  Status ``pending_initiative`` until every required initiative roll exists,
  then ``active`` with round 1 and the first turn established. ``ended``
  exists in the lifecycle vocabulary for the future #239 end path; this
  issue never transitions to it.
- EncounterParticipant: one row per explicitly selected combatant,
  referencing canonical PC (characters) or NPC/monster (world_entities)
  identities plus a frozen combat-stat source snapshot.

Current-state rows are authoritative for reads; ``campaign_domain_events``
(``encounter.started`` / ``encounter.initiative_ready``) provide provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

ENCOUNTER_STATUSES = ("pending_initiative", "active", "ended")
PARTICIPANT_KINDS = ("pc", "npc", "monster")
INITIATIVE_STATUSES = ("pending", "fulfilled")
ROLL_SOURCES = ("human_app", "human_physical", "dm_runtime")
STAT_VISIBILITIES = ("public", "dm_private")

# Entity visibilities treated as hidden: non-player stats resolved from these
# stay DM-private in every non-owner projection.
HIDDEN_ENTITY_VISIBILITIES = frozenset({"dm_only", "dm_private", "private", "hidden"})


class Encounter(Base):
    __tablename__ = "encounters"
    __table_args__ = (
        UniqueConstraint("campaign_id", "operation_id", name="uq_encounters_campaign_operation"),
        CheckConstraint(
            "status IN ('pending_initiative', 'active', 'ended')",
            name="ck_encounters_status",
        ),
        CheckConstraint(
            "start_source IN ('dm_effect', 'api')",
            name="ck_encounters_start_source",
        ),
        Index("ix_encounters_campaign_status", "campaign_id", "status"),
        # Lifecycle invariant: at most one pending/active encounter per
        # campaign, enforced in PostgreSQL so concurrent starts cannot both
        # insert. The service also serializes on the campaign row.
        Index(
            "uq_encounters_one_active_per_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text("status IN ('pending_initiative', 'active')"),
            sqlite_where=text("status IN ('pending_initiative', 'active')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Source-turn thread for snapshot/realtime scoping (dm_turns.thread_id shape).
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_initiative", server_default="pending_initiative")
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # First turn of round 1 once initiative is complete; null while pending.
    # Plain UUID (no FK) to avoid a circular FK with encounter_participants.
    active_participant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    active_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Encounter-local revision: 1 at start, +1 on initiative-ready.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Scene/map links: canonical location entity + display name + opaque map ref.
    scene_location_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True
    )
    scene_location_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    map_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    start_source: Mapped[str] = mapped_column(String(24), nullable=False, default="api", server_default="api")
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_turns.id", ondelete="SET NULL"), nullable=True
    )
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dm_turn_attempts.id", ondelete="SET NULL"), nullable=True
    )
    created_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True
    )
    ended_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True
    )
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Authoritative order: ordered participant id strings, set at ready time.
    turn_order_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tie_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Observability (durable): initiative wait + time-to-first-turn in ms,
    # per-participant roll source map.
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    initiative_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_first_turn_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roll_sources: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # ── Turn progression (issue #231) ─────────────────────────────────────
    # Monotonic per-encounter turn counter: 0 until initiative is ready, 1 for
    # the first turn, +1 on every end-turn/skip advance. End-turn and skip
    # commands bind the sequence they observed (source turn); a mismatch
    # fails closed as stale instead of advancing twice.
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the party opens a skip vote against the blocking active PC;
    # cleared on every advance. Observability: blocked-on-player duration.
    blocked_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    invalid_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_turn_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_end_turn_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "thread_id": self.thread_id,
            "status": self.status,
            "round": self.round,
            "active_participant_id": str(self.active_participant_id) if self.active_participant_id else None,
            "active_index": self.active_index,
            "revision": self.revision,
            "scene_location_entity_id": str(self.scene_location_entity_id) if self.scene_location_entity_id else None,
            "scene_location_name": self.scene_location_name,
            "map_ref": self.map_ref,
            "start_source": self.start_source,
            "source_turn_id": str(self.source_turn_id) if self.source_turn_id else None,
            "source_attempt_id": str(self.source_attempt_id) if self.source_attempt_id else None,
            "created_event_id": str(self.created_event_id) if self.created_event_id else None,
            "ended_event_id": str(self.ended_event_id) if self.ended_event_id else None,
            "operation_id": self.operation_id,
            "participant_count": self.participant_count,
            "turn_order_ids": list(self.turn_order_ids or []),
            "tie_resolution": self.tie_resolution,
            "initiated_at": self.initiated_at.isoformat() if self.initiated_at else None,
            "ready_at": self.ready_at.isoformat() if self.ready_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "initiative_wait_ms": self.initiative_wait_ms,
            "time_to_first_turn_ms": self.time_to_first_turn_ms,
            "roll_sources": dict(self.roll_sources or {}),
            "turn_sequence": int(self.turn_sequence or 0),
            "turn_started_at": self.turn_started_at.isoformat() if self.turn_started_at else None,
            "blocked_since": self.blocked_since.isoformat() if self.blocked_since else None,
            "skipped_count": int(self.skipped_count or 0),
            "invalid_attempt_count": int(self.invalid_attempt_count or 0),
            "last_turn_duration_ms": self.last_turn_duration_ms,
            "last_end_turn_latency_ms": self.last_end_turn_latency_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EncounterParticipant(Base):
    __tablename__ = "encounter_participants"
    __table_args__ = (
        UniqueConstraint("encounter_id", "participant_key", name="uq_encounter_participants_encounter_key"),
        UniqueConstraint("roll_request_id", name="uq_encounter_participants_roll_request"),
        CheckConstraint("kind IN ('pc', 'npc', 'monster')", name="ck_encounter_participants_kind"),
        CheckConstraint(
            "initiative_status IN ('pending', 'fulfilled')",
            name="ck_encounter_participants_initiative_status",
        ),
        CheckConstraint(
            "roll_source IS NULL OR roll_source IN ('human_app', 'human_physical', 'dm_runtime')",
            name="ck_encounter_participants_roll_source",
        ),
        CheckConstraint(
            "stat_visibility IN ('public', 'dm_private')",
            name="ck_encounter_participants_stat_visibility",
        ),
        Index("ix_encounter_participants_encounter", "encounter_id"),
        Index("ix_encounter_participants_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Idempotency within the encounter: "pc:<character_id>" or
    # "npc|monster:<entity_id>" (+ ":<n>" when one entity fields multiples).
    participant_key: Mapped[str] = mapped_column(String(96), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    npc_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True
    )
    controller_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Frozen at start: total initiative modifier + dex component for tiebreaks.
    initiative_modifier: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dex_modifier: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stat_source: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    stat_visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="public", server_default="public")
    initiative_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # Human-PC link to the #204 durable roll request; null for NPC/monster
    # (DM/runtime path writes raw_roll/total directly, no request row).
    roll_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("player_roll_requests.id", ondelete="SET NULL"), nullable=True
    )
    raw_roll: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initiative_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roll_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Authoritative position once the encounter is ready; null while pending.
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_tied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self, *, include_private: bool = False):
        value = {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "campaign_id": str(self.campaign_id),
            "participant_key": self.participant_key,
            "kind": self.kind,
            "character_id": str(self.character_id) if self.character_id else None,
            "npc_entity_id": str(self.npc_entity_id) if self.npc_entity_id else None,
            "controller_user_id": str(self.controller_user_id) if self.controller_user_id else None,
            "display_name": self.display_name,
            "stat_visibility": self.stat_visibility,
            "initiative_status": self.initiative_status,
            "initiative_total": self.initiative_total,
            "sort_order": self.sort_order,
            "is_tied": bool(self.is_tied),
            "roll_request_id": str(self.roll_request_id) if self.roll_request_id else None,
            "fulfilled_at": self.fulfilled_at.isoformat() if self.fulfilled_at else None,
        }
        if include_private:
            value.update(
                initiative_modifier=self.initiative_modifier,
                dex_modifier=self.dex_modifier,
                stat_source=self.stat_source or {},
                raw_roll=self.raw_roll,
                roll_source=self.roll_source,
            )
        return value


class EncounterTurnState(Base):
    """Durable per-participant action economy — issue #231.

    One row per encounter participant, created when initiative completes.
    Resources reset at the participant's own turn start (action, bonus
    action, movement to max, reaction per 2024 refresh, per-turn extras to
    max). Current-state rows are authoritative for reads; turn Started/Ended
    domain events provide provenance.
    """

    __tablename__ = "encounter_turn_states"
    __table_args__ = (
        UniqueConstraint("encounter_id", "participant_id", name="uq_turn_states_encounter_participant"),
        Index("ix_turn_states_encounter", "encounter_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    bonus_action_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    reaction_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    movement_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    movement_max: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    # Extensible per-turn resources: {name: {"max": int, "remaining": int}}.
    extra_resources: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    turn_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "participant_id": str(self.participant_id),
            "action_available": bool(self.action_available),
            "bonus_action_available": bool(self.bonus_action_available),
            "reaction_available": bool(self.reaction_available),
            "movement_remaining": int(self.movement_remaining),
            "movement_max": int(self.movement_max),
            "extra_resources": dict(self.extra_resources or {}),
            "turn_started_at": self.turn_started_at.isoformat() if self.turn_started_at else None,
            "turn_ended_at": self.turn_ended_at.isoformat() if self.turn_ended_at else None,
        }


class EncounterSkipVote(Base):
    """Durable party skip vote — issue #231.

    One row per (encounter, target, turn sequence, voter). Votes are scoped
    to the turn sequence active when cast so stale votes from earlier turns
    never accumulate toward a later skip. A skipped PC is never AI-played:
    skip advances initiative with no actions generated.
    """

    __tablename__ = "encounter_skip_votes"
    __table_args__ = (
        UniqueConstraint(
            "encounter_id", "target_participant_id", "turn_sequence", "voter_user_id",
            name="uq_skip_votes_encounter_target_seq_voter",
        ),
        Index("ix_skip_votes_encounter", "encounter_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False
    )
    voter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "target_participant_id": str(self.target_participant_id),
            "voter_user_id": str(self.voter_user_id) if self.voter_user_id else None,
            "turn_sequence": int(self.turn_sequence or 0),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ── Authoritative VTT map geometry — issue #232 ──────────────────────────────

#: Launch-supported diagonal policy values. ``no_corner_cut`` (default) blocks
#: diagonal steps that cut across a blocked orthogonal corner; the permissive
#: variant allows them. Stored per map so the launch policy is auditable.
DIAGONAL_POLICIES = ("no_corner_cut", "allow_corner_cut")

#: Launch-supported terrain kinds. ``blocked`` denies entry; ``difficult``
#: multiplies entry cost; ``open`` is an explicit override back to cost 1
#: (lets DM-authored changes clear earlier zones without deleting rows).
TERRAIN_KINDS = ("blocked", "difficult", "open")

MAP_TERRAIN_VISIBILITIES = ("public", "dm_only")

#: Launch-supported movement modes. Only ``walk`` resolves against turn-state
#: budgets today; the column stays extensible for future modes.
MOVEMENT_MODES = ("walk",)


class EncounterMap(Base):
    """Authoritative encounter grid geometry — issue #232.

    One row per encounter: rectangular grid dimensions plus the deterministic
    diagonal policy. Deliberately separate from generated background art: the
    opaque ``background_art_ref`` never participates in legality math.
    ``revision`` bumps on every geometry/terrain/placement mutation so
    reconnects and projections can detect stale map reads.
    """

    __tablename__ = "encounter_maps"
    __table_args__ = (
        UniqueConstraint("encounter_id", name="uq_encounter_maps_encounter"),
        CheckConstraint(
            "diagonal_policy IN ('no_corner_cut', 'allow_corner_cut')",
            name="ck_encounter_maps_diagonal_policy",
        ),
        Index("ix_encounter_maps_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    diagonal_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, default="no_corner_cut", server_default="no_corner_cut"
    )
    # Opaque background-art reference (generated image id/URL). Never read by
    # geometry, pathing, or legality code — placement math uses width/height
    # and terrain zones only.
    background_art_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "campaign_id": str(self.campaign_id),
            "width": int(self.width),
            "height": int(self.height),
            "diagonal_policy": self.diagonal_policy,
            "background_art_ref": self.background_art_ref,
            "revision": int(self.revision or 1),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EncounterTerrainZone(Base):
    """One DM-authored terrain rectangle — issue #232.

    Zones overlay the grid in creation order (later rows win on overlap) so a
    DM change can re-carve earlier terrain without rewriting history.
    ``label`` is DM-authored prose and may stay ``dm_only``; projections
    strip it for non-owners while keeping the mechanical effect.
    """

    __tablename__ = "encounter_terrain_zones"
    __table_args__ = (
        CheckConstraint("kind IN ('blocked', 'difficult', 'open')", name="ck_terrain_zones_kind"),
        CheckConstraint(
            "visibility IN ('public', 'dm_only')",
            name="ck_terrain_zones_visibility",
        ),
        Index("ix_terrain_zones_map", "map_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounter_maps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    rect_col: Mapped[int] = mapped_column(Integer, nullable=False)
    rect_row: Mapped[int] = mapped_column(Integer, nullable=False)
    rect_width: Mapped[int] = mapped_column(Integer, nullable=False)
    rect_height: Mapped[int] = mapped_column(Integer, nullable=False)
    # Entry-cost multiplier for ``difficult`` (baseline 2); ignored for
    # ``blocked``/``open``. Extensible per-zone tuning without new kinds.
    cost_multiplier: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="public", server_default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self, *, include_dm_label: bool = False):
        value = {
            "id": str(self.id),
            "kind": self.kind,
            "rect": {
                "col": int(self.rect_col),
                "row": int(self.rect_row),
                "width": int(self.rect_width),
                "height": int(self.rect_height),
            },
            "cost_multiplier": int(self.cost_multiplier),
            "visibility": self.visibility,
        }
        if include_dm_label:
            value["label"] = self.label
        return value


class EncounterPlacement(Base):
    """Durable token placement — issue #232.

    One row per encounter participant: grid cell coordinates. Null row means
    the participant has no token on the map yet (e.g. map initialized before
    they were placed). Positions are authoritative backend state — reconnects
    rebuild from these rows, never from narration.
    """

    __tablename__ = "encounter_placements"
    __table_args__ = (
        UniqueConstraint("encounter_id", "participant_id", name="uq_placements_encounter_participant"),
        Index("ix_placements_encounter", "encounter_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "participant_id": str(self.participant_id),
            "col": int(self.col),
            "row": int(self.row),
        }


class EncounterMove(Base):
    """Durable movement ledger — issue #232.

    One row per committed move, unique on (encounter_id, operation_id): a
    duplicate command replays the recorded outcome instead of moving or
    spending twice. The row pins the turn sequence so a stale retry from an
    older turn can never double-spend a later budget.
    """

    __tablename__ = "encounter_moves"
    __table_args__ = (
        UniqueConstraint("encounter_id", "operation_id", name="uq_moves_encounter_operation"),
        Index("ix_moves_encounter", "encounter_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    movement_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="walk", server_default="walk")
    from_col: Mapped[int] = mapped_column(Integer, nullable=False)
    from_row: Mapped[int] = mapped_column(Integer, nullable=False)
    to_col: Mapped[int] = mapped_column(Integer, nullable=False)
    to_row: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_squares: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_feet: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "encounter_id": str(self.encounter_id),
            "participant_id": str(self.participant_id),
            "operation_id": self.operation_id,
            "movement_mode": self.movement_mode,
            "from": {"col": int(self.from_col), "row": int(self.from_row)},
            "to": {"col": int(self.to_col), "row": int(self.to_row)},
            "cost_squares": int(self.cost_squares),
            "cost_feet": int(self.cost_feet),
            "path": list(self.path or []),
            "turn_sequence": int(self.turn_sequence or 0),
        }
