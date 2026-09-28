"""Stripe add-funds funding operations + webhook audit — issue #256.

Payment records live here, separate from fictional game state: these tables
reference campaigns only for reconciliation/accounting and never join
narrative, rules, or DM inputs. No card/payment credentials are ever stored —
Stripe owns card-data collection/processing; we persist only Stripe object
and event identifiers needed for reconciliation and audit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# Funding-operation lifecycle. Terminal states never transition back to
# pending: ambiguous late Stripe signals are reconciled, never guessed.
FUNDING_STATUS_PENDING = "pending"
FUNDING_STATUS_CONFIRMED = "confirmed"
FUNDING_STATUS_FAILED = "failed"
FUNDING_STATUS_CANCELED = "canceled"

FUNDING_STATUSES = frozenset({
    FUNDING_STATUS_PENDING,
    FUNDING_STATUS_CONFIRMED,
    FUNDING_STATUS_FAILED,
    FUNDING_STATUS_CANCELED,
})

FUNDING_TERMINAL_STATUSES = frozenset({
    FUNDING_STATUS_CONFIRMED,
    FUNDING_STATUS_FAILED,
    FUNDING_STATUS_CANCELED,
})

# Webhook event application outcomes (audit only).
WEBHOOK_APPLIED_PROCESSED = "processed"
WEBHOOK_APPLIED_DUPLICATE = "duplicate"
WEBHOOK_APPLIED_IGNORED = "ignored"


class CampaignFundingOperation(Base):
    """One logical campaign add-funds intent, reconciled against Stripe.

    The internal id is the stable funding-operation ID: Stripe checkout
    creation uses ``funding-op:{id}`` as its idempotency key, so retrying the
    same logical operation can never create duplicate charges. The
    ``(campaign_id, idempotency_key)`` unique constraint gives the same
    guarantee at the API layer.
    """

    __tablename__ = "campaign_funding_operations"
    __table_args__ = (
        UniqueConstraint("campaign_id", "idempotency_key", name="uq_funding_ops_campaign_idempotency"),
        UniqueConstraint("stripe_checkout_session_id", name="uq_funding_ops_stripe_session"),
        CheckConstraint(
            "status IN ('pending','confirmed','failed','canceled')",
            name="ck_funding_ops_status",
        ),
        CheckConstraint("amount_cents > 0", name="ck_funding_ops_amount_positive"),
        Index("ix_funding_ops_campaign", "campaign_id"),
        Index("ix_funding_ops_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="usd", server_default="usd")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FUNDING_STATUS_PENDING)
    # API-layer idempotency: retries with the same key return this row.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Who funded this operation. Accounting-only attribution; gameplay
    # authority is unchanged.
    contributor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Stripe reconciliation identifiers (never card/payment credentials).
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    stripe_confirm_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # One-time Stripe-hosted checkout URL (returned while pending so a lost
    # browser can resume the same checkout; never a credential).
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The exactly-one #253 ``added_funds`` ledger entry minted on confirm.
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> dict:
        """Member-safe projection: aggregates + status only.

        Never exposes Stripe identifiers, contributor identity, or payment
        details — participants get aggregate capacity via capacity-state.
        """
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "status": self.status,
            "checkout_url": self.checkout_url if self.status == FUNDING_STATUS_PENDING else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }


class StripeWebhookEvent(Base):
    """Audit record for one received Stripe webhook event.

    The ``stripe_event_id`` unique constraint is the duplicate-delivery
    guard: a redelivered event collides here and is answered as a duplicate
    without reprocessing. Only the minimum reconciliation fields are
    persisted — never raw card/payment credentials.
    """

    __tablename__ = "stripe_webhook_events"
    __table_args__ = (
        UniqueConstraint("stripe_event_id", name="uq_stripe_webhook_events_event_id"),
        Index("ix_stripe_webhook_events_campaign", "campaign_id"),
        Index("ix_stripe_webhook_events_operation", "funding_operation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    funding_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_funding_operations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    applied: Mapped[str] = mapped_column(String(16), nullable=False, default=WEBHOOK_APPLIED_PROCESSED)
    # Minimal reconciliation envelope only (event/object ids + status —
    # never credentials).
    receipt: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
