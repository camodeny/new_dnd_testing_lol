"""add worker execution ledger and failed-work — issue 191

Revision ID: f0a1b2c3d4e5
Revises: e5f6a7b8c9d0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "worker_executions" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "worker_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(128), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("aggregate_type", sa.String(64), nullable=True),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(16), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_executions_campaign_id", "worker_executions", ["campaign_id"])
    op.create_index("ix_worker_executions_operation_id", "worker_executions", ["operation_id"])
    op.create_index("ix_worker_executions_idempotency_key", "worker_executions", ["idempotency_key"])
    op.create_index("ix_worker_executions_trace_id", "worker_executions", ["trace_id"])
    op.create_index("ix_worker_executions_status_next_attempt", "worker_executions", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_executions_status_next_attempt", table_name="worker_executions")
    op.drop_index("ix_worker_executions_trace_id", table_name="worker_executions")
    op.drop_index("ix_worker_executions_idempotency_key", table_name="worker_executions")
    op.drop_index("ix_worker_executions_operation_id", table_name="worker_executions")
    op.drop_index("ix_worker_executions_campaign_id", table_name="worker_executions")
    op.drop_table("worker_executions")
