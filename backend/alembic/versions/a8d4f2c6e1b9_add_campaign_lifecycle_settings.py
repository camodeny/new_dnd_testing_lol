"""add campaign lifecycle and launch settings — issue 240

Revision ID: a8d4f2c6e1b9
Revises: c1d2e3f4a5b6
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8d4f2c6e1b9"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("campaigns")}

    if "status" not in columns:
        op.add_column(
            "campaigns",
            sa.Column("status", sa.String(length=16), nullable=False, server_default="lobby"),
        )
    if "theme" not in columns:
        op.add_column("campaigns", sa.Column("theme", sa.String(length=128), nullable=True))
    if "brief" not in columns:
        op.add_column("campaigns", sa.Column("brief", sa.Text(), nullable=True))
        op.execute(sa.text("UPDATE campaigns SET brief = description WHERE description IS NOT NULL"))
    if "difficulty" not in columns:
        op.add_column(
            "campaigns",
            sa.Column("difficulty", sa.String(length=16), nullable=False, server_default="medium"),
        )
    if "content_boundaries" not in columns:
        op.add_column(
            "campaigns",
            sa.Column(
                "content_boundaries",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # Old pre-alpha requests could be silently clamped as high as eight. Normalize
    # stored rows before installing the authoritative 1–6 database invariant.
    op.execute(sa.text("UPDATE campaigns SET required_players = LEAST(6, GREATEST(1, required_players))"))

    checks = {constraint.get("name") for constraint in sa.inspect(bind).get_check_constraints("campaigns")}
    if "ck_campaigns_lifecycle_status" not in checks:
        op.create_check_constraint(
            "ck_campaigns_lifecycle_status",
            "campaigns",
            "status IN ('lobby', 'starting', 'active', 'archived')",
        )
    if "ck_campaigns_required_players_launch_range" not in checks:
        op.create_check_constraint(
            "ck_campaigns_required_players_launch_range",
            "campaigns",
            "required_players BETWEEN 1 AND 6",
        )


def downgrade() -> None:
    bind = op.get_bind()
    checks = {constraint.get("name") for constraint in sa.inspect(bind).get_check_constraints("campaigns")}
    if "ck_campaigns_required_players_launch_range" in checks:
        op.drop_constraint("ck_campaigns_required_players_launch_range", "campaigns", type_="check")
    if "ck_campaigns_lifecycle_status" in checks:
        op.drop_constraint("ck_campaigns_lifecycle_status", "campaigns", type_="check")

    columns = {column["name"] for column in sa.inspect(bind).get_columns("campaigns")}
    for column in ("content_boundaries", "difficulty", "brief", "theme", "status"):
        if column in columns:
            op.drop_column("campaigns", column)
