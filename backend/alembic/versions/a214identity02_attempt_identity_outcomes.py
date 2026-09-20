"""attempt-local pre-narration identity outcomes — issue #214.

Revision ID: a214identity02
Revises: a214identity01
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a214identity02"
down_revision: Union[str, Sequence[str], None] = "a214identity01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dm_turn_attempts", sa.Column("identity_resolutions", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("dm_turn_attempts", "identity_resolutions")
