"""adventure epilogues — issue #262 (additive).

Revision ID: a262epil01
Revises: s249contest01

Additive only: optional per-PC post-adventure epilogue rows plus the
epilogue phase columns on adventures. Does not touch existing tables'
data; the new adventures columns carry server defaults for legacy rows.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a262epil01"
down_revision: Union[str, Sequence[str], None] = "s249contest01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "adventures",
        sa.Column(
            "epilogue_status", sa.String(length=16), nullable=False, server_default="none"
        ),
    )
    op.add_column(
        "adventures",
        sa.Column("epilogues_opened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "adventures",
        sa.Column("epilogues_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_adventures_epilogue_status",
        "adventures",
        sa.text("epilogue_status IN ('none', 'open', 'closed')"),
    )
    op.create_table(
        "adventure_epilogues",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("adventure_id", sa.UUID(as_uuid=True), sa.ForeignKey("adventures.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("character_id", sa.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="simple"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="submitted"),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="public"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("roll_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("roll_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome_text", sa.Text(), nullable=True),
        sa.Column("source_event_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("adventure_id", "character_id", name="uq_adventure_epilogues_adventure_character"),
        sa.UniqueConstraint("campaign_id", "operation_id", name="uq_adventure_epilogues_campaign_operation"),
        sa.CheckConstraint(
            "status IN ('submitted', 'awaiting_roll', 'resolved', 'skipped')",
            name="ck_adventure_epilogues_status",
        ),
        sa.CheckConstraint(
            "kind IN ('simple', 'adjudicated')",
            name="ck_adventure_epilogues_kind",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')",
            name="ck_adventure_epilogues_visibility",
        ),
    )
    op.create_index("ix_adventure_epilogues_adventure", "adventure_epilogues", ["adventure_id"])
    op.create_index("ix_adventure_epilogues_campaign", "adventure_epilogues", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_adventure_epilogues_campaign", table_name="adventure_epilogues")
    op.drop_index("ix_adventure_epilogues_adventure", table_name="adventure_epilogues")
    op.drop_table("adventure_epilogues")
    op.drop_constraint("ck_adventures_epilogue_status", "adventures", type_="check")
    op.drop_column("adventures", "epilogues_closed_at")
    op.drop_column("adventures", "epilogues_opened_at")
    op.drop_column("adventures", "epilogue_status")
