"""durable campaign clocks — issue #218 (additive).

Revision ID: a218clocks01
Revises: a214identity02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a218clocks01"
down_revision: Union[str, Sequence[str], None] = "a214identity02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn is not None and conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore
    op.create_table(
        "campaign_clocks",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("stages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("advancement_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("completion_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completion_effect", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="dm_only"),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("progress_carry", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("evaluated_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_turn_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_campaign_clocks_campaign_idempotency"),
        sa.CheckConstraint("progress >= 0", name="ck_campaign_clocks_progress_nonnegative"),
        sa.CheckConstraint("threshold >= 1", name="ck_campaign_clocks_threshold_positive"),
        sa.CheckConstraint("progress_carry >= 0", name="ck_campaign_clocks_carry_nonnegative"),
        sa.CheckConstraint("revision >= 1", name="ck_campaign_clocks_revision_positive"),
        sa.CheckConstraint("evaluated_through_sequence >= 0", name="ck_campaign_clocks_evaluated_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending','active','ticking','dormant','completed','retired')",
            name="ck_campaign_clocks_status",
        ),
    )
    op.create_index("ix_campaign_clocks_campaign_status", "campaign_clocks", ["campaign_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_campaign_clocks_campaign_status", table_name="campaign_clocks")
    op.drop_table("campaign_clocks")
