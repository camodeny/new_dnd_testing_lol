"""stripe add-funds funding operations + webhook audit — issue #256 (additive).

Revision ID: 9f4a25600001
Revises: rename_source_checksum

Additive only: campaign_funding_operations + stripe_webhook_events tables.
Payment records stay separate from fictional game state; no card/payment
credentials are stored anywhere. Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9f4a25600001"
down_revision: Union[str, Sequence[str], None] = "rename_source_checksum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_funding_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="usd"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("contributor_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_confirm_event_id", sa.String(length=128), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_funding_ops_campaign_idempotency"),
        sa.UniqueConstraint("stripe_checkout_session_id", name="uq_funding_ops_stripe_session"),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','failed','canceled')",
            name="ck_funding_ops_status",
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_funding_ops_amount_positive"),
    )
    op.create_index("ix_funding_ops_campaign", "campaign_funding_operations", ["campaign_id"])
    op.create_index("ix_funding_ops_campaign_status", "campaign_funding_operations", ["campaign_id", "status"])
    op.create_index(
        "ix_funding_ops_payment_intent", "campaign_funding_operations", ["stripe_payment_intent_id"]
    )

    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("funding_operation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaign_funding_operations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("applied", sa.String(length=16), nullable=False, server_default="processed"),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_event_id", name="uq_stripe_webhook_events_event_id"),
    )
    op.create_index("ix_stripe_webhook_events_campaign", "stripe_webhook_events", ["campaign_id"])
    op.create_index("ix_stripe_webhook_events_operation", "stripe_webhook_events", ["funding_operation_id"])


def downgrade() -> None:
    op.drop_index("ix_stripe_webhook_events_operation", table_name="stripe_webhook_events")
    op.drop_index("ix_stripe_webhook_events_campaign", table_name="stripe_webhook_events")
    op.drop_table("stripe_webhook_events")
    op.drop_index("ix_funding_ops_payment_intent", table_name="campaign_funding_operations")
    op.drop_index("ix_funding_ops_campaign_status", table_name="campaign_funding_operations")
    op.drop_index("ix_funding_ops_campaign", table_name="campaign_funding_operations")
    op.drop_table("campaign_funding_operations")
