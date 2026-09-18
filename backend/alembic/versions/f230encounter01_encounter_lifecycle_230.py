"""authoritative encounter lifecycle — issue #230 (additive).

Revision ID: f230encounter01
Revises: d3a263a1f263

Additive only: creates encounters + encounter_participants (lifecycle,
participants, initiative, rounds, active turn). Does not touch existing
tables or rows.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f230encounter01"
down_revision: Union[str, Sequence[str], None] = "d3a263a1f263"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "encounters",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending_initiative"),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_participant_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("active_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scene_location_entity_id", sa.UUID(as_uuid=True), sa.ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("scene_location_name", sa.String(length=256), nullable=True),
        sa.Column("map_ref", sa.String(length=256), nullable=True),
        sa.Column("start_source", sa.String(length=24), nullable=False, server_default="api"),
        sa.Column("source_turn_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_attempt_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turn_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_event_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ended_event_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("participant_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("turn_order_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tie_resolution", sa.Text(), nullable=True),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initiative_wait_ms", sa.Integer(), nullable=True),
        sa.Column("time_to_first_turn_ms", sa.Integer(), nullable=True),
        sa.Column("roll_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("campaign_id", "operation_id", name="uq_encounters_campaign_operation"),
        sa.CheckConstraint("status IN ('pending_initiative', 'active', 'ended')", name="ck_encounters_status"),
        sa.CheckConstraint("start_source IN ('dm_effect', 'api')", name="ck_encounters_start_source"),
    )
    op.create_index("ix_encounters_campaign_status", "encounters", ["campaign_id", "status"])
    op.create_index(
        "uq_encounters_one_active_per_campaign",
        "encounters",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_initiative', 'active')"),
        sqlite_where=sa.text("status IN ('pending_initiative', 'active')"),
    )
    op.create_table(
        "encounter_participants",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("participant_key", sa.String(length=96), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("character_id", sa.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("npc_entity_id", sa.UUID(as_uuid=True), sa.ForeignKey("world_entities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("controller_user_id", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("initiative_modifier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dex_modifier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stat_source", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stat_visibility", sa.String(length=16), nullable=False, server_default="public"),
        sa.Column("initiative_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("roll_request_id", sa.UUID(as_uuid=True), sa.ForeignKey("player_roll_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("raw_roll", sa.Integer(), nullable=True),
        sa.Column("initiative_total", sa.Integer(), nullable=True),
        sa.Column("roll_source", sa.String(length=16), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("is_tied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", "participant_key", name="uq_encounter_participants_encounter_key"),
        sa.UniqueConstraint("roll_request_id", name="uq_encounter_participants_roll_request"),
        sa.CheckConstraint("kind IN ('pc', 'npc', 'monster')", name="ck_encounter_participants_kind"),
        sa.CheckConstraint("initiative_status IN ('pending', 'fulfilled')", name="ck_encounter_participants_initiative_status"),
        sa.CheckConstraint(
            "roll_source IS NULL OR roll_source IN ('human_app', 'human_physical', 'dm_runtime')",
            name="ck_encounter_participants_roll_source",
        ),
        sa.CheckConstraint("stat_visibility IN ('public', 'dm_private')", name="ck_encounter_participants_stat_visibility"),
    )
    op.create_index("ix_encounter_participants_encounter", "encounter_participants", ["encounter_id"])
    op.create_index("ix_encounter_participants_campaign", "encounter_participants", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_encounter_participants_campaign", table_name="encounter_participants")
    op.drop_index("ix_encounter_participants_encounter", table_name="encounter_participants")
    op.drop_table("encounter_participants")
    op.drop_index("uq_encounters_one_active_per_campaign", table_name="encounters")
    op.drop_index("ix_encounters_campaign_status", table_name="encounters")
    op.drop_table("encounters")
