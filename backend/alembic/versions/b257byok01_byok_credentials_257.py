"""BYOK provider credentials + campaign policy — issue #257 (additive).

Revision ID: b257byok01
Revises: 9f4a25600001

Additive only: provider_credentials + campaign_byok_policies tables and a
nullable ai_runs.credential_id trace column (credential IDs only, never key
material). Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b257byok01"
down_revision: Union[str, Sequence[str], None] = "9f4a25600001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_ai_runs_credential", table_name="ai_runs")
    op.drop_column("ai_runs", "credential_id")
    op.drop_index("ix_campaign_byok_policies_credential", table_name="campaign_byok_policies")
    op.drop_table("campaign_byok_policies")
    op.drop_index("ix_provider_credentials_provider", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_owner", table_name="provider_credentials")
    op.drop_table("provider_credentials")
