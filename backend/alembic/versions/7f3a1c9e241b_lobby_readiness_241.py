"""campaign lobby character selection/readiness — issue #241 (additive).

Revision ID: 7f3a1c9e241b
Revises: 9c1d209a44aa

Additive only: adds selected_character_id + is_ready + ready_at to
campaign_members. Does not touch existing rows beyond defaults.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "7f3a1c9e241b"
down_revision: Union[str, Sequence[str], None] = "9c1d209a44aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaign_members",
        sa.Column("selected_character_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "campaign_members",
        sa.Column("is_ready", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "campaign_members",
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaign_members_selected_character",
        "campaign_members",
        "characters",
        ["selected_character_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_campaign_members_selected_character",
        "campaign_members",
        ["selected_character_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_members_selected_character", table_name="campaign_members")
    op.drop_constraint("fk_campaign_members_selected_character", "campaign_members", type_="foreignkey")
    op.drop_column("campaign_members", "ready_at")
    op.drop_column("campaign_members", "is_ready")
    op.drop_column("campaign_members", "selected_character_id")
