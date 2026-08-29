"""add character_chat_messages for creator chat persistence

Revision ID: a1b2c3d4e5f6
Revises: 94fb722d0dcc
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "b7c9e2a4f1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "character_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("character_chat_owner_idx", "character_chat_messages", ["owner_id"])
    op.create_index("character_chat_character_idx", "character_chat_messages", ["character_id"])
    op.create_index("character_chat_created_idx", "character_chat_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("character_chat_created_idx", table_name="character_chat_messages")
    op.drop_index("character_chat_character_idx", table_name="character_chat_messages")
    op.drop_index("character_chat_owner_idx", table_name="character_chat_messages")
    op.drop_table("character_chat_messages")
