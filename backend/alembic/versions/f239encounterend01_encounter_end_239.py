"""DM-controlled encounter end + post-combat hooks — issue #239 (additive).

Revision ID: f239encounterend01
Revises: a218clocks01

Additive only: encounter end columns (outcome/reason/actor/operation/
participant outcomes/duration/observability counters) plus
encounter_end_followups (durable loot/XP/death/custody/post-turn hooks).
No existing tables or rows touched beyond new nullable/defaulted columns.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f239encounterend01"
down_revision: Union[str, Sequence[str], None] = "a218clocks01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("encounters", sa.Column("end_outcome", sa.String(length=32), nullable=True))
    op.add_column("encounters", sa.Column("end_reason", sa.Text(), nullable=True))
    op.add_column("encounters", sa.Column("ended_by", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True))
    op.add_column("encounters", sa.Column("end_operation_id", sa.String(length=128), nullable=True))
    op.add_column("encounters", sa.Column("end_participant_outcomes", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("encounters", sa.Column("end_duration_ms", sa.Integer(), nullable=True))
    op.add_column("encounters", sa.Column("duplicate_end_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("encounters", sa.Column("followup_failure_count", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "encounter_end_followups",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("hook_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", "hook_type", name="uq_end_followups_encounter_hook"),
        sa.CheckConstraint(
            "hook_type IN ('loot_availability', 'xp_progression', 'death_aftermath', 'custody_state', 'post_turn_consolidation')",
            name="ck_end_followups_hook_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'failed')",
            name="ck_end_followups_status",
        ),
    )
    op.create_index("ix_end_followups_encounter", "encounter_end_followups", ["encounter_id"])


def downgrade() -> None:
    op.drop_index("ix_end_followups_encounter", table_name="encounter_end_followups")
    op.drop_table("encounter_end_followups")
    op.drop_column("encounters", "followup_failure_count")
    op.drop_column("encounters", "duplicate_end_count")
    op.drop_column("encounters", "end_duration_ms")
    op.drop_column("encounters", "end_participant_outcomes")
    op.drop_column("encounters", "end_operation_id")
    op.drop_column("encounters", "ended_by")
    op.drop_column("encounters", "end_reason")
    op.drop_column("encounters", "end_outcome")
