"""Private lore visibility tier — issue #244 follow-up (additive).

Revision ID: f244lobby02
Revises: f244lobby01

Additive only: explicit ``visibility`` column on ``campaign_character_lore``
(always ``'private'``) so the authorization rule keys off stored data per
the #211 private model instead of table identity.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f244lobby02"
down_revision: Union[str, Sequence[str], None] = "f244lobby01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaign_character_lore",
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
    )


def downgrade() -> None:
    op.drop_column("campaign_character_lore", "visibility")
