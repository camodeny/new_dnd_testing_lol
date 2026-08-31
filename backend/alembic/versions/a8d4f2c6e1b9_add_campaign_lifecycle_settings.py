"""add campaign lifecycle and launch settings — issue 240

Revision ID: a8d4f2c6e1b9
Revises: a2b3c4d5e6f7
Create Date: 2026-08-30

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8d4f2c6e1b9"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="lobby"
        ),
    )
    op.add_column("campaigns", sa.Column("theme", sa.String(length=128), nullable=True))
    op.add_column("campaigns", sa.Column("brief", sa.Text(), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column(
            "difficulty", sa.String(length=16), nullable=False, server_default="medium"
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "content_boundaries",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_campaigns_lifecycle_status",
        "campaigns",
        "status IN ('lobby', 'starting', 'active', 'archived')",
    )
    op.create_check_constraint(
        "ck_campaigns_required_players_launch_range",
        "campaigns",
        "required_players BETWEEN 1 AND 6",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_campaigns_required_players_launch_range", "campaigns", type_="check"
    )
    op.drop_constraint("ck_campaigns_lifecycle_status", "campaigns", type_="check")
    op.drop_column("campaigns", "content_boundaries")
    op.drop_column("campaigns", "difficulty")
    op.drop_column("campaigns", "brief")
    op.drop_column("campaigns", "theme")
    op.drop_column("campaigns", "status")
