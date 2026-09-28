"""Explicit repair records + forward-DM directives — issue #221 (additive).

Revision ID: s221rep01
Revises: rename_source_checksum

Additive only: creates ``campaign_repairs`` (auditable repair records with
issue/source linkage, evidence, before/proposed/applied state, status,
actor/source, correction-visibility, retcon lineage, decision telemetry)
and ``repair_directives`` (one-time forward-DM reconciliation directives
for the #202 REPAIR_DIRECTIVES lane). No existing table is altered.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "s221rep01"
down_revision: Union[str, Sequence[str], None] = "rename_source_checksum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_repairs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repair_key", sa.String(128), nullable=False),
        sa.Column("repair_type", sa.String(32), nullable=False, server_default="deterministic_derived"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("detection_path", sa.String(16), nullable=False, server_default="automatic"),
        sa.Column("affected_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conflicting_records", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proposed_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("applied_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(24), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("requires_player_visible_correction", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("player_visible_correction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="dm_only"),
        sa.Column("is_retcon", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retcon_of_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decision_model", sa.String(128), nullable=True),
        sa.Column("decision_distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["post_turn_consistency_incidents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["retcon_of_id"], ["campaign_repairs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "repair_key", name="uq_campaign_repairs_campaign_key"),
        sa.CheckConstraint(
            "status IN ('pending','adjudication_required','applying','applied','failed','deferred','retconned')",
            name="ck_campaign_repairs_status",
        ),
    )
    op.create_index("ix_campaign_repairs_campaign", "campaign_repairs", ["campaign_id"])
    op.create_index("ix_campaign_repairs_campaign_status", "campaign_repairs", ["campaign_id", "status"])
    op.create_index("ix_campaign_repairs_campaign_incident", "campaign_repairs", ["campaign_id", "incident_id"])
    op.create_index("ix_campaign_repairs_op", "campaign_repairs", ["operation_id"])
    op.create_table(
        "repair_directives",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repair_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("directive_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("audience", sa.String(16), nullable=False, server_default="campaign"),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("player_safe_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="dm_only"),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repair_id"], ["campaign_repairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "directive_key", name="uq_repair_directives_campaign_key"),
        sa.CheckConstraint("status IN ('open','consumed','closed')", name="ck_repair_directives_status"),
    )
    op.create_index("ix_repair_directives_campaign", "repair_directives", ["campaign_id"])
    op.create_index("ix_repair_directives_campaign_status", "repair_directives", ["campaign_id", "status"])
    op.create_index("ix_repair_directives_campaign_repair", "repair_directives", ["campaign_id", "repair_id"])


def downgrade() -> None:
    op.drop_table("repair_directives")
    op.drop_table("campaign_repairs")
