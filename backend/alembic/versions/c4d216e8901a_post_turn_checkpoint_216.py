"""post-turn checkpoint + runs — issue #216 (additive).

Revision ID: c4d216e8901a
Revises: b7e21f4c90d2

Additive only: per-campaign processed-through checkpoint and durable
post-turn run records. Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4d216e8901a"
down_revision: Union[str, Sequence[str], None] = "b7e21f4c90d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "post_turn_checkpoints",
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("processed_through_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_by_run_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("processed_through_sequence >= 0", name="ck_post_turn_checkpoint_nonnegative"),
    )
    op.create_table(
        "post_turn_runs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("to_sequence", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True, index=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True, index=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True, index=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("campaign_id", "from_sequence", "to_sequence", name="uq_post_turn_runs_campaign_range"),
        sa.CheckConstraint("from_sequence >= 1", name="ck_post_turn_runs_from_gte_1"),
        sa.CheckConstraint("to_sequence >= from_sequence", name="ck_post_turn_runs_to_gte_from"),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed','skipped')", name="ck_post_turn_runs_status"),
        sa.CheckConstraint("trigger IN ('normal','force','critical','admin_skip')", name="ck_post_turn_runs_trigger"),
    )


def downgrade() -> None:
    op.drop_table("post_turn_runs")
    op.drop_table("post_turn_checkpoints")
