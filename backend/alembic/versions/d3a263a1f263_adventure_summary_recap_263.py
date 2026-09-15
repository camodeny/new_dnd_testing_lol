"""adventure derived summary/recap — issue #263 (additive).

Revision ID: d3a263a1f263
Revises: f3a1c9260d26 (issue #260 adventure lifecycle — the canonical
adventures table; this revision only extends it)

Additive only: adds the summary source-range columns to ``adventures`` and
creates ``adventure_summaries``. Never creates a second adventures table.
Includes a data backfill binding deterministic source bounds for completed
legacy rows (from their linked completion event); see inline comment.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d3a263a1f263"
down_revision: Union[str, Sequence[str], None] = "f3a1c9260d26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Deploy-time backfill for adventures created before range tracking existed
# (issue #260 rows). Every row matching these predicates is legacy by
# construction: the columns did not exist before this revision, so all
# pre-existing rows carry the server default (start 0 / end NULL), while
# every post-migration open/completion path binds explicit cursors.
#
# - START: estimate the arc opening as the first domain event at/after the
#   adventure's opened timestamp. When no such event exists yet (active
#   adventure with an event-free arc at deploy time), fall back to the
#   campaign's next sequence (max + 1) so the scope begins at deploy instead
#   of silently degrading to full history (0).
# - END: bind completed rows to their linked completion event
#   (domain-event sequence == resulting campaign revision by invariant).
# Kept as importable constants so the focused migration test executes the
# exact deploy-time SQL.
BACKFILL_START_SQL = (
    "UPDATE adventures AS a SET start_sequence = COALESCE(("
    "SELECT MIN(e2.sequence) FROM campaign_domain_events AS e2 "
    "WHERE e2.campaign_id = a.campaign_id AND e2.created_at >= a.started_at"
    "), ("
    "SELECT COALESCE(MAX(e3.sequence), 0) + 1 FROM campaign_domain_events AS e3 "
    "WHERE e3.campaign_id = a.campaign_id"
    ")) WHERE a.start_sequence = 0"
)
BACKFILL_END_SQL = (
    "UPDATE adventures AS a SET end_sequence = e.sequence, end_revision = e.sequence "
    "FROM campaign_domain_events AS e "
    "WHERE a.source_event_id = e.id AND a.status = 'completed' AND a.end_sequence IS NULL"
)


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
    # Backfill source bounds for pre-existing rows (see BACKFILL_*_SQL above).
    op.execute(sa.text(BACKFILL_START_SQL))
    op.execute(sa.text(BACKFILL_END_SQL))


def downgrade() -> None:
    op.drop_index("ix_adventure_summaries_campaign", table_name="adventure_summaries")
    op.drop_table("adventure_summaries")
    op.drop_column("adventures", "end_revision")
    op.drop_column("adventures", "end_sequence")
    op.drop_column("adventures", "start_sequence")
