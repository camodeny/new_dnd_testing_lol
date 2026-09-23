"""Add soft deletion to characters.

Revision ID: e927softdelchar
Revises: e926softdel
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e927softdelchar"
down_revision: Union[str, Sequence[str], None] = "e926softdel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("characters", "is_deleted")
