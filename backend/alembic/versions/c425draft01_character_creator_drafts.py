"""Add character draft lifecycle fields.

Revision ID: c425draft01
Revises: f244lobby03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c425draft01"
down_revision: Union[str, Sequence[str], None] = "f244lobby03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("status", sa.String(length=16), server_default="complete", nullable=False),
    )
    op.add_column(
        "characters",
        sa.Column("creator_step", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_characters_status",
        "characters",
        "status IN ('draft', 'complete')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_characters_status", "characters", type_="check")
    op.drop_column("characters", "creator_step")
    op.drop_column("characters", "status")
