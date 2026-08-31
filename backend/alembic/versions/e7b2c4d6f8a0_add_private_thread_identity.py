"""add idempotent private gameplay thread identity — issue 247

Revision ID: e7b2c4d6f8a0
Revises: c1d2e3f4a5b6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b2c4d6f8a0"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaign_threads", sa.Column("private_kind", sa.String(16), nullable=True))
    op.add_column("campaign_threads", sa.Column("private_key", sa.String(160), nullable=True))
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_campaign_threads_private_key "
            "ON campaign_threads (campaign_id, private_key) WHERE private_key IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_campaign_threads_private_key"))
    op.drop_column("campaign_threads", "private_key")
    op.drop_column("campaign_threads", "private_kind")
