"""Drop unused campaign summaries and repair tables.

Revision ID: drop219221
Revises: rmb257byok01

``campaign_summaries`` (#219) was written by post-turn but never read, and
``campaign_repairs`` / ``repair_directives`` (#221) had no production entry
point. Downgrade restores the empty tables only.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "drop219221"
down_revision = "rmb257byok01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("repair_directives")
    op.drop_table("campaign_repairs")
    op.drop_index("ix_campaign_summaries_campaign_status", table_name="campaign_summaries")
    op.drop_table("campaign_summaries")


def downgrade() -> None:
    op.create_table(
        "campaign_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default="running"),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("to_sequence", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("prose", sa.Text(), nullable=True),
        sa.Column("claims", postgresql.JSONB(), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deterministic_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unsupported_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rebuild_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation_provider", sa.String(64), nullable=True),
        sa.Column("generation_model", sa.String(128), nullable=True),
        sa.Column("generation_latency_ms", sa.Integer(), nullable=True),
        sa.Column("verification_policy", postgresql.JSONB(), nullable=True),
        sa.Column("verification_model", sa.String(128), nullable=True),
        sa.Column("support_distribution", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id", "scope", "from_sequence", "to_sequence",
            name="uq_campaign_summaries_campaign_scope_range",
        ),
        sa.CheckConstraint(
            "status IN ('pending','current','stale','failed','deferred')",
            name="ck_campaign_summaries_status",
        ),
    )
    op.create_index(
        "ix_campaign_summaries_campaign_status",
        "campaign_summaries", ["campaign_id", "status"],
    )

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

