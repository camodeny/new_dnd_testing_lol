"""Public party composition support + private character lore — issue #244 (additive).

Revision ID: f244lobby01
Revises: f243lobby01

Additive only: new ``campaign_character_lore`` table (one row per
campaign+character, private DM-setup lore). No existing tables or rows
touched. Party composition itself is a derived read projection over the
existing lobby members/sheets — no schema change needed for it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f244lobby01"
down_revision: Union[str, Sequence[str], None] = "f243lobby01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_character_lore",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("campaign_id", "character_id", name="uq_character_lore_campaign_character"),
    )
    op.create_index("ix_character_lore_campaign", "campaign_character_lore", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_character_lore_campaign", table_name="campaign_character_lore")
    op.drop_table("campaign_character_lore")
