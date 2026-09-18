"""world knowledge idempotency ledger — issue #211 (additive follow-up).

Revision ID: 9e91bbab4ee3
Revises: e2110a17c211

Additive only: creates world_knowledge_idempotency, the durable record of
every idempotency key ever consumed by a knowledge mutation (create or
re-assertion update). One mutable world_knowledge row can only carry its
latest key, so update-path keys are recorded here; retries resolve through
this ledger and return the current row without re-mutating. Does not touch
existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9e91bbab4ee3"
down_revision: Union[str, Sequence[str], None] = "e2110a17c211"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "world_knowledge_idempotency",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_id"], ["world_knowledge.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_knowledge_idempotency_campaign_key"),
    )
    op.create_index("ix_world_knowledge_idempotency_row", "world_knowledge_idempotency", ["campaign_id", "knowledge_id"])


def downgrade() -> None:
    op.drop_index("ix_world_knowledge_idempotency_row", table_name="world_knowledge_idempotency")
    op.drop_table("world_knowledge_idempotency")
