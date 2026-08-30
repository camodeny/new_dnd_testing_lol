"""add durable DM turn/attempt state machine — issue 200

Revision ID: c1d2e3f4a5b6
Revises: c8d9e0f1a2b3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dm_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("audience", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("input_set_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("submission_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("current_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("streaming_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assembly_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assembly_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("streaming_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_waiting_ms", sa.Integer(), nullable=True),
        sa.Column("time_executing_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dm_turns_campaign_id", "dm_turns", ["campaign_id"])
    op.create_index("ix_dm_turns_thread_id", "dm_turns", ["thread_id"])
    op.create_index("ix_dm_turns_status", "dm_turns", ["status"])
    op.create_index("ix_dm_turns_campaign_thread_status", "dm_turns", ["campaign_id", "thread_id", "status"])
    op.create_index("ix_dm_turns_campaign_status", "dm_turns", ["campaign_id", "status"])
    # CAS: at most one active turn per thread (pending/streaming/failed_visible)
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_dm_turns_active_per_thread ON dm_turns (campaign_id, thread_id) "
            "WHERE status IN ('pending','streaming','failed_visible')"
        )
    )

    op.create_table(
        "dm_turn_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("audience", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("input_set_revision", sa.Integer(), nullable=False),
        sa.Column("submission_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("parent_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("worker_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(256), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("streaming_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(32), nullable=True),
        sa.Column("assembly_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assembly_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_attempt_id"], ["dm_turn_attempts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["dm_turns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_job_id"], ["worker_executions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "attempt_number", name="uq_dm_turn_attempts_turn_number"),
    )
    op.create_index("ix_dm_turn_attempts_turn_id", "dm_turn_attempts", ["turn_id"])
    op.create_index("ix_dm_turn_attempts_campaign_id", "dm_turn_attempts", ["campaign_id"])
    op.create_index("ix_dm_turn_attempts_thread_id", "dm_turn_attempts", ["thread_id"])
    op.create_index("ix_dm_turn_attempts_status", "dm_turn_attempts", ["status"])
    op.create_index("ix_dm_turn_attempts_turn_status", "dm_turn_attempts", ["turn_id", "status"])
    op.create_index("ix_dm_turn_attempts_campaign_status", "dm_turn_attempts", ["campaign_id", "status"])
    op.create_index("ix_dm_turn_attempts_worker_job_id", "dm_turn_attempts", ["worker_job_id"])


def downgrade() -> None:
    op.drop_index("ix_dm_turn_attempts_worker_job_id", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_campaign_status", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_turn_status", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_status", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_thread_id", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_campaign_id", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_turn_id", table_name="dm_turn_attempts")
    op.drop_table("dm_turn_attempts")
    op.execute(sa.text("DROP INDEX IF EXISTS uq_dm_turns_active_per_thread"))
    op.drop_index("ix_dm_turns_campaign_status", table_name="dm_turns")
    op.drop_index("ix_dm_turns_campaign_thread_status", table_name="dm_turns")
    op.drop_index("ix_dm_turns_status", table_name="dm_turns")
    op.drop_index("ix_dm_turns_thread_id", table_name="dm_turns")
    op.drop_index("ix_dm_turns_campaign_id", table_name="dm_turns")
    op.drop_table("dm_turns")
