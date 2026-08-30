"""add end-to-end runtime observability — issue 192

Revision ID: b2c3d4e5f6a7
Revises: f0a1b2c3d4e5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    outbox_columns = {column["name"] for column in inspector.get_columns("outbox")}
    if "trace_id" not in outbox_columns:
        op.add_column("outbox", sa.Column("trace_id", sa.String(64), nullable=True))
        op.create_index("ix_outbox_trace_id", "outbox", ["trace_id"])
    event_columns = {column["name"] for column in inspector.get_columns("campaign_domain_events")}
    if "trace_id" not in event_columns:
        op.add_column("campaign_domain_events", sa.Column("trace_id", sa.String(64), nullable=True))
        op.create_index("ix_campaign_domain_events_trace_id", "campaign_domain_events", ["trace_id"])
    tables = set(inspector.get_table_names())
    if "operation_traces" not in tables:
        op.create_table(
            "operation_traces",
            sa.Column("trace_id", sa.String(64), primary_key=True),
            sa.Column("operation_id", sa.String(128), nullable=False),
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True)),
            sa.Column("worker_started_at", sa.DateTime(timezone=True)),
            sa.Column("first_visible_at", sa.DateTime(timezone=True)),
            sa.Column("narration_completed_at", sa.DateTime(timezone=True)),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
            sa.Column("telemetry_dropped", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_operation_traces_operation_id", "operation_traces", ["operation_id"])
        op.create_index("ix_operation_traces_campaign_id", "operation_traces", ["campaign_id"])
    if "ai_runs" not in tables:
        op.create_table(
            "ai_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("trace_id", sa.String(64), nullable=False),
            sa.Column("operation_id", sa.String(128), nullable=False),
            sa.Column("parent_run_id", postgresql.UUID(as_uuid=True)),
            sa.Column("logical_operation", sa.String(128), nullable=False),
            sa.Column("role", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("classification", sa.String(16), nullable=False),
            sa.Column("billable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("first_token_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("input_tokens", sa.Integer()), sa.Column("output_tokens", sa.Integer()),
            sa.Column("cost_usd", sa.Float()), sa.Column("result_code", sa.String(64)),
            sa.Column("error_type", sa.String(128)), sa.Column("content_metadata", postgresql.JSONB()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        )
        op.create_index("ix_ai_runs_trace_id", "ai_runs", ["trace_id"])
        op.create_index("ix_ai_runs_operation_id", "ai_runs", ["operation_id"])


def downgrade() -> None:
    op.drop_table("ai_runs")
    op.drop_table("operation_traces")
    op.drop_index("ix_campaign_domain_events_trace_id", table_name="campaign_domain_events")
    op.drop_column("campaign_domain_events", "trace_id")
    op.drop_index("ix_outbox_trace_id", table_name="outbox")
    op.drop_column("outbox", "trace_id")
