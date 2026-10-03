"""Remove user-managed provider credentials and BYOK accounting.

Revision ID: rmb257byok01
Revises: b257byok01

Upgrade deletes encrypted credentials, campaign credential policies, credential
trace IDs, and zero-cost usage markers. Downgrade restores the old schema only;
deleted credential and marker data cannot be recovered.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "rmb257byok01"
down_revision = "b257byok01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_ai_runs_credential", table_name="ai_runs")
    op.drop_column("ai_runs", "credential_id")
    op.drop_table("campaign_byok_policies")
    op.drop_table("provider_credentials")
    op.execute("DELETE FROM campaign_usage_entries WHERE entry_type = 'byok_marker'")
    op.drop_constraint("ck_usage_entries_entry_type", "campaign_usage_entries", type_="check")
    op.create_check_constraint(
        "ck_usage_entries_entry_type",
        "campaign_usage_entries",
        "entry_type IN ('allocation','contribution','added_funds','grace','ai_spend',"
        "'refund','recredit','admin_adjustment')",
    )


def downgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key_last4", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_test_result", sa.String(length=32), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_credentials_owner", "provider_credentials", ["owner_user_id"])
    op.create_index("ix_provider_credentials_provider", "provider_credentials", ["provider"])

    op.create_table(
        "campaign_byok_policies",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True),
        sa.Column("authorized_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("campaign_id"),
    )
    op.create_index(
        "ix_campaign_byok_policies_credential", "campaign_byok_policies", ["credential_id"]
    )

    op.add_column("ai_runs", sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_ai_runs_credential", "ai_runs", ["credential_id"])

    op.drop_constraint("ck_usage_entries_entry_type", "campaign_usage_entries", type_="check")
    op.create_check_constraint(
        "ck_usage_entries_entry_type",
        "campaign_usage_entries",
        "entry_type IN ('allocation','contribution','added_funds','grace','ai_spend',"
        "'refund','recredit','byok_marker','admin_adjustment')",
    )
