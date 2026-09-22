"""Rebuildable campaign summaries — issue #219 (additive).

Revision ID: s219summ01
Revises: f244lobby02

Additive only: creates ``campaign_summaries`` (derived running-summary
records with source range/revision metadata, claim spans, generation vs.
verification role tracking, and stale/rebuild counters). No existing table
is altered; summaries are derived projections, never canon.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "s219summ01"
down_revision: Union[str, Sequence[str], None] = "f244lobby02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default="running"),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("to_sequence", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("prose", sa.Text(), nullable=True),
        sa.Column("claims", postgresql.JSONB(), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deterministic_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unsupported_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rebuild_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation_provider", sa.String(64), nullable=True),
        sa.Column("generation_model", sa.String(128), nullable=True),
        sa.Column("generation_latency_ms", sa.Integer(), nullable=True),
        sa.Column("verification_policy", postgresql.JSONB(), nullable=True),
        sa.Column("verification_model", sa.String(128), nullable=True),
        sa.Column("support_distribution", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id", "scope", "from_sequence", "to_sequence",
            name="uq_campaign_summaries_campaign_scope_range",
        ),
        sa.CheckConstraint(
            "status IN ('pending','current','stale','failed','deferred')",
            name="ck_campaign_summaries_status",
        ),
    )
    op.create_index(
        "ix_campaign_summaries_campaign_status",
        "campaign_summaries", ["campaign_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_summaries_campaign_status", table_name="campaign_summaries")
    op.drop_table("campaign_summaries")
