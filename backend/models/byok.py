"""BYOK provider credentials + campaign authorization policy — issue #257.

One row per user-managed provider credential. Secrets are stored only as
server-encrypted ciphertext (``encrypted_secret``); reads project through
``to_masked_dict`` which never includes ciphertext, plaintext, or key
material. ``key_fingerprint`` (SHA-256 hex) supports rotation detection;
``key_last4`` supports masked UX only.

``CampaignByokPolicy`` is the explicit authorized-campaign policy: at most
one active credential per campaign, set only by the campaign owner. The key
itself is never exposed to or shared with campaign members — execution
resolves the policy server-side and injects the decrypted secret only into
server-to-provider transport.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


# Canonical credential lifecycle states (single schema, no aliases).
CREDENTIAL_STATUS_ACTIVE = "active"
CREDENTIAL_STATUS_INVALID = "invalid"
CREDENTIAL_STATUSES = frozenset({CREDENTIAL_STATUS_ACTIVE, CREDENTIAL_STATUS_INVALID})


class ProviderCredential(Base):
    """One user-managed provider credential (BYOK)."""

    __tablename__ = "provider_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    # Fernet ciphertext of the raw secret. Never returned, never logged.
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex of the raw secret (rotation/change detection, not reversible).
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Last 4 chars of the secret for masked UX (e.g. "••••3f9a").
    key_last4: Mapped[str] = mapped_column(String(8), nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    last_test_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_masked_dict(self) -> dict:
        """Participant/owner-safe projection: no ciphertext, no secret."""
        return {
            "id": str(self.id),
            "owner_user_id": str(self.owner_user_id),
            "provider": self.provider,
            "label": self.label,
            "key_hint": f"••••{self.key_last4}" if self.key_last4 else "••••",
            "status": self.status,
            "last_test_result": self.last_test_result,
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignByokPolicy(Base):
    """Explicit authorized-campaign BYOK policy — issue #257.

    At most one row per campaign (``campaign_id`` is the primary key). Only
    the campaign owner may set/clear it. ``credential_id`` references the
    authorized member credential; ON DELETE SET NULL so credential removal
    returns routing to funded/provider policy without corrupting gameplay
    state. ``enabled`` gates execution use without deleting the assignment.
    """

    __tablename__ = "campaign_byok_policies"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    authorized_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self, *, credential: ProviderCredential | None = None) -> dict:
        """Owner/member-safe projection: credential IDs only, never secrets."""
        return {
            "campaign_id": str(self.campaign_id),
            "credential_id": str(self.credential_id) if self.credential_id else None,
            "credential": credential.to_masked_dict() if credential is not None else None,
            "authorized_by": str(self.authorized_by) if self.authorized_by else None,
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
