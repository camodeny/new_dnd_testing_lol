"""staged effects and three-phase turn commit semantics — issue 206

Revision ID: a9b8c7d6e5f4
Revises: e7b2c4d6f8a0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "e7b2c4d6f8a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # dm_turn_attempts — staged effects + audit + idempotency + abandonment
    op.add_column("dm_turn_attempts", sa.Column("staged_effects", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("dm_turn_attempts", sa.Column("contract_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dm_turn_attempts", sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dm_turn_attempts", sa.Column("commit_operation_id", sa.String(128), nullable=True))
    op.add_column("dm_turn_attempts", sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dm_turn_attempts", sa.Column("abandonment_reason", sa.String(64), nullable=True))
    op.create_index("ix_dm_turn_attempts_stream_id", "dm_turn_attempts", ["stream_id"])
    op.create_index("ix_dm_turn_attempts_commit_operation_id", "dm_turn_attempts", ["commit_operation_id"])
    # FK to dm_streams (SET NULL on delete, nullable)
    op.create_foreign_key("fk_dm_turn_attempts_stream_id", "dm_turn_attempts", "dm_streams", ["stream_id"], ["id"], ondelete="SET NULL")

    # dm_turns — commit observability
    op.add_column("dm_turns", sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dm_turns", sa.Column("commit_duration_ms", sa.Integer(), nullable=True))
    op.add_column("dm_turns", sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dm_turns", sa.Column("abandonment_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("dm_turns", "abandonment_reason")
    op.drop_column("dm_turns", "abandoned_at")
    op.drop_column("dm_turns", "commit_duration_ms")
    op.drop_column("dm_turns", "committed_at")

    op.drop_constraint("fk_dm_turn_attempts_stream_id", "dm_turn_attempts", type_="foreignkey")
    op.drop_index("ix_dm_turn_attempts_commit_operation_id", table_name="dm_turn_attempts")
    op.drop_index("ix_dm_turn_attempts_stream_id", table_name="dm_turn_attempts")
    op.drop_column("dm_turn_attempts", "abandonment_reason")
    op.drop_column("dm_turn_attempts", "abandoned_at")
    op.drop_column("dm_turn_attempts", "commit_operation_id")
    op.drop_column("dm_turn_attempts", "stream_id")
    op.drop_column("dm_turn_attempts", "contract_snapshot")
    op.drop_column("dm_turn_attempts", "staged_effects")
