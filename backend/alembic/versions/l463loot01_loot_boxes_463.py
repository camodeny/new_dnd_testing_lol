"""Loot boxes (issue #463).

Revision ID: l463loot01
Revises: c261xp01

The AI DM awards sealed boxes with a generated item pool; the character's
player opens them and code draws the contents onto the sheet.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l463loot01"
down_revision = "c261xp01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loot_boxes",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("character_id", sa.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("audience", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_turn_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("award_key", sa.String(200), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="sealed"),
        sa.Column("draws", sa.Integer(), nullable=False),
        sa.Column("loot_mode", sa.String(32), nullable=False),
        sa.Column("character_level", sa.Integer(), nullable=False),
        sa.Column("pool", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contents", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_by", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("campaign_id", "award_key", name="uq_loot_boxes_award_key"),
        sa.CheckConstraint("status IN ('sealed', 'opened')", name="ck_loot_boxes_status"),
        sa.CheckConstraint("draws BETWEEN 1 AND 6", name="ck_loot_boxes_draws"),
    )
    op.create_index("ix_loot_boxes_campaign_id", "loot_boxes", ["campaign_id"])
    op.create_index("ix_loot_boxes_character", "loot_boxes", ["character_id", "status"])
    # One shared loot-mode set (#463): legacy values the form never offered.
    op.execute(
        "UPDATE campaigns SET loot_mode = 'frequent_gamble' "
        "WHERE loot_mode NOT IN ('frequent_gamble', 'rare_treasure', 'generous', 'scarce')"
    )


def downgrade() -> None:
    op.drop_index("ix_loot_boxes_character", table_name="loot_boxes")
    op.drop_index("ix_loot_boxes_campaign_id", table_name="loot_boxes")
    op.drop_table("loot_boxes")
