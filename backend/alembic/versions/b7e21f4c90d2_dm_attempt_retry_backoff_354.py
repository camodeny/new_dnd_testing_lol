"""dm attempt retry backoff — issue #354 (additive).

Revision ID: b7e21f4c90d2
Revises: 9c1d209a44aa

Additive only: adds retry_count + next_retry_at to dm_turn_attempts so a
retriable pre-visibility failure can be requeued behind ready work instead
of monopolizing the global oldest-prepared sweep.
Does not touch existing columns or the squashed baseline.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e21f4c90d2"
down_revision: Union[str, Sequence[str], None] = "9c1d209a44aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dm_turn_attempts",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "dm_turn_attempts",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dm_turn_attempts_retry_eligible",
        "dm_turn_attempts", ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dm_turn_attempts_retry_eligible", table_name="dm_turn_attempts")
    op.drop_column("dm_turn_attempts", "next_retry_at")
    op.drop_column("dm_turn_attempts", "retry_count")
