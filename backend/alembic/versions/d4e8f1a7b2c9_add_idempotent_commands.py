"""add durable idempotent command records — issue 189

Revision ID: d4e8f1a7b2c9
Revises: d3a7c1e9f2b6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d4e8f1a7b2c9"
down_revision: Union[str, Sequence[str], None] = "d3a7c1e9f2b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "idempotent_commands" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "idempotent_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("command_type", sa.String(128), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "idempotency_key", "command_type", "scope_type", "scope_id", name="uq_idempotent_commands_identity"),
    )
    op.create_index("ix_idempotent_commands_actor_id", "idempotent_commands", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_idempotent_commands_actor_id", table_name="idempotent_commands")
    op.drop_table("idempotent_commands")
