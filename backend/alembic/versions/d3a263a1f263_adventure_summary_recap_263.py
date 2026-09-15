"""adventure derived summary/recap — issue #263 (additive).

Revision ID: d3a263a1f263
Revises: f3a1c9260d26 (issue #260 adventure lifecycle — the canonical
adventures table; this revision only extends it)

Additive only: adds the summary source-range columns to ``adventures`` and
creates ``adventure_summaries``. Never creates a second adventures table.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d3a263a1f263"
down_revision: Union[str, Sequence[str], None] = "f3a1c9260d26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Source-range boundary for derived summaries on the canonical #260 table.
    op.add_column(
        "adventures",
        sa.Column("start_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("adventures", sa.Column("end_sequence", sa.Integer(), nullable=True))
    op.add_column("adventures", sa.Column("end_revision", sa.Integer(), nullable=True))
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
    op.drop_column("adventures", "end_revision")
    op.drop_column("adventures", "end_sequence")
    op.drop_column("adventures", "start_sequence")
