"""add transactional outbox — issue 190

Revision ID: e5f6a7b8c9d0
Revises: f6a8b2c1d4e5
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "f6a8b2c1d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False, server_default="campaign"),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_status_next_attempt", "outbox", ["status", "next_attempt_at"])
    op.create_index("ix_outbox_campaign_id", "outbox", ["campaign_id"])
    op.create_index("ix_outbox_aggregate", "outbox", ["aggregate_type", "aggregate_id"])
    op.create_index("ix_outbox_operation_id", "outbox", ["operation_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_operation_id", table_name="outbox")
    op.drop_index("ix_outbox_aggregate", table_name="outbox")
    op.drop_index("ix_outbox_campaign_id", table_name="outbox")
    op.drop_index("ix_outbox_status_next_attempt", table_name="outbox")
    op.drop_table("outbox")
