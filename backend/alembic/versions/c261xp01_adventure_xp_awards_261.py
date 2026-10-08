"""Adventure-closing XP ledger (issue #261).

Revision ID: c261xp01
Revises: a234attack01

One row per (adventure, character): the durable exactly-once fence for the
XP the adventure-closing sweep adds to a character's sheet.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c261xp01"
down_revision = "a234attack01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adventure_xp_awards",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("adventure_id", sa.UUID(as_uuid=True), sa.ForeignKey("adventures.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("character_id", sa.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("xp_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xp_before", sa.Integer(), nullable=True),
        sa.Column("xp_after", sa.Integer(), nullable=True),
        sa.Column("qualifies_for_level", sa.Integer(), nullable=True),
        sa.Column("breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("adventure_id", "character_id", name="uq_adventure_xp_awards_adventure_character"),
        sa.CheckConstraint("xp_awarded >= 0", name="ck_adventure_xp_awards_nonnegative"),
    )


def downgrade() -> None:
    op.drop_table("adventure_xp_awards")
