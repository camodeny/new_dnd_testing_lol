"""adventure lifecycle — issue #260 (additive).

Revision ID: f3a1c9260d26
Revises: c41d210f9a07, c4d216e8901a (merge + additive)

Additive only: lightweight adventure/arc records linked to campaigns.
Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a1c9260d26"
down_revision: Union[str, Sequence[str], None] = ("c41d210f9a07", "c4d216e8901a")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "adventures",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("public_summary", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_turn_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_event_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("closing_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("closing_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("closing_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("campaign_id", "operation_id", name="uq_adventures_campaign_operation"),
        sa.CheckConstraint("status IN ('active', 'completed')", name="ck_adventures_status"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('victory', 'failure', 'retreat', 'capture', 'death', 'tpk', 'villain_victory')",
            name="ck_adventures_outcome",
        ),
        sa.CheckConstraint(
            "closing_status IN ('pending', 'succeeded', 'failed')",
            name="ck_adventures_closing_status",
        ),
    )
    # One active adventure per campaign (partial unique index backstop for
    # concurrent starts; the service also serializes on the campaign row).
    op.create_index(
        "uq_adventures_one_active_per_campaign",
        "adventures",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_adventures_one_active_per_campaign", table_name="adventures")
    op.drop_table("adventures")
