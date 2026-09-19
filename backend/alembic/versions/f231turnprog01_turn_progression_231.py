"""turn progression + action economy + skip votes — issue #231 (additive).

Revision ID: f231turnprog01
Revises: d383teleme01

Additive only: encounter turn-sequence/blocked/counter columns plus
encounter_turn_states (per-participant action economy) and
encounter_skip_votes (durable party skip flow). No existing tables or rows
touched beyond new nullable/defaulted columns.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f231turnprog01"
down_revision: Union[str, Sequence[str], None] = "d383teleme01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("encounters", sa.Column("turn_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("encounters", sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("encounters", sa.Column("blocked_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("encounters", sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("encounters", sa.Column("invalid_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("encounters", sa.Column("last_turn_duration_ms", sa.Integer(), nullable=True))
    op.add_column("encounters", sa.Column("last_end_turn_latency_ms", sa.Integer(), nullable=True))

    op.create_table(
        "encounter_turn_states",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("participant_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("action_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("bonus_action_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("reaction_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("movement_remaining", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("movement_max", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("extra_resources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("turn_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", "participant_id", name="uq_turn_states_encounter_participant"),
    )
    op.create_index("ix_turn_states_encounter", "encounter_turn_states", ["encounter_id"])

    op.create_table(
        "encounter_skip_votes",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("target_participant_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("voter_user_id", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("turn_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "encounter_id", "target_participant_id", "turn_sequence", "voter_user_id",
            name="uq_skip_votes_encounter_target_seq_voter",
        ),
    )
    op.create_index("ix_skip_votes_encounter", "encounter_skip_votes", ["encounter_id"])


def downgrade() -> None:
    op.drop_index("ix_skip_votes_encounter", table_name="encounter_skip_votes")
    op.drop_table("encounter_skip_votes")
    op.drop_index("ix_turn_states_encounter", table_name="encounter_turn_states")
    op.drop_table("encounter_turn_states")
    op.drop_column("encounters", "last_end_turn_latency_ms")
    op.drop_column("encounters", "last_turn_duration_ms")
    op.drop_column("encounters", "invalid_attempt_count")
    op.drop_column("encounters", "skipped_count")
    op.drop_column("encounters", "blocked_since")
    op.drop_column("encounters", "turn_started_at")
    op.drop_column("encounters", "turn_sequence")
