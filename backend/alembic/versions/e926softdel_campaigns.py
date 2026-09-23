"""Add soft deletion to campaigns.

Revision ID: e926softdel
Revises: c425draft01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e926softdel"
down_revision: Union[str, Sequence[str], None] = "c425draft01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "is_deleted")
