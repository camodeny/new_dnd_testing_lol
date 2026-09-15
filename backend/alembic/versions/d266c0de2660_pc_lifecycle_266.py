"""replacement-character lifecycle — issue #266 (additive).

Revision ID: d266c0de2660
Revises: c41d210f9a07, c4d216e8901a (merge — main carried both heads)

Additive only: creates campaign_pc_lifecycles (active/dead/retired PC
lifecycle + replacement links + introduction status). Does not touch
existing tables or rows.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d266c0de2660"
down_revision: Union[str, Sequence[str], None] = ("c41d210f9a07", "c4d216e8901a")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_pc_lifecycles",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("died_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_tpk", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("replaced_by_character_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("replacement_of_character_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("introduction_status", sa.String(24), nullable=False, server_default="na"),
        sa.Column("introduced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'dead', 'retired')", name="ck_pc_lifecycles_status"),
        sa.CheckConstraint(
            "introduction_status IN ('na', 'pending_introduction', 'introduced')",
            name="ck_pc_lifecycles_introduction_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("campaign_pc_lifecycles")
