"""Private player notes (own journal scratch space).

Revision ID: n001playnotes01
Revises: l463loot01
"""

from alembic import op
import sqlalchemy as sa

revision = "n001playnotes01"
down_revision = "l463loot01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_player_notes",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_player_notes_campaign_user", "campaign_player_notes", ["campaign_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_player_notes_campaign_user", table_name="campaign_player_notes")
    op.drop_table("campaign_player_notes")
