"""adventure lifecycle + derived summary/recap — issue #263 (additive).

Revision ID: d3a263a1f263
Revises: c4d216e8901a, c41d210f9a07 (merge — both were heads)

Additive only: creates adventures + adventure_summaries. Does not touch
existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d3a263a1f263"
down_revision: Union[str, Sequence[str], None] = ("c4d216e8901a", "c41d210f9a07")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "adventures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(length=256), nullable=False, server_default="Untitled Adventure"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("source_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("start_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("end_sequence", sa.Integer(), nullable=True),
        sa.Column("end_revision", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_adventures_campaign_idempotency"),
        sa.CheckConstraint("status IN ('open', 'completed')", name="ck_adventures_status"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('victory','defeat','retreat','capture','tpk','villain_victory','draw','pyrrhic_victory')",
            name="ck_adventures_outcome",
        ),
    )
    op.create_index("ix_adventures_campaign_status", "adventures", ["campaign_id", "status"])
    op.create_table(
        "adventure_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("adventure_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("adventures.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("source_event_from", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_event_to", sa.Integer(), nullable=True),
        sa.Column("source_revision", sa.Integer(), nullable=True),
        sa.Column("is_derived", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("historical_text", sa.Text(), nullable=True),
        sa.Column("recap_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stale_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rebuild_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("validation_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("leak_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("views", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("adventure_id", name="uq_adventure_summaries_adventure"),
        sa.CheckConstraint("status IN ('pending', 'current', 'stale', 'failed')", name="ck_adventure_summaries_status"),
    )
    op.create_index("ix_adventure_summaries_campaign", "adventure_summaries", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_adventure_summaries_campaign", table_name="adventure_summaries")
    op.drop_table("adventure_summaries")
    op.drop_index("ix_adventures_campaign_status", table_name="adventures")
    op.drop_table("adventures")
