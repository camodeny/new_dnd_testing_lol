"""Campaign monetary capacity ledger — issue #253.

Append-only, auditable usage/capacity accounting for one campaign's shared
pool. One canonical schema (pre-alpha: no legacy shims).

Money is integer cents. Funding entries are positive, AI-spend entries are
negative, BYOK markers are exactly zero. Corrections never mutate history:
post a compensating entry (refund / re-credit / admin adjustment).

Each primary billable AI run from #192 maps to exactly one ``ai_spend``
entry via ``ai_run_id`` (unique). Recovery / non-billable runs must never
produce an entry — enforced in ``app.billing.ledger``.

Privacy: entries carry no payment-provider identifiers or credentials.
Contributor attribution is a profile id + aggregate sums only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# Canonical entry types — single schema, no aliases.
ENTRY_TYPE_ALLOCATION = "allocation"  # subscription / plan funded allocation
ENTRY_TYPE_CONTRIBUTION = "contribution"  # participant contribution to shared pool
ENTRY_TYPE_ADDED_FUNDS = "added_funds"  # top-up (checkout lives in #256)
ENTRY_TYPE_GRACE = "grace"  # bounded grace grant (policy lives elsewhere)
ENTRY_TYPE_AI_SPEND = "ai_spend"  # primary billable AI run cost
ENTRY_TYPE_REFUND = "refund"  # compensating credit for returned funds
ENTRY_TYPE_RECREDIT = "recredit"  # compensating credit (idempotent re-credit, #256)
ENTRY_TYPE_BYOK_MARKER = "byok_marker"  # non-platform spend marker, amount == 0
ENTRY_TYPE_ADMIN_ADJUSTMENT = "admin_adjustment"  # signed operator correction

ENTRY_TYPES = frozenset({
    ENTRY_TYPE_ALLOCATION,
    ENTRY_TYPE_CONTRIBUTION,
    ENTRY_TYPE_ADDED_FUNDS,
    ENTRY_TYPE_GRACE,
    ENTRY_TYPE_AI_SPEND,
    ENTRY_TYPE_REFUND,
    ENTRY_TYPE_RECREDIT,
    ENTRY_TYPE_BYOK_MARKER,
    ENTRY_TYPE_ADMIN_ADJUSTMENT,
})

# Entries that raise the funded threshold (admin_adjustment handled by sign).
FUNDED_CREDIT_TYPES = frozenset({
    ENTRY_TYPE_ALLOCATION,
    ENTRY_TYPE_CONTRIBUTION,
    ENTRY_TYPE_ADDED_FUNDS,
    ENTRY_TYPE_GRACE,
    ENTRY_TYPE_REFUND,
    ENTRY_TYPE_RECREDIT,
})


class CampaignUsageEntry(Base):
    """One immutable ledger line in a campaign's shared capacity pool."""

    __tablename__ = "campaign_usage_entries"
    __table_args__ = (
        UniqueConstraint("campaign_id", "idempotency_key", name="uq_usage_entries_campaign_idempotency"),
        UniqueConstraint("ai_run_id", name="uq_usage_entries_ai_run"),
        CheckConstraint(
            "entry_type IN ('allocation','contribution','added_funds','grace','ai_spend',"
            "'refund','recredit','byok_marker','admin_adjustment')",
            name="ck_usage_entries_entry_type",
        ),
        Index("ix_usage_entries_campaign", "campaign_id"),
        Index("ix_usage_entries_campaign_type", "campaign_id", "entry_type"),
        Index("ix_usage_entries_contributor", "campaign_id", "contributor_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Signed cents: funding > 0, ai_spend < 0, byok_marker == 0, admin any sign != 0? (zero allowed but pointless)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Exactly one ai_spend entry per primary billable AI run; NULL otherwise.
    ai_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, unique=True)
    # Which human funded this line. Gameplay authority is unchanged — the
    # campaign owner / member roles still govern play; this is accounting only.
    contributor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Client-supplied idempotency key, unique per campaign. Retries with the
    # same key return the existing row instead of double-posting.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Human-readable reason. Must never contain payment secrets / provider ids.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Aggregate-safe metadata only (e.g. source label). No credentials.
    entry_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "entry_type": self.entry_type,
            "amount_cents": self.amount_cents,
            "ai_run_id": str(self.ai_run_id) if self.ai_run_id else None,
            "contributor_user_id": str(self.contributor_user_id) if self.contributor_user_id else None,
            "idempotency_key": self.idempotency_key,
            "note": self.note,
            "metadata": self.entry_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_public_dict(self) -> dict:
        """Participant-safe projection: no idempotency keys, no metadata."""
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "entry_type": self.entry_type,
            "amount_cents": self.amount_cents,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
