"""add worker claim_token for lease fencing — issue 191 follow-up

Revision ID: f1a2b3c4d5e6
Revises: f0a1b2c3d4e5
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_executions", sa.Column("claim_token", sa.String(64), nullable=True))
    op.create_index("ix_worker_executions_claim_token", "worker_executions", ["claim_token"])


def downgrade() -> None:
    op.drop_index("ix_worker_executions_claim_token", table_name="worker_executions")
    op.drop_column("worker_executions", "claim_token")
