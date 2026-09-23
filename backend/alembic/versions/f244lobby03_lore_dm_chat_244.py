"""Private lore-DM setup chat messages — issue #244 follow-up (additive).

Revision ID: f244lobby03
Revises: s219summ01

Additive only: new ``campaign_lore_chat_messages`` table holding the
per-(campaign, character, player) guided back-and-forth that helps players
write private setup lore before launch. The DM side is advisory only —
nothing here is canon and the seed job never reads this table. Reads are
owner-only (fail closed); writes freeze with lore once the campaign leaves
the lobby (enforced in the router, not the schema).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f244lobby03"
down_revision: Union[str, Sequence[str], None] = "s219summ01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_lore_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("proposal_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_lore_chat_campaign_character", "campaign_lore_chat_messages", ["campaign_id", "character_id"])
    op.create_index("ix_lore_chat_user", "campaign_lore_chat_messages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_lore_chat_user", table_name="campaign_lore_chat_messages")
    op.drop_index("ix_lore_chat_campaign_character", table_name="campaign_lore_chat_messages")
    op.drop_table("campaign_lore_chat_messages")
