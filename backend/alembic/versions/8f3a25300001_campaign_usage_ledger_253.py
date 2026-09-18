"""campaign usage/capacity ledger — issue #253 (additive).

Revision ID: 8f3a25300001
Revises: d3a263a1f263

Additive only: campaign_usage_entries table. Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "8f3a25300001"
down_revision: Union[str, Sequence[str], None] = "d3a263a1f263"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_usage_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("ai_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contributor_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("entry_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_usage_entries_campaign_idempotency"),
        sa.UniqueConstraint("ai_run_id", name="uq_usage_entries_ai_run"),
        sa.CheckConstraint(
            "entry_type IN ('allocation','contribution','added_funds','grace','ai_spend',"
            "'refund','recredit','byok_marker','admin_adjustment')",
            name="ck_usage_entries_entry_type",
        ),
    )
    op.create_index("ix_usage_entries_campaign", "campaign_usage_entries", ["campaign_id"])
    op.create_index("ix_usage_entries_campaign_type", "campaign_usage_entries", ["campaign_id", "entry_type"])
    op.create_index("ix_usage_entries_contributor", "campaign_usage_entries",
                    ["campaign_id", "contributor_user_id"])
    op.create_index("ix_usage_entries_ai_run_id", "campaign_usage_entries", ["ai_run_id"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("ix_usage_entries_ai_run_id", table_name="campaign_usage_entries")
    op.drop_index("ix_usage_entries_contributor", table_name="campaign_usage_entries")
    op.drop_index("ix_usage_entries_campaign_type", table_name="campaign_usage_entries")
    op.drop_index("ix_usage_entries_campaign", table_name="campaign_usage_entries")
    op.drop_table("campaign_usage_entries")
