"""bounded decision telemetry — issue #383 (additive).

Revision ID: d383teleme01
Revises: f230encounter01

Additive only: creates decision_telemetry (one row per evaluated bounded
decision question: role, adapter/model versions, schema versions,
candidate IDs, selection, full distribution, runner-up/margin,
latency/cost, trace/campaign/turn linkage, frame/state revisions,
execution mode, policy directive, revalidation result, ground truth, and
downstream correction signals). No existing tables touched; deploy runs
migrations explicitly (no cold-start DDL).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d383teleme01"
down_revision: Union[str, Sequence[str], None] = "f230encounter01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "decision_telemetry",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True, index=True),
        sa.Column("decision_class", sa.String(length=128), nullable=False, index=True),
        sa.Column("question_id", sa.String(length=128), nullable=False),
        sa.Column("question_kind", sa.String(length=16), nullable=False, server_default="choice"),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=True),
        sa.Column("candidate_schema_version", sa.Integer(), nullable=True),
        sa.Column("frame_schema_version", sa.Integer(), nullable=True),
        sa.Column("policy_schema_version", sa.Integer(), nullable=True),
        sa.Column("telemetry_schema_version", sa.Integer(), nullable=True),
        sa.Column("candidate_ids", postgresql.JSONB(), nullable=True),
        sa.Column("selected_id", sa.String(length=256), nullable=True),
        sa.Column("probabilities", postgresql.JSONB(), nullable=True),
        sa.Column("runner_up_id", sa.String(length=256), nullable=True),
        sa.Column("margin", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("turn_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turns.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("frame_id", sa.String(length=64), nullable=True, index=True),
        sa.Column("state_revision", sa.String(length=128), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("policy_directive", sa.String(length=32), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=True),
        sa.Column("revalidation_error", sa.Text(), nullable=True),
        sa.Column("ground_truth_id", sa.String(length=256), nullable=True),
        sa.Column("correction_source", sa.String(length=128), nullable=True),
        sa.Column("correction_indicates_wrong", sa.Boolean(), nullable=True),
        sa.Column("corrected_to", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_decision_telemetry_class_mode",
        "decision_telemetry", ["decision_class", "mode"],
    )


def downgrade() -> None:
    op.drop_index("ix_decision_telemetry_class_mode", table_name="decision_telemetry")
    op.drop_table("decision_telemetry")
