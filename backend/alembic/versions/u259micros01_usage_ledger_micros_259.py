"""campaign usage ledger in integer micro-USD — issue #259.

Revision ID: u259micros01
Revises: n001playnotes01

A typical AI run costs a fraction of a cent, so whole-cent ledger lines
rounded every spend to zero. ``campaign_usage_entries.amount_cents`` becomes
``amount_micros`` (BIGINT, 1e-6 USD); existing rows convert exactly (x10,000).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "u259micros01"
down_revision: Union[str, Sequence[str], None] = "n001playnotes01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "campaign_usage_entries", "amount_cents",
        new_column_name="amount_micros",
        type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=False,
    )
    op.execute("UPDATE campaign_usage_entries SET amount_micros = amount_micros * 10000")


def downgrade() -> None:
    op.execute("UPDATE campaign_usage_entries SET amount_micros = amount_micros / 10000")
    op.alter_column(
        "campaign_usage_entries", "amount_micros",
        new_column_name="amount_cents",
        type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=False,
    )
