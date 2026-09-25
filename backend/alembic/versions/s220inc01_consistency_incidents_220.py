"""Post-turn consistency incidents — issue #220 (additive).

Revision ID: s220inc01
Revises: s246start01

Additive only: creates ``post_turn_consistency_incidents`` (explicit
deterministic/semantic consistency incidents with source range, conflicting
records, evidence, severity/type, status, and decision telemetry for the
#221 repair workflow). No existing table is altered; the verifier never
rewrites canon.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "s220inc01"
down_revision: Union[str, Sequence[str], None] = "s246start01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "post_turn_consistency_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("to_sequence", sa.Integer(), nullable=False),
        sa.Column("incident_key", sa.String(128), nullable=False),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False, server_default="canon_conflict"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("detection_path", sa.String(16), nullable=False, server_default="deterministic"),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("affected_records", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decision_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decision_model", sa.String(128), nullable=True),
        sa.Column("decision_distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("detection_latency_ms", sa.Integer(), nullable=True),
        sa.Column("repeat_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "incident_key", name="uq_post_turn_incidents_campaign_key"),
        sa.CheckConstraint(
            "status IN ('open','deferred','verifier_failed','resolved')",
            name="ck_post_turn_incidents_status",
        ),
        sa.CheckConstraint(
            "detection_path IN ('deterministic','semantic','operational')",
            name="ck_post_turn_incidents_path",
        ),
    )
    op.create_index(
        "ix_post_turn_incidents_campaign_status",
        "post_turn_consistency_incidents", ["campaign_id", "status"],
    )
    op.create_index(
        "ix_post_turn_incidents_campaign", "post_turn_consistency_incidents", ["campaign_id"],
    )
    op.create_index(
        "ix_post_turn_incidents_type", "post_turn_consistency_incidents", ["incident_type"],
    )
    op.create_index(
        "ix_post_turn_incidents_op", "post_turn_consistency_incidents", ["operation_id"],
    )


def downgrade() -> None:
    op.drop_table("post_turn_consistency_incidents")
